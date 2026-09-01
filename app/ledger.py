from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import uuid4

from .config import local_now
from .engine import validate_allocation
from .platform_catalog import PlatformCatalogService
from .portfolio import (
    active_capital_summary,
    active_index_exposures,
    fund_exposure_identity,
    infer_active_capital,
)
from .trading_calendar import ChinaTradingCalendar


BUY_ACTIONS = {"买入", "定投", "转换转入"}
EXIT_ACTIONS = {"卖出", "清仓", "转换", "转换转出"}
POSTED_STATUS = "已确认"
_CRITICAL_FIELDS = (
    "trade_date", "fund_code", "action", "amount", "units", "nav", "fee",
    "confirm_date", "nav_date", "status", "counts_toward_active_capital",
)


class LedgerError(RuntimeError):
    pass


class PortfolioLedgerService:
    """Post confirmed OTC-fund transactions exactly once.

    A posted trade is an accounting fact.  It may be annotated, but changing
    fund/action/cash/units/NAV/dates requires an explicit reversal followed by
    a new correcting trade.  This prevents a later edit from silently putting
    the trade log, cost lots, holdings and cash ledger out of sync.
    """

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _ensure_tables(conn) -> None:
        # Kept here as a backward-compatible guard for databases created by an
        # older executable.  The versioned migration creates the same schema.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledger_postings (
                trade_id INTEGER PRIMARY KEY REFERENCES trades(id),
                posting_kind TEXT NOT NULL,
                critical_snapshot_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                posted_at TEXT NOT NULL,
                reversed_at TEXT NOT NULL DEFAULT '',
                reversal_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS lot_consumptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exit_trade_id INTEGER NOT NULL REFERENCES trades(id),
                sequence_no INTEGER NOT NULL,
                lot_id INTEGER REFERENCES cost_lots(id),
                fund_code TEXT NOT NULL,
                confirmed_date TEXT NOT NULL DEFAULT '',
                units REAL NOT NULL,
                cost_basis REAL NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(exit_trade_id, sequence_no)
            );
            CREATE TABLE IF NOT EXISTS active_cash_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                event_kind TEXT NOT NULL,
                trade_id INTEGER REFERENCES trades(id),
                amount REAL NOT NULL,
                balance_after REAL NOT NULL,
                created_at TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS trade_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL REFERENCES trades(id),
                field_name TEXT NOT NULL,
                old_value TEXT NOT NULL,
                new_value TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trade_corrections_trade
            ON trade_corrections(trade_id,id);
            """
        )
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(ledger_postings)").fetchall()
        }
        if "posting_seq" not in columns:
            conn.execute("ALTER TABLE ledger_postings ADD COLUMN posting_seq INTEGER")
            rows = conn.execute(
                "SELECT trade_id FROM ledger_postings ORDER BY posted_at,trade_id"
            ).fetchall()
            for sequence, row in enumerate(rows, 1):
                conn.execute(
                    "UPDATE ledger_postings SET posting_seq=? WHERE trade_id=?",
                    (sequence, int(row[0])),
                )
        next_sequence = int(conn.execute(
            "SELECT COALESCE(MAX(posting_seq),0) FROM ledger_postings"
        ).fetchone()[0] or 0)
        missing = conn.execute(
            "SELECT trade_id FROM ledger_postings WHERE posting_seq IS NULL ORDER BY posted_at,trade_id"
        ).fetchall()
        for row in missing:
            next_sequence += 1
            conn.execute(
                "UPDATE ledger_postings SET posting_seq=? WHERE trade_id=?",
                (next_sequence, int(row[0])),
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_postings_sequence "
            "ON ledger_postings(posting_seq)"
        )

    @staticmethod
    def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in _CRITICAL_FIELDS:
            value = row.get(key)
            if key in {"amount", "units", "nav", "fee"}:
                value = round(float(value or 0), 10)
            elif key == "counts_toward_active_capital":
                value = 1 if int(value or 0) else 0
            else:
                value = str(value or "")
            result[key] = value
        return result

    @classmethod
    def _snapshots_match(cls, saved: dict[str, Any], current_row: dict[str, Any]) -> bool:
        current = cls._snapshot(current_row)
        for key in _CRITICAL_FIELDS:
            if key not in saved:
                continue
            value = saved.get(key)
            if key in {"amount", "units", "nav", "fee"}:
                value = round(float(value or 0), 10)
            elif key == "counts_toward_active_capital":
                value = 1 if int(value or 0) else 0
            else:
                value = str(value or "")
            if current.get(key) != value:
                return False
        return True

    @staticmethod
    def _holding_rows(conn, fund_code: str) -> list[dict[str, Any]]:
        return [
            dict(row) for row in conn.execute(
                "SELECT * FROM holdings WHERE fund_code=? ORDER BY id", (fund_code,)
            ).fetchall()
        ]

    @staticmethod
    def _active_holding_rows(conn, fund_code: str) -> list[dict[str, Any]]:
        return [
            dict(row) for row in conn.execute(
                "SELECT * FROM holdings WHERE fund_code=? AND active=1 ORDER BY id", (fund_code,)
            ).fetchall()
        ]

    @staticmethod
    def _holding_classification(
        conn, fund_code: str, *, active_only: bool = True,
    ) -> int | None:
        where = " AND active=1" if active_only else ""
        rows = conn.execute(
            "SELECT counts_toward_active_capital FROM holdings WHERE fund_code=?" + where,
            (fund_code,),
        ).fetchall()
        if not rows:
            return None
        values = {1 if int(row[0] or 0) else 0 for row in rows}
        if len(values) != 1:
            raise LedgerError(
                "同一基金存在互相矛盾的主动资金分类；已停止入账，请先导出诊断并统一持仓分类。"
            )
        return values.pop()

    @staticmethod
    def _next_posting_sequence(conn) -> int:
        return int(conn.execute(
            "SELECT COALESCE(MAX(posting_seq),0)+1 FROM ledger_postings"
        ).fetchone()[0] or 1)

    @staticmethod
    def _put_setting(conn, key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def _ensure_cash_balance(self, conn, now: str) -> float:
        row = conn.execute("SELECT value FROM settings WHERE key='active_cash_balance'").fetchone()
        if row is not None:
            try:
                return float(row[0])
            except (TypeError, ValueError) as exc:
                raise LedgerError("主动资金现金余额已损坏，请先从备份恢复或人工复核。") from exc
        limit_row = conn.execute(
            "SELECT value FROM settings WHERE key='active_capital_limit'"
        ).fetchone()
        limit_value = float(limit_row[0]) if limit_row else 8000.0
        invested = float(conn.execute(
            """SELECT COALESCE(SUM(cost),0) FROM holdings
                WHERE active=1 AND COALESCE(counts_toward_active_capital,0)=1"""
        ).fetchone()[0] or 0)
        opening = round(limit_value - invested, 10)
        self._put_setting(conn, "active_cash_balance", opening)
        self._put_setting(conn, "active_cash_initialized_at", now)
        conn.execute(
            """INSERT OR IGNORE INTO active_cash_ledger(
                   event_key,event_kind,trade_id,amount,balance_after,created_at,notes
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                "opening-balance", "OPENING_BALANCE", None, opening, opening, now,
                "由主动资金上限减去迁移时仍在持仓的主动策略成本初始化。",
            ),
        )
        return opening

    def _append_cash_event(
        self, conn, *, event_key: str, event_kind: str, amount: float,
        now: str, trade_id: int | None = None, notes: str = "",
    ) -> float:
        existing = conn.execute(
            "SELECT balance_after FROM active_cash_ledger WHERE event_key=?", (event_key,)
        ).fetchone()
        if existing:
            return float(existing[0])
        balance = self._ensure_cash_balance(conn, now)
        new_balance = round(balance + float(amount), 10)
        conn.execute(
            """INSERT INTO active_cash_ledger(
                   event_key,event_kind,trade_id,amount,balance_after,created_at,notes
               ) VALUES(?,?,?,?,?,?,?)""",
            (event_key, event_kind, trade_id, float(amount), new_balance, now, notes),
        )
        self._put_setting(conn, "active_cash_balance", new_balance)
        return new_balance

    def _write_equity_snapshot_if_available(self, conn, now: str, source: str) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='portfolio_equity_snapshots'"
        ).fetchone()
        if not exists:
            return
        value = float(conn.execute(
            """SELECT COALESCE(SUM(units*latest_nav),0) FROM holdings
                 WHERE active=1 AND COALESCE(counts_toward_active_capital,0)=1"""
        ).fetchone()[0] or 0)
        cash = self._ensure_cash_balance(conn, now)
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(portfolio_equity_snapshots)").fetchall()
        }
        if "external_flow_total" in columns:
            external = float(conn.execute(
                """SELECT COALESCE(SUM(amount),0) FROM active_cash_ledger
                   WHERE event_kind='CAPITAL_LIMIT_ADJUSTMENT'"""
            ).fetchone()[0] or 0)
            conn.execute(
                """INSERT INTO portfolio_equity_snapshots(
                       captured_at,holdings_value,active_cash,total_equity,external_flow_total,source
                   ) VALUES(?,?,?,?,?,?)""",
                (now, value, cash, value + cash, external, source),
            )
        else:
            conn.execute(
                """INSERT INTO portfolio_equity_snapshots(
                       captured_at,holdings_value,active_cash,total_equity,source
                   ) VALUES(?,?,?,?,?)""",
                (now, value, cash, value + cash, source),
            )

    def is_posted(self, trade_id: int) -> bool:
        """Return whether the trade has ever touched the accounting ledger.

        A reversed posting remains an immutable audit record.  Treating it as an
        ordinary editable row would let the user rewrite the evidence after the
        compensating entry had already restored cash and lots.
        """
        with self.db.connect() as conn:
            self._ensure_tables(conn)
            row = conn.execute(
                "SELECT reversed_at FROM ledger_postings WHERE trade_id=?", (int(trade_id),)
            ).fetchone()
            if row:
                return True
            return bool(conn.execute(
                "SELECT 1 FROM cost_lots WHERE trade_id=?", (int(trade_id),)
            ).fetchone())

    def record_equity_snapshot(self, source: str = "正式净值更新") -> None:
        """Persist one active-strategy equity observation for honest drawdown reporting."""
        now = local_now().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            self._ensure_tables(conn)
            self._write_equity_snapshot_if_available(conn, now, str(source or "正式净值更新"))

    def assert_trade_mutation_allowed(
        self, trade_id: int, proposed: dict[str, Any] | None = None,
    ) -> None:
        """Reject edits to accounting fields after posting.

        Callers may pass the complete proposed trade.  Re-saving the exact same
        accounting values is harmless; changing any critical field is blocked.
        """
        with self.db.connect() as conn:
            self._ensure_tables(conn)
            posting = conn.execute(
                "SELECT * FROM ledger_postings WHERE trade_id=?", (int(trade_id),)
            ).fetchone()
            legacy = conn.execute(
                "SELECT 1 FROM cost_lots WHERE trade_id=?", (int(trade_id),)
            ).fetchone()
            if not posting and not legacy:
                return
            if proposed is not None and posting:
                original = json.loads(str(posting["critical_snapshot_json"]))
                if not str(posting["reversed_at"] or "") and self._snapshots_match(original, proposed):
                    return
            raise LedgerError(
                "这笔交易已经进入持仓、成本批次和现金账本，不能直接改金额、份额、净值、日期、基金或状态；"
                "请先执行撤销入账，再新建一笔更正交易。"
            )

    def finalize_trade(
        self, trade_id: int, proposed: dict[str, Any], *, conn=None,
        tables_ready: bool = False,
    ) -> dict[str, Any]:
        """Atomically save a pending trade and, when confirmed, post it.

        The former UI flow committed ``status='已确认'`` before opening a second
        transaction for holdings/cash.  A crash between those commits produced a
        confirmed-looking row that had never reached the ledger.  This method is
        the only supported mutation path for an unposted trade and keeps both
        operations inside one SQLite transaction.
        """
        if conn is None:
            with self.db.connect() as opened:
                self._ensure_tables(opened)
                return self.finalize_trade(
                    trade_id, proposed, conn=opened, tables_ready=True,
                )
        if not tables_ready:
            self._ensure_tables(conn)
        current = conn.execute("SELECT * FROM trades WHERE id=?", (int(trade_id),)).fetchone()
        if not current:
            raise LedgerError("找不到这笔交易。")
        posting = conn.execute(
            "SELECT 1 FROM ledger_postings WHERE trade_id=?", (int(trade_id),)
        ).fetchone()
        legacy_lot = conn.execute(
            "SELECT 1 FROM cost_lots WHERE trade_id=?", (int(trade_id),)
        ).fetchone()
        if posting or legacy_lot:
            raise LedgerError("这笔交易已经进入账本，不能直接修改；请先撤销，再新建更正记录。")

        values = dict(current)
        values.update(proposed or {})
        status = str(values.get("status") or "待确认")
        if status not in {"待确认", POSTED_STATUS, "已撤销"}:
            raise LedgerError("交易状态只能是待确认、已确认或已撤销。")
        confirm_text = str(values.get("confirm_date") or "").strip()
        nav_text = str(values.get("nav_date") or "").strip()
        if status != POSTED_STATUS:
            # A pending/withdrawn record has no factual confirmation date yet.
            # Keeping a guessed date here makes the history look settled before
            # the platform has actually confirmed it.
            values["confirm_date"] = ""
        else:
            if not confirm_text or not nav_text:
                raise LedgerError("正式确认交易必须填写净值日期和支付宝实际确认日。")
            try:
                nav_day = date.fromisoformat(nav_text)
                confirm_day = date.fromisoformat(confirm_text)
            except ValueError as exc:
                raise LedgerError("净值日期和确认日期必须是YYYY-MM-DD。") from exc
            calendar = ChinaTradingCalendar.from_db(self.db)
            if not calendar.is_trading_day(nav_day):
                raise LedgerError(f"{nav_text}不是交易日，不能作为正式净值日期。")
            if not calendar.is_trading_day(confirm_day) or confirm_day < nav_day:
                raise LedgerError("确认日必须是交易日，并且不能早于净值日期。")
            if confirm_day > local_now().date():
                raise LedgerError("确认日不能晚于今天；尚未发生的确认必须继续保持待确认。")
            values["confirm_date"] = confirm_text
        fund_code = str(values.get("fund_code") or "").strip()
        if not conn.execute("SELECT 1 FROM funds WHERE fund_code=?", (fund_code,)).fetchone():
            raise LedgerError("基金不存在；请先在基金库添加或核对这只基金。")
        fields = (
            "trade_date", "confirm_date", "fund_code", "action", "amount", "units",
            "nav", "fee", "nav_date", "status", "rationale",
            "counts_toward_active_capital",
        )
        conn.execute(
            "UPDATE trades SET " + ",".join(f"{field}=?" for field in fields) + " WHERE id=?",
            tuple(values.get(field) for field in fields) + (int(trade_id),),
        )
        if status != POSTED_STATUS:
            return {"posted": False, "updated": True, "reason": "交易尚未确认"}
        return self._post_confirmed_trade_in_conn(
            conn, int(trade_id), as_of=local_now(), user_verified_actual=True,
        )

    def post_confirmed_trade(self, trade_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            self._ensure_tables(conn)
            return self._post_confirmed_trade_in_conn(conn, int(trade_id), as_of=local_now())

    def _post_confirmed_trade_in_conn(
        self, conn, trade_id: int, *, as_of, user_verified_actual: bool = False,
    ) -> dict[str, Any]:
        now = as_of.isoformat(timespec="seconds")
        trade = conn.execute(
            """SELECT t.*,f.fund_name,f.index_code,f.index_name,f.correlation_bucket,f.notes
                 FROM trades t JOIN funds f USING(fund_code) WHERE t.id=?""",
            (int(trade_id),),
        ).fetchone()
        if not trade:
            raise LedgerError("找不到这笔交易。")
        row = dict(trade)
        if str(row.get("status") or "") != POSTED_STATUS:
            return {"posted": False, "reason": "交易尚未确认"}
        action = str(row.get("action") or "")
        if action not in BUY_ACTIONS | EXIT_ACTIONS:
            return {"posted": False, "reason": "该操作不改变基金持仓"}

        posting = conn.execute(
            "SELECT * FROM ledger_postings WHERE trade_id=?", (int(trade_id),)
        ).fetchone()
        if posting:
            if str(posting["reversed_at"] or ""):
                raise LedgerError("这笔交易曾被撤销；请新建更正交易，不能把原交易再次入账。")
            original = json.loads(str(posting["critical_snapshot_json"]))
            if not self._snapshots_match(original, row):
                raise LedgerError("已入账交易的关键字段后来发生变化；为保护账本，已拒绝重复处理。")
            return {"posted": False, "reason": "这笔交易已经入账，未重复处理"}

        amount = float(row.get("amount") or 0)
        units = float(row.get("units") or 0)
        nav = float(row.get("nav") or 0)
        fee = float(row.get("fee") or 0)
        if min(amount, units, nav) <= 0 or fee < 0:
            raise LedgerError("确认交易必须同时具有正数的实际金额、确认份额和正式净值，费用不能为负。")

        requested_counts = row.get("counts_toward_active_capital")
        holding_counts = self._holding_classification(
            conn, str(row["fund_code"]), active_only=True,
        )
        if holding_counts is not None:
            if requested_counts is not None and (1 if int(requested_counts or 0) else 0) != holding_counts:
                raise LedgerError(
                    "交易的主动资金分类与现有持仓不一致；请先在当前持仓中统一分类，"
                    "不能靠本次买卖凭空增加或漏回主动现金。"
                )
            counts = holding_counts
        else:
            counts = 1 if (
                (requested_counts is not None and int(requested_counts or 0))
                or (requested_counts is None and infer_active_capital(row))
            ) else 0
        row["counts_toward_active_capital"] = counts
        before = self._holding_rows(conn, str(row["fund_code"]))
        # Initialize cash before holdings change so old databases obtain the
        # correct opening balance (limit minus existing invested cost).
        if counts:
            self._ensure_cash_balance(conn, now)

        if counts and action in BUY_ACTIONS:
            self._validate_active_buy(
                row, as_of=as_of, exclude_trade_id=int(trade_id),
                user_verified_actual=user_verified_actual,
            )

        if action in BUY_ACTIONS:
            result, metadata = self._post_buy(conn, row, before, now)
            cash_delta = -amount if counts and not result.get("adopted_legacy") else 0.0
            posting_kind = "买入"
        else:
            result, metadata = self._post_exit(conn, row, before, now)
            cash_delta = amount if counts else 0.0
            posting_kind = "卖出"

        metadata["holding_before"] = before
        metadata["cash_delta"] = cash_delta
        metadata["fund_code"] = str(row["fund_code"])
        posting_seq = self._next_posting_sequence(conn)
        conn.execute(
            """INSERT INTO ledger_postings(
                   trade_id,posting_kind,critical_snapshot_json,metadata_json,posted_at,posting_seq
               ) VALUES(?,?,?,?,?,?)""",
            (
                int(trade_id), posting_kind,
                json.dumps(self._snapshot(row), ensure_ascii=False, sort_keys=True),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, posting_seq,
            ),
        )
        if counts and cash_delta:
            self._append_cash_event(
                conn, event_key=f"trade:{int(trade_id)}",
                event_kind="CONFIRMED_BUY" if posting_kind == "买入" else "CONFIRMED_SELL",
                amount=cash_delta, now=now, trade_id=int(trade_id),
                notes="按用户核对后的实际净现金流入账。",
            )
        conn.execute(
            "UPDATE trades SET counts_toward_active_capital=? WHERE id=?", (counts, int(trade_id))
        )
        self._write_equity_snapshot_if_available(conn, now, f"确认{posting_kind}#{int(trade_id)}")
        return {**result, "posted": True, "active_capital": bool(counts)}

    def _validate_active_buy(
        self, row: dict[str, Any], *, as_of, exclude_trade_id: int | None = None,
        user_verified_actual: bool = False,
    ) -> None:
        code = str(row.get("fund_code") or "")
        mapping = self.db.row(
            """SELECT f.investable_index_id FROM investable_funds f
               JOIN investable_indices i ON i.id=f.investable_index_id
               WHERE f.fund_code=? AND f.active=1 AND i.active=1
                 AND f.relation_kind='同一指数'
               ORDER BY f.radar_eligible DESC,f.quality_rank,f.id LIMIT 1""",
            (code,),
        )
        if not mapping:
            raise LedgerError("主动买入基金尚未精确映射到当前中证指数，不能写入真实持仓。")
        catalog = PlatformCatalogService(self.db)
        if user_verified_actual:
            # At order/draft time a fresh same-day public subscription check is
            # mandatory.  Once Alipay has supplied the final amount, units, NAV
            # and confirmation date, that transaction is already a historical
            # accounting fact.  Requiring today's TTL again made a valid Monday
            # order impossible to record on Wednesday.  Preserve the durable
            # strict-C/static mapping gate, but do not pretend today's status can
            # decide whether a past confirmed transaction happened.
            mapped_fund = self.db.row(
                """SELECT * FROM investable_funds
                     WHERE investable_index_id=? AND fund_code=? AND active=1""",
                (int(mapping["investable_index_id"]), code),
            )
            if not mapped_fund or not catalog.fund_has_strict_catalog_eligibility(mapped_fund):
                raise LedgerError(
                    "该基金未通过精确同指数、场外被动C类的长期目录校验，不能写入主动持仓。"
                )
        else:
            try:
                catalog.require_fund_execution(
                    int(mapping["investable_index_id"]), code,
                    intended_amount=float(row.get("amount") or 0),
                    as_of=as_of, require_same_day=True,
                )
            except ValueError as exc:
                raise LedgerError(str(exc)) from exc

        capital = active_capital_summary(self.db, exclude_trade_id=exclude_trade_id)
        amount = float(row.get("amount") or 0)
        if amount > float(capital["cash_available"]) + 0.005:
            raise LedgerError(
                f"该买入需{amount:.2f}元，但扣除其他存活草稿/待确认买单后仅余"
                f"{float(capital['cash_available']):.2f}元主动现金。"
            )
        identity = fund_exposure_identity(self.db, code)
        proposals = [{
            **identity, "name": identity["index_name"], "amount": amount,
            "counts_toward_active_capital": 1,
        }]
        # A user can confirm a manually-created pending trade without first
        # visiting the draft checker.  Include every other live reservation in
        # the same hard gate so pending orders cannot bypass the per-index,
        # correlation-bucket or maximum-theme limits.
        reserved_rows = self.db.rows(
            """SELECT fund_code,planned_amount AS amount,counts_toward_active_capital
                   FROM decision_drafts
                  WHERE status='草稿' AND action IN ('买入','定投','转换转入')
                 UNION ALL
                 SELECT fund_code,amount,counts_toward_active_capital
                   FROM trades
                  WHERE status='待确认' AND action IN ('买入','定投','转换转入')
                    AND (? IS NULL OR id<>?)""",
            (exclude_trade_id, exclude_trade_id),
        )
        for reserved in reserved_rows:
            if not int(reserved.get("counts_toward_active_capital") or 0):
                continue
            reserved_identity = fund_exposure_identity(
                self.db, str(reserved.get("fund_code") or "")
            )
            proposals.append({
                **reserved_identity,
                "name": reserved_identity["index_name"],
                "amount": max(0.0, float(reserved.get("amount") or 0)),
                "counts_toward_active_capital": 1,
            })
        current = [
            {
                **item, "name": item["index_name"],
                "capital_amount": float(item.get("cost") or 0),
                "counts_toward_active_capital": 1,
            }
            for item in active_index_exposures(self.db).values()
        ]
        risk = validate_allocation(proposals, current, self.db.settings())
        if risk.decision != "通过":
            raise LedgerError("主动资金风控未通过：" + "；".join(risk.reasons))

    def _post_buy(
        self, conn, row: dict[str, Any], before: list[dict[str, Any]], now: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        trade_id = int(row["id"])
        existing_lot = conn.execute(
            "SELECT id FROM cost_lots WHERE trade_id=?", (trade_id,)
        ).fetchone()
        if existing_lot:
            # Older versions posted the lot but had no posting marker.  Adopt it
            # without touching holdings or cash a second time.
            holding = conn.execute(
                "SELECT id FROM holdings WHERE fund_code=? AND active=1 ORDER BY id LIMIT 1",
                (row["fund_code"],),
            ).fetchone()
            return (
                {"holding_id": int(holding[0]) if holding else None, "adopted_legacy": True},
                {"lot_id": int(existing_lot[0]), "adopted_legacy": True},
            )

        active = self._active_holding_rows(conn, str(row["fund_code"]))
        counts = int(row["counts_toward_active_capital"])
        confirmed = str(row.get("confirm_date") or row.get("trade_date") or "")
        pricing = str(row.get("nav_date") or row.get("trade_date") or "")
        if active:
            canonical = active[0]
            total_units = sum(float(item["units"] or 0) for item in active) + float(row["units"])
            total_cost = sum(float(item["cost"] or 0) for item in active) + float(row["amount"])
            earliest = min([confirmed, *[str(item["confirmed_date"]) for item in active if item["confirmed_date"]]])
            conn.execute(
                """UPDATE holdings SET units=?,cost=?,latest_nav=?,nav_date=?,confirmed_date=?,
                          counts_toward_active_capital=?,active=1 WHERE id=?""",
                (
                    total_units, total_cost, float(row["nav"]), pricing, earliest,
                    counts, int(canonical["id"]),
                ),
            )
            for duplicate in active[1:]:
                conn.execute(
                    "UPDATE holdings SET units=0,cost=0,active=0 WHERE id=?", (int(duplicate["id"]),)
                )
            holding_id = int(canonical["id"])
        else:
            sleeve = "主动" if counts else ("定投" if row["action"] == "定投" else "其他")
            cursor = conn.execute(
                """INSERT INTO holdings(
                       fund_code,confirmed_date,units,cost,latest_nav,nav_date,sleeve,thesis,active,
                       counts_toward_active_capital
                   ) VALUES(?,?,?,?,?,?,?,?,1,?)""",
                (
                    row["fund_code"], confirmed, float(row["units"]), float(row["amount"]),
                    float(row["nav"]), pricing, sleeve,
                    f"由已确认交易#{int(row['id'])}自动建立", counts,
                ),
            )
            holding_id = int(cursor.lastrowid)
        cursor = conn.execute(
            """INSERT INTO cost_lots(
                   fund_code,trade_id,occurrence_id,confirmed_date,pricing_date,units,remaining_units,
                   gross_amount,fee,net_invested,nav,source,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["fund_code"], int(row["id"]), row.get("recurring_occurrence_id"),
                confirmed, pricing, float(row["units"]),
                float(row["units"]), float(row["amount"]), float(row.get("fee") or 0),
                max(0.0, float(row["amount"]) - float(row.get("fee") or 0)),
                float(row["nav"]), str(row.get("source") or "已确认交易自动入账"), now,
            ),
        )
        return {"holding_id": holding_id}, {"lot_id": int(cursor.lastrowid)}

    def _post_exit(
        self, conn, row: dict[str, Any], before: list[dict[str, Any]], now: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        active = [item for item in before if int(item.get("active") or 0)]
        if not active:
            raise LedgerError("当前没有这只基金的有效持仓，不能确认卖出或清仓。")
        holding_units = sum(float(item.get("units") or 0) for item in active)
        holding_cost = sum(float(item.get("cost") or 0) for item in active)
        sold_units = float(row["units"])
        tolerance = max(1e-6, holding_units * 1e-7)
        if sold_units > holding_units + tolerance:
            raise LedgerError(f"确认卖出份额{sold_units:.4f}超过当前持仓{holding_units:.4f}。")
        if str(row["action"]) == "清仓" and abs(sold_units - holding_units) > tolerance:
            raise LedgerError("清仓必须填写平台最终确认的全部份额；部分份额请使用“卖出”。")

        lots = [
            dict(item) for item in conn.execute(
                """SELECT * FROM cost_lots WHERE fund_code=? AND remaining_units>0
                   ORDER BY confirmed_date,id""",
                (row["fund_code"],),
            ).fetchall()
        ]
        tracked_units = sum(float(item["remaining_units"] or 0) for item in lots)
        tracked_cost = sum(
            float(item["gross_amount"] or 0)
            * float(item["remaining_units"] or 0) / max(float(item["units"] or 0), 1e-12)
            for item in lots
        )
        if tracked_units > holding_units + max(0.01, tolerance):
            raise LedgerError("成本批次份额大于当前持仓，账本已有冲突；请先导出诊断，不要继续卖出入账。")
        legacy_units = max(0.0, holding_units - tracked_units)
        legacy_cost = max(0.0, holding_cost - tracked_cost)
        components: list[dict[str, Any]] = []
        if legacy_units > tolerance:
            components.append({
                "lot_id": None,
                "confirmed_date": min(str(item["confirmed_date"]) for item in active),
                "remaining_units": legacy_units,
                "unit_cost": legacy_cost / legacy_units if legacy_units else 0.0,
            })
        for lot in lots:
            components.append({
                "lot_id": int(lot["id"]),
                "confirmed_date": str(lot["confirmed_date"]),
                "remaining_units": float(lot["remaining_units"]),
                "unit_cost": float(lot["gross_amount"] or 0) / max(float(lot["units"] or 0), 1e-12),
            })
        components.sort(key=lambda item: (item["confirmed_date"], item["lot_id"] is not None, item["lot_id"] or 0))

        left = sold_units
        consumed_cost = 0.0
        sequence = 0
        for component in components:
            if left <= tolerance:
                break
            take = min(left, float(component["remaining_units"]))
            if take <= tolerance:
                continue
            cost_basis = take * float(component["unit_cost"])
            sequence += 1
            conn.execute(
                """INSERT INTO lot_consumptions(
                       exit_trade_id,sequence_no,lot_id,fund_code,confirmed_date,units,cost_basis,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    int(row["id"]), sequence, component["lot_id"], row["fund_code"],
                    component["confirmed_date"], take, cost_basis, now,
                ),
            )
            if component["lot_id"] is not None:
                conn.execute(
                    "UPDATE cost_lots SET remaining_units=MAX(0,remaining_units-?) WHERE id=?",
                    (take, int(component["lot_id"])),
                )
            consumed_cost += cost_basis
            left -= take
        if left > tolerance:
            raise LedgerError("成本批次和历史持仓不足以覆盖本次卖出份额，已取消整笔入账。")

        residual_units = max(0.0, holding_units - sold_units)
        residual_cost = max(0.0, holding_cost - consumed_cost)
        canonical = active[0]
        still_active = 1 if residual_units > tolerance else 0
        conn.execute(
            """UPDATE holdings SET units=?,cost=?,latest_nav=?,nav_date=?,active=? WHERE id=?""",
            (
                residual_units, residual_cost, float(row["nav"]),
                str(row.get("nav_date") or row.get("trade_date") or ""), still_active,
                int(canonical["id"]),
            ),
        )
        for duplicate in active[1:]:
            conn.execute(
                "UPDATE holdings SET units=0,cost=0,active=0 WHERE id=?", (int(duplicate["id"]),)
            )
        realized = float(row["amount"]) - consumed_cost
        conn.execute(
            "UPDATE trades SET result_pnl=?,result_pnl_status='已核算' WHERE id=?",
            (realized, int(row["id"])),
        )
        return (
            {
                "holding_id": int(canonical["id"]), "sold_units": sold_units,
                "cost_basis": round(consumed_cost, 10), "realized_pnl": round(realized, 10),
            },
            {
                "holding_id": int(canonical["id"]), "consumed_cost": consumed_cost,
                "sold_units": sold_units, "result_pnl_before": row.get("result_pnl"),
                "result_pnl_status_before": row.get("result_pnl_status"),
            },
        )

    def reverse_posting(self, trade_id: int, reason: str) -> dict[str, Any]:
        """Reverse a posted transaction and restore its pre-posting holdings.

        A reversal is blocked when a later posted transaction for the same fund
        exists; reverse those later transactions first.  The original row stays
        visible and is marked revoked for a complete audit trail.
        """
        reason = str(reason or "").strip()
        if not reason:
            raise LedgerError("撤销入账必须填写原因。")
        now = local_now().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            self._ensure_tables(conn)
            posting = conn.execute(
                """SELECT p.*,t.fund_code FROM ledger_postings p
                   JOIN trades t ON t.id=p.trade_id WHERE p.trade_id=?""",
                (int(trade_id),),
            ).fetchone()
            if not posting:
                raise LedgerError("这笔交易尚未进入统一账本，无需撤销。")
            if str(posting["reversed_at"] or ""):
                return {"reversed": False, "reason": "这笔交易已经撤销"}
            later = conn.execute(
                """SELECT p.trade_id FROM ledger_postings p JOIN trades t ON t.id=p.trade_id
                    WHERE t.fund_code=? AND p.reversed_at='' AND p.posting_seq>? AND p.trade_id<>?
                    ORDER BY p.posting_seq DESC LIMIT 1""",
                (posting["fund_code"], int(posting["posting_seq"]), int(trade_id)),
            ).fetchone()
            if later:
                raise LedgerError(f"该基金后来还有已入账交易#{int(later[0])}；请按时间倒序先撤销后续交易。")

            metadata = json.loads(str(posting["metadata_json"] or "{}"))
            if metadata.get("adopted_legacy") or metadata.get("migration"):
                raise LedgerError("这笔交易由旧版账本接管，缺少入账前快照，不能自动撤销；请导出诊断后人工更正。")
            if str(posting["posting_kind"]) == "买入":
                lot = conn.execute(
                    "SELECT * FROM cost_lots WHERE trade_id=?", (int(trade_id),)
                ).fetchone()
                if lot and abs(float(lot["remaining_units"]) - float(lot["units"])) > 1e-7:
                    raise LedgerError("这笔买入的份额已有部分卖出，必须先撤销后续卖出交易。")
                conn.execute("DELETE FROM cost_lots WHERE trade_id=?", (int(trade_id),))
            else:
                consumptions = conn.execute(
                    "SELECT * FROM lot_consumptions WHERE exit_trade_id=? ORDER BY sequence_no",
                    (int(trade_id),),
                ).fetchall()
                for item in consumptions:
                    if item["lot_id"] is not None:
                        conn.execute(
                            "UPDATE cost_lots SET remaining_units=remaining_units+? WHERE id=?",
                            (float(item["units"]), int(item["lot_id"])),
                        )
                conn.execute("DELETE FROM lot_consumptions WHERE exit_trade_id=?", (int(trade_id),))
                conn.execute(
                    "UPDATE trades SET result_pnl=?,result_pnl_status=? WHERE id=?",
                    (
                        metadata.get("result_pnl_before"),
                        metadata.get("result_pnl_status_before") or "待核算",
                        int(trade_id),
                    ),
                )

            before = list(metadata.get("holding_before") or [])
            before_ids = {int(item["id"]) for item in before}
            current = self._holding_rows(conn, str(posting["fund_code"]))
            meaningful = [item for item in current if int(item.get("active") or 0)]
            if not meaningful:
                meaningful = [
                    item for item in current
                    if abs(float(item.get("units") or 0)) > 1e-9
                    or abs(float(item.get("cost") or 0)) > 1e-7
                ]
            if not meaningful and current:
                meaningful = [current[-1]]
            current_counts: int | None = None
            current_profile: dict[str, Any] | None = meaningful[0] if meaningful else None
            if meaningful:
                classifications = {
                    1 if int(item.get("counts_toward_active_capital") or 0) else 0
                    for item in meaningful
                }
                if len(classifications) != 1:
                    raise LedgerError(
                        "该基金当前持仓的主动资金分类互相矛盾；为避免现金失真，已拒绝撤销。"
                    )
                current_counts = classifications.pop()
            for item in current:
                if int(item["id"]) not in before_ids:
                    conn.execute("DELETE FROM holdings WHERE id=?", (int(item["id"]),))
            for item in before:
                restored = dict(item)
                # Classification is a later user decision, not a property of the
                # old trade snapshot.  Preserve it while undoing only this trade;
                # otherwise reclassify-out then reverse-buy would add the same
                # cash twice (the historic 8,000 -> 10,000 defect).
                if current_counts is not None:
                    restored["counts_toward_active_capital"] = current_counts
                if current_profile is not None:
                    for profile_key in ("sleeve", "thesis"):
                        if profile_key in restored and profile_key in current_profile:
                            restored[profile_key] = current_profile[profile_key]
                columns = [key for key in restored.keys()]
                exists = conn.execute("SELECT 1 FROM holdings WHERE id=?", (int(item["id"]),)).fetchone()
                if exists:
                    update_columns = [key for key in columns if key != "id"]
                    conn.execute(
                        "UPDATE holdings SET " + ",".join(f"{key}=?" for key in update_columns) + " WHERE id=?",
                        tuple(restored[key] for key in update_columns) + (int(item["id"]),),
                    )
                else:
                    conn.execute(
                        "INSERT INTO holdings(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                        tuple(restored[key] for key in columns),
                    )

            critical = json.loads(str(posting["critical_snapshot_json"] or "{}"))
            trade_amount = float(critical.get("amount") or 0)
            effective_counts = current_counts
            if effective_counts is None:
                effective_counts = 1 if int(critical.get("counts_toward_active_capital") or 0) else 0
            reversal_cash_delta = 0.0
            if effective_counts:
                reversal_cash_delta = (
                    trade_amount if str(posting["posting_kind"]) == "买入" else -trade_amount
                )
            if reversal_cash_delta:
                self._append_cash_event(
                    conn, event_key=f"reversal:{int(trade_id)}", event_kind="POSTING_REVERSAL",
                    amount=reversal_cash_delta, now=now, trade_id=int(trade_id), notes=reason,
                )
            conn.execute(
                "UPDATE ledger_postings SET reversed_at=?,reversal_reason=? WHERE trade_id=?",
                (now, reason, int(trade_id)),
            )
            conn.execute("UPDATE trades SET status='已撤销' WHERE id=?", (int(trade_id),))
            recurring_occurrence_id = critical.get("recurring_occurrence_id")
            if recurring_occurrence_id in (None, ""):
                trade_row = conn.execute(
                    "SELECT recurring_occurrence_id FROM trades WHERE id=?", (int(trade_id),)
                ).fetchone()
                recurring_occurrence_id = trade_row[0] if trade_row else None
            if recurring_occurrence_id not in (None, ""):
                conn.execute(
                    """UPDATE recurring_occurrences
                          SET status='已跳过',notes=CASE
                                WHEN TRIM(COALESCE(notes,''))='' THEN ?
                                ELSE notes || '；' || ? END,
                              updated_at=?
                        WHERE id=?""",
                    (
                        f"对应交易#{int(trade_id)}已撤销：{reason}",
                        f"对应交易#{int(trade_id)}已撤销：{reason}",
                        now, int(recurring_occurrence_id),
                    ),
                )
            self._write_equity_snapshot_if_available(conn, now, f"撤销交易#{int(trade_id)}")
            return {"reversed": True, "trade_id": int(trade_id)}

    def correct_posted_buy_fee(
        self, trade_id: int, new_fee: float, reason: str,
    ) -> dict[str, Any]:
        """Correct one posted buy fee without rewriting the rest of the trade.

        ``amount`` is the confirmed cash outflow, so changing a purchase fee
        does not change holding units, holding cost or active cash.  It does
        change the fee fact and the lot's net-invested split.  This narrow path
        updates those representations atomically and records the old and new
        values in ``trade_corrections``.
        """
        reason = str(reason or "").strip()
        if not reason:
            raise LedgerError("更正已入账费用必须填写原因。")
        try:
            corrected = round(float(new_fee), 10)
        except (TypeError, ValueError) as exc:
            raise LedgerError("更正后的费用必须是数字。") from exc
        if corrected < 0:
            raise LedgerError("更正后的费用不能小于0。")

        now = local_now().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            self._ensure_tables(conn)
            trade = conn.execute(
                "SELECT * FROM trades WHERE id=?", (int(trade_id),)
            ).fetchone()
            posting = conn.execute(
                "SELECT * FROM ledger_postings WHERE trade_id=?", (int(trade_id),)
            ).fetchone()
            if not trade or not posting:
                raise LedgerError("这笔交易尚未进入统一账本，直接在确认表单中修改即可。")
            if str(posting["reversed_at"] or ""):
                raise LedgerError("已撤销交易只能作为审计记录保留，不能再更正费用。")
            row = dict(trade)
            if str(row.get("action") or "") not in BUY_ACTIONS or str(posting["posting_kind"]) != "买入":
                raise LedgerError("当前安全更正入口只处理已入账买入、定投或转换转入费用。")
            amount = float(row.get("amount") or 0)
            if corrected > amount + 1e-9:
                raise LedgerError("买入费用不能超过这笔交易的实际总金额。")
            old_fee = round(float(row.get("fee") or 0), 10)
            if abs(old_fee - corrected) <= 1e-10:
                return {
                    "changed": False, "trade_id": int(trade_id),
                    "old_fee": old_fee, "new_fee": corrected,
                }

            saved = json.loads(str(posting["critical_snapshot_json"] or "{}"))
            if not self._snapshots_match(saved, row):
                raise LedgerError("交易与账本快照已经不一致；为避免扩大错误，本次更正已停止。")
            lots = conn.execute(
                "SELECT * FROM cost_lots WHERE trade_id=? ORDER BY id", (int(trade_id),)
            ).fetchall()
            if len(lots) != 1:
                raise LedgerError("该买入的成本批次数量异常，不能只改费用；请导出诊断后复核。")
            lot = lots[0]
            gross = float(lot["gross_amount"] or 0)
            if corrected > gross + 1e-9:
                raise LedgerError("买入费用不能超过成本批次的实际总金额。")

            conn.execute("UPDATE trades SET fee=? WHERE id=?", (corrected, int(trade_id)))
            conn.execute(
                "UPDATE cost_lots SET fee=?,net_invested=? WHERE id=?",
                (corrected, round(gross - corrected, 10), int(lot["id"])),
            )
            corrected_row = {**row, "fee": corrected}
            conn.execute(
                "UPDATE ledger_postings SET critical_snapshot_json=? WHERE trade_id=?",
                (
                    json.dumps(self._snapshot(corrected_row), ensure_ascii=False, sort_keys=True),
                    int(trade_id),
                ),
            )
            occurrence_id = row.get("recurring_occurrence_id")
            if occurrence_id not in (None, ""):
                conn.execute(
                    "UPDATE recurring_occurrences SET fee=?,updated_at=? WHERE id=?",
                    (corrected, now, int(occurrence_id)),
                )
            conn.execute(
                """INSERT INTO trade_corrections(
                       trade_id,field_name,old_value,new_value,reason,created_at
                   ) VALUES(?,'fee',?,?,?,?)""",
                (int(trade_id), f"{old_fee:.10g}", f"{corrected:.10g}", reason, now),
            )
            return {
                "changed": True, "trade_id": int(trade_id),
                "old_fee": old_fee, "new_fee": corrected,
                "lot_id": int(lot["id"]),
            }

    def adjust_active_capital_limit(self, new_limit: float, reason: str = "用户修改主动资金上限") -> dict[str, float]:
        """Change the capital limit and cash balance by the same audited delta."""
        try:
            new_limit = float(new_limit)
        except (TypeError, ValueError) as exc:
            raise LedgerError("主动资金上限必须是数字。") from exc
        if new_limit < 0:
            raise LedgerError("主动资金上限不能为负数。")
        now = local_now().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            self._ensure_tables(conn)
            old_row = conn.execute(
                "SELECT value FROM settings WHERE key='active_capital_limit'"
            ).fetchone()
            old_limit = float(old_row[0]) if old_row else 8000.0
            balance = self._ensure_cash_balance(conn, now)
            delta = round(new_limit - old_limit, 10)
            if abs(delta) <= 1e-10:
                return {"old_limit": old_limit, "new_limit": new_limit, "cash_balance": balance}
            self._put_setting(conn, "active_capital_limit", new_limit)
            balance = self._append_cash_event(
                conn, event_key=f"limit:{now}:{uuid4().hex}", event_kind="CAPITAL_LIMIT_ADJUSTMENT",
                amount=delta, now=now, notes=str(reason or "用户修改主动资金上限"),
            )
            self._write_equity_snapshot_if_available(conn, now, "调整主动资金上限")
            return {"old_limit": old_limit, "new_limit": new_limit, "cash_balance": balance}

    def reclassify_holding(
        self, holding_id: int, counts_toward_active_capital: int,
        reason: str = "用户修改持仓资金分类",
    ) -> dict[str, Any]:
        """Move an open holding into/out of the tactical pool with a cash audit event."""
        new_counts = 1 if int(counts_toward_active_capital or 0) else 0
        now = local_now().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            self._ensure_tables(conn)
            row = conn.execute(
                "SELECT * FROM holdings WHERE id=? AND active=1", (int(holding_id),)
            ).fetchone()
            if not row:
                raise LedgerError("找不到这笔有效持仓。")
            fund_code = str(row["fund_code"])
            active_rows = self._active_holding_rows(conn, fund_code)
            classifications = {
                1 if int(item.get("counts_toward_active_capital") or 0) else 0
                for item in active_rows
            }
            if len(classifications) != 1:
                raise LedgerError(
                    "同一基金存在互相矛盾的资金分类；请先导出诊断，软件不会只修改其中一行。"
                )
            old_counts = classifications.pop()
            if old_counts == new_counts:
                return {"changed": False, "old": old_counts, "new": new_counts}
            self._ensure_cash_balance(conn, now)
            cost = sum(max(0.0, float(item.get("cost") or 0)) for item in active_rows)
            cash_delta = -cost if new_counts else cost
            conn.execute(
                "UPDATE holdings SET counts_toward_active_capital=? WHERE fund_code=? AND active=1",
                (new_counts, fund_code),
            )
            self._append_cash_event(
                conn,
                event_key=f"holding-class:{int(holding_id)}:{now}:{uuid4().hex}",
                event_kind="HOLDING_RECLASSIFICATION",
                amount=cash_delta,
                now=now,
                notes=str(reason or "用户修改持仓资金分类"),
            )
            self._write_equity_snapshot_if_available(conn, now, f"调整持仓#{int(holding_id)}资金分类")
            return {
                "changed": True, "old": old_counts, "new": new_counts,
                "cash_delta": cash_delta,
            }
