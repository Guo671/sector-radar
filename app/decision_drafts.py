from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from .config import local_now, local_today
from .engine import RiskResult, validate_allocation
from .portfolio import (
    active_capital_summary,
    active_index_exposures,
    allocate_candidate_amounts,
    fund_exposure_identity,
    infer_active_capital,
)
from .platform_catalog import PlatformCatalogService
from .recurring import RecurringInvestmentService
from .trading_calendar import ChinaTradingCalendar, infer_fund_kind


class DecisionDraftError(RuntimeError):
    pass


class DecisionDraftService:
    """Keep AI advice, intraday intentions, and posted trade records separate."""

    ACTIONS = {"买入", "卖出", "清仓", "转换", "其他"}

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _day(as_of: datetime | None = None) -> str:
        return (as_of.date() if as_of else local_today()).isoformat()

    def ensure_today_recurring(self, as_of: datetime | None = None) -> int:
        day = self._day(as_of)
        RecurringInvestmentService(self.db).generate_due(
            through=as_of.date() if as_of else local_today()
        )
        occurrences = self.db.rows(
            """SELECT o.*,f.fund_name,f.index_code,f.index_name,f.correlation_bucket,f.notes
               FROM recurring_occurrences o
               JOIN funds f USING(fund_code)
               WHERE o.scheduled_date=? AND o.status NOT IN ('已入账','已跳过')
               ORDER BY o.id""",
            (day,),
        )
        now = (as_of or local_now()).isoformat(timespec="seconds")
        calendar = ChinaTradingCalendar.from_db(self.db)
        pricing_day = calendar.expected_pricing_date(date.fromisoformat(day)).isoformat()
        created = 0
        with self.db.connect() as conn:
            for row in occurrences:
                counts = 1 if infer_active_capital(row) else 0
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO decision_drafts(
                           trade_date,source_kind,source_key,recurring_occurrence_id,fund_code,fund_name,
                           action,planned_amount,rationale,status,created_at,updated_at,pricing_date,
                           settlement_status,counts_toward_active_capital
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        day, "自动定投", f"recurring:{row['id']}", row["id"], row["fund_code"],
                        row["fund_name"], "定投", float(row["amount"]),
                        "定投计划自动加入草稿；收盘入账后仍须等待正式净值并核对支付宝扣款。",
                        "草稿", now, now, pricing_day, "待联网补全", counts,
                    ),
                )
                created += max(0, cursor.rowcount)
        return created

    def list_for_day(self, day: str | None = None) -> list[dict[str, Any]]:
        day = day or local_today().isoformat()
        return self.db.rows(
            "SELECT * FROM decision_drafts WHERE trade_date=? ORDER BY status='草稿' DESC,id",
            (day,),
        )

    def list_stale_drafts(self, before_day: str | None = None) -> list[dict[str, Any]]:
        """Return unresolved drafts from earlier days for an explicit user choice.

        They are never silently treated as today's operation.  Until the user
        carries or abandons them they remain visible and continue to reserve
        tactical cash, which prevents an unnoticed old intention from causing
        an over-allocation.
        """
        before_day = before_day or local_today().isoformat()
        return self.db.rows(
            """SELECT * FROM decision_drafts
                 WHERE trade_date<? AND status='草稿'
                 ORDER BY trade_date,id""",
            (before_day,),
        )

    def carry_stale_draft(
        self, draft_id: int, *, as_of: datetime | None = None,
    ) -> int:
        """Copy one old intention into today after an explicit user decision."""
        now_dt = as_of or local_now()
        day = self._day(now_dt)
        now = now_dt.isoformat(timespec="seconds")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM decision_drafts WHERE id=?", (int(draft_id),)
            ).fetchone()
            if not row or str(row["status"] or "") != "草稿":
                raise DecisionDraftError("这条旧草稿已经处理，不能重复结转。")
            if str(row["trade_date"] or "") >= day:
                raise DecisionDraftError("这不是跨日草稿，无需结转。")
            if row["recurring_occurrence_id"]:
                raise DecisionDraftError(
                    "旧定投草稿不能顺延冒充今天扣款；若当天没有实际扣款，请选择放弃。"
                )
            pricing_day = self._default_pricing_date(str(row["fund_code"] or ""), now_dt)
            cursor = conn.execute(
                """INSERT INTO decision_drafts(
                       trade_date,source_kind,source_key,fund_code,fund_name,action,
                       planned_amount,planned_units,planned_pct,rationale,ai_advice,status,
                       created_at,updated_at,pricing_date,settlement_status,
                       counts_toward_active_capital,result_note
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    day, f"用户结转·{row['source_kind']}", f"carry:{int(draft_id)}:{uuid4().hex}",
                    row["fund_code"], row["fund_name"], row["action"], row["planned_amount"],
                    0.0, 0.0, row["rationale"], row["ai_advice"], "草稿", now, now, pricing_day,
                    "待联网补全", int(row["counts_toward_active_capital"] or 0),
                    f"由{row['trade_date']}旧草稿经用户确认结转；金额和操作仍可编辑。",
                ),
            )
            new_id = int(cursor.lastrowid)
            conn.execute(
                """UPDATE decision_drafts SET status='已结转',result_note=?,posted_at=?,updated_at=?
                     WHERE id=? AND status='草稿'""",
                (f"用户已明确结转为今日草稿#{new_id}。", now, now, int(draft_id)),
            )
            return new_id

    @staticmethod
    def _matching_unposted_recurring_trades(conn, draft, occurrence) -> list[dict[str, Any]]:
        """Return only reversible pending trades that may duplicate this deduction."""
        rows = conn.execute(
            """SELECT t.* FROM trades t
                 WHERE t.fund_code=? AND t.trade_date=?
                   AND ABS(t.amount-?)<0.005
                   AND t.action IN ('买入','定投') AND t.status='待确认'
                   AND t.recurring_occurrence_id IS NULL
                   AND NOT EXISTS(SELECT 1 FROM cost_lots c WHERE c.trade_id=t.id)
                   AND NOT EXISTS(SELECT 1 FROM ledger_postings p WHERE p.trade_id=t.id)
                 ORDER BY t.id""",
            (
                str(draft["fund_code"] or ""),
                str(draft["trade_date"] or ""),
                float(occurrence["amount"] or 0),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def preview_stale_recurring_deduction(
        self, draft_id: int, *, as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Describe the exact old deduction and any safe duplicate candidate."""
        day = self._day(as_of)
        with self.db.connect() as conn:
            draft = conn.execute(
                "SELECT * FROM decision_drafts WHERE id=?", (int(draft_id),)
            ).fetchone()
            if not draft or str(draft["status"] or "") != "草稿":
                raise DecisionDraftError("这条旧草稿已经处理，不能重复确认扣款。")
            if str(draft["trade_date"] or "") >= day:
                raise DecisionDraftError("这不是跨日草稿；今天的定投请在收盘流程处理。")
            occurrence_id = draft["recurring_occurrence_id"]
            if not occurrence_id:
                raise DecisionDraftError("只有自动定投旧草稿才能确认原交易日扣款。")
            occurrence = conn.execute(
                "SELECT * FROM recurring_occurrences WHERE id=?", (int(occurrence_id),)
            ).fetchone()
            if not occurrence:
                raise DecisionDraftError("对应的定投流水不存在，已停止处理。")
            if str(occurrence["status"] or "") in {"已入账", "已跳过"}:
                raise DecisionDraftError(f"这笔定投流水已经是“{occurrence['status']}”，不能再次确认扣款。")
            linked = conn.execute(
                """SELECT * FROM trades WHERE recurring_occurrence_id=?
                     ORDER BY id""",
                (int(occurrence_id),),
            ).fetchall()
            if len(linked) > 1:
                raise DecisionDraftError("同一定投流水关联了多笔交易，请先导出诊断，软件不会猜哪笔正确。")
            candidates = self._matching_unposted_recurring_trades(conn, draft, occurrence)
            return {
                "draft": dict(draft),
                "occurrence": dict(occurrence),
                "linked_trade": dict(linked[0]) if linked else None,
                "matching_trades": candidates,
            }

    def acknowledge_stale_recurring_deduction(
        self, draft_id: int, *, link_trade_id: int | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Record an actual old deduction without pretending settlement is final.

        The original date and amount are preserved.  Public NAV and calculated
        units may be copied into the *estimated* settlement fields, but no
        holding, cost lot, cash posting or confirmed transaction is created.
        """
        now_dt = as_of or local_now()
        day = self._day(now_dt)
        now = now_dt.isoformat(timespec="seconds")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            draft = conn.execute(
                "SELECT * FROM decision_drafts WHERE id=?", (int(draft_id),)
            ).fetchone()
            if not draft:
                raise DecisionDraftError("旧草稿不存在。")
            occurrence_id = draft["recurring_occurrence_id"]
            occurrence = conn.execute(
                "SELECT * FROM recurring_occurrences WHERE id=?", (int(occurrence_id),)
            ).fetchone() if occurrence_id else None
            if (
                str(draft["status"] or "") == "等待净值确认"
                and occurrence is not None
                and int(occurrence["deduction_confirmed"] or 0) == 1
            ):
                return {
                    "draft_id": int(draft["id"]),
                    "occurrence_id": int(occurrence["id"]),
                    "linked_trade_id": int(draft["trade_id"]) if draft["trade_id"] else None,
                    "already_confirmed": True,
                    "nav": float(occurrence["nav"] or 0),
                    "units": float(occurrence["units"] or 0),
                    "settlement_status": str(draft["settlement_status"] or ""),
                }
            if str(draft["status"] or "") != "草稿":
                raise DecisionDraftError("这条旧草稿已经处理，不能重复确认扣款。")
            if str(draft["trade_date"] or "") >= day:
                raise DecisionDraftError("这不是跨日草稿；今天的定投请在收盘流程处理。")
            if occurrence is None:
                raise DecisionDraftError("只有存在定投流水的旧草稿才能确认原交易日扣款。")
            if str(occurrence["status"] or "") in {"已入账", "已跳过"}:
                raise DecisionDraftError(f"这笔定投流水已经是“{occurrence['status']}”，不能再次确认扣款。")
            if str(occurrence["fund_code"] or "") != str(draft["fund_code"] or ""):
                raise DecisionDraftError("草稿基金与定投流水不一致，已停止处理。")
            if str(occurrence["scheduled_date"] or "") != str(draft["trade_date"] or ""):
                raise DecisionDraftError("草稿日期与定投计划日不一致，已停止处理。")
            if abs(float(occurrence["amount"] or 0) - float(draft["planned_amount"] or 0)) >= 0.005:
                raise DecisionDraftError("草稿金额与定投流水金额不一致，请先核对，软件不会自动改写。")

            linked_rows = conn.execute(
                "SELECT * FROM trades WHERE recurring_occurrence_id=? ORDER BY id",
                (int(occurrence["id"]),),
            ).fetchall()
            if len(linked_rows) > 1:
                raise DecisionDraftError("同一定投流水关联了多笔交易，请先导出诊断。")
            linked_trade = linked_rows[0] if linked_rows else None
            candidates = self._matching_unposted_recurring_trades(conn, draft, occurrence)
            if linked_trade is not None:
                linked_trade_id_value = int(linked_trade["id"])
                linked_is_safe = (
                    str(linked_trade["status"] or "") == "待确认"
                    and str(linked_trade["action"] or "") in {"买入", "定投"}
                    and str(linked_trade["fund_code"] or "") == str(draft["fund_code"] or "")
                    and str(linked_trade["trade_date"] or "") == str(draft["trade_date"] or "")
                    and abs(
                        float(linked_trade["amount"] or 0)
                        - float(occurrence["amount"] or 0)
                    ) < 0.005
                    and conn.execute(
                        "SELECT 1 FROM cost_lots WHERE trade_id=?", (linked_trade_id_value,)
                    ).fetchone() is None
                    and conn.execute(
                        "SELECT 1 FROM ledger_postings WHERE trade_id=?", (linked_trade_id_value,)
                    ).fetchone() is None
                )
                if not linked_is_safe:
                    raise DecisionDraftError(
                        f"该定投流水已关联交易#{linked_trade_id_value}，但它不是可安全修改的待确认买入；"
                        "软件不会覆盖已过账或不一致的交易，请先导出诊断。"
                    )
            if linked_trade is not None and link_trade_id not in (None, int(linked_trade["id"])):
                raise DecisionDraftError(f"该流水已经关联交易#{int(linked_trade['id'])}，不能改连另一笔。")
            if linked_trade is None and candidates:
                candidate_ids = [int(row["id"]) for row in candidates]
                if link_trade_id is None:
                    raise DecisionDraftError(
                        "发现同日同基金同金额的待确认交易："
                        + "、".join(f"#{value}" for value in candidate_ids)
                        + "。为避免重复，必须由用户明确选择是否合并。"
                    )
                if int(link_trade_id) not in candidate_ids:
                    raise DecisionDraftError("指定的合并交易不属于这笔定投，已停止处理。")
                linked_trade = conn.execute(
                    "SELECT * FROM trades WHERE id=?", (int(link_trade_id),)
                ).fetchone()
            elif linked_trade is None and link_trade_id is not None:
                raise DecisionDraftError("指定的待确认交易不存在或不满足安全合并条件。")

            nav = float(occurrence["nav"] or 0)
            units = float(occurrence["units"] or 0)
            fee = float(occurrence["fee"] or 0)
            pricing_date = str(occurrence["pricing_date"] or "")
            ready = bool(nav > 0 and units > 0 and pricing_date)
            settlement_status = (
                "公开净值已核验；待支付宝份额/确认日复核"
                if ready else "已确认扣款；等待正式净值"
            )
            linked_trade_id = None
            if linked_trade is not None:
                linked_trade_id = int(linked_trade["id"])
                existing_reason = str(linked_trade["rationale"] or "").strip()
                audit_reason = (
                    f"用户于{now_dt.date().isoformat()}确认{draft['trade_date']}支付宝实际扣款"
                    f"{float(draft['planned_amount']):.2f}元；最终份额、确认日期和手续费仍待平台复核。"
                )
                rationale = "；".join(value for value in (existing_reason, audit_reason) if value)
                conn.execute(
                    """UPDATE trades SET action='定投',nav=?,units=?,fee=?,rationale=?,
                               source=?,nav_date=?,source_url=?,recurring_occurrence_id=?,
                               counts_toward_active_capital=0,result_pnl_status='不适用'
                         WHERE id=?""",
                    (
                        float(linked_trade["nav"] or 0) or nav,
                        float(linked_trade["units"] or 0) or units,
                        float(linked_trade["fee"] or 0) or fee,
                        rationale,
                        "用户确认实际扣款·待平台结算复核",
                        pricing_date,
                        str(linked_trade["source_url"] or occurrence["source_url"] or ""),
                        int(occurrence["id"]),
                        linked_trade_id,
                    ),
                )

            confirmation_note = (
                f"用户于{now_dt.date().isoformat()}确认该日支付宝实际扣款"
                f"¥{float(draft['planned_amount']):.2f}；仅确认扣款事实，最终确认份额、"
                "确认日期和手续费仍以支付宝正式结果为准。"
            )
            old_notes = str(occurrence["notes"] or "").strip()
            notes = old_notes if confirmation_note in old_notes else (
                old_notes + "；" + confirmation_note if old_notes else confirmation_note
            )
            conn.execute(
                """UPDATE recurring_occurrences
                      SET deduction_confirmed=1,deduction_confirmed_at=?,
                          deduction_confirmation_source='用户在今日决策确认',
                          notes=?,updated_at=? WHERE id=?""",
                (now, notes, now, int(occurrence["id"])),
            )
            result_note = (
                f"用户已确认{draft['trade_date']}实际扣款¥{float(draft['planned_amount']):.2f}。"
                + (f"已与交易日志#{linked_trade_id}去重关联；" if linked_trade_id else "")
                + (
                    f"公开净值{nav:.4f}、估算份额{units:.2f}；"
                    if ready else "正式净值和份额尚未取得；"
                )
                + "尚未按支付宝正式结果过账。"
            )
            conn.execute(
                """UPDATE decision_drafts SET status='等待净值确认',result_note=?,trade_id=?,
                           posted_at=?,updated_at=?,nav_date=?,settlement_nav=?,settlement_fee=?,
                           settlement_units=?,settlement_status=?,settlement_source_url=?,
                           settlement_checked_at=? WHERE id=? AND status='草稿'""",
                (
                    result_note, linked_trade_id, now, now, pricing_date, nav, fee, units,
                    settlement_status, str(occurrence["source_url"] or ""),
                    str(occurrence["fetched_at"] or ""), int(draft["id"]),
                ),
            )
            return {
                "draft_id": int(draft["id"]),
                "occurrence_id": int(occurrence["id"]),
                "linked_trade_id": linked_trade_id,
                "already_confirmed": False,
                "nav": nav,
                "units": units,
                "settlement_status": settlement_status,
            }

    def abandon_stale_draft(
        self, draft_id: int, *, reason: str = "用户确认当日未实际执行", as_of: datetime | None = None,
    ) -> None:
        """Keep an audit trace while releasing an old draft's reservation."""
        now = (as_of or local_now()).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM decision_drafts WHERE id=?", (int(draft_id),)
            ).fetchone()
            if not row or str(row["status"] or "") != "草稿":
                raise DecisionDraftError("这条旧草稿已经处理。")
            if str(row["trade_date"] or "") >= self._day(as_of):
                raise DecisionDraftError("今天的草稿请使用删除/撤销，不要标成跨日未执行。")
            if row["recurring_occurrence_id"]:
                conn.execute(
                    """UPDATE recurring_occurrences SET status='已跳过',notes=?,updated_at=?
                         WHERE id=?""",
                    (str(reason or "用户确认当日未扣款"), now, int(row["recurring_occurrence_id"])),
                )
            conn.execute(
                """UPDATE decision_drafts SET status='已放弃',result_note=?,posted_at=?,updated_at=?
                     WHERE id=? AND status='草稿'""",
                (str(reason or "用户确认当日未实际执行"), now, now, int(draft_id)),
            )

    def add_manual(
        self, *, fund_code: str, fund_name: str = "", action: str,
        planned_amount: float = 0, planned_units: float = 0, planned_pct: float = 0,
        rationale: str = "", pricing_date: str = "", as_of: datetime | None = None,
        counts_toward_active_capital: int | None = None,
    ) -> int:
        code, name, action, amount, _units, _percent, fund = self._validate_manual(
            fund_code=fund_code, fund_name=fund_name, action=action,
            planned_amount=planned_amount, planned_units=planned_units, planned_pct=planned_pct,
        )
        now_dt = as_of or local_now()
        now = now_dt.isoformat(timespec="seconds")
        day = self._day(as_of)
        pricing_day = self._validate_pricing_date(
            pricing_date or self._default_pricing_date(code, now_dt)
        )
        with self.db.connect() as conn:
            if not fund:
                conn.execute(
                    """INSERT INTO funds(
                           fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,
                           purchase_fee_pct,status,notes,updated_at,name_status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (code, name, "", "", "", "待分类", 0.0, "待自动识别", "仅录入基金代码；系统空闲时联网补全名称。", now, "待自动识别"),
                )
            if counts_toward_active_capital is None:
                counts = 1 if fund and infer_active_capital(fund) else 0
            else:
                counts = 1 if int(counts_toward_active_capital or 0) else 0
            cursor = conn.execute(
                """INSERT INTO decision_drafts(
                       trade_date,source_kind,source_key,fund_code,fund_name,action,planned_amount,
                       planned_units,planned_pct,rationale,status,created_at,updated_at,pricing_date,
                       settlement_status,counts_toward_active_capital
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    day, "用户手工", f"manual:{uuid4().hex}", code, name, action, round(amount, 2),
                    0.0, 0.0, str(rationale or "").strip(), "草稿", now, now, pricing_day,
                    "待联网补全", counts,
                ),
            )
            return int(cursor.lastrowid)

    def _default_pricing_date(self, fund_code: str, submitted_at: datetime) -> str:
        fund = self.db.row(
            "SELECT fund_name,index_name,notes FROM funds WHERE fund_code=?", (fund_code,)
        ) or {}
        kind = infer_fund_kind(
            str(fund.get("fund_name") or ""), str(fund.get("index_name") or ""),
            str(fund.get("notes") or ""),
        )
        return ChinaTradingCalendar.from_db(self.db).resolve_order(
            submitted_at, fund_kind=kind,
        ).pricing_date.isoformat()

    def _validate_pricing_date(self, value: str) -> str:
        try:
            parsed = date.fromisoformat(str(value or "").strip())
        except ValueError as exc:
            raise DecisionDraftError("净值所属交易日必须是 YYYY-MM-DD，例如 2026-08-07。") from exc
        if not ChinaTradingCalendar.from_db(self.db).is_trading_day(parsed):
            raise DecisionDraftError(
                f"{parsed.isoformat()}不是交易日，不能作为净值定价日；请填写实际交易日。"
            )
        return parsed.isoformat()

    def _validate_manual(
        self, *, fund_code: str, fund_name: str, action: str,
        planned_amount: float, planned_units: float, planned_pct: float,
    ) -> tuple[str, str, str, float, float, float, dict[str, Any] | None]:
        """Validate and calculate a manual draft without writing any rows."""
        code = str(fund_code or "").strip()
        name = str(fund_name or "").strip()
        action = str(action or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise DecisionDraftError("基金代码必须是6位数字。")
        if action not in self.ACTIONS:
            raise DecisionDraftError("盘中草稿操作只能是买入、卖出、清仓、转换或其他。")
        try:
            amount = max(0.0, float(planned_amount or 0))
            units = 0.0
            percent = 0.0
        except (TypeError, ValueError) as exc:
            raise DecisionDraftError("操作金额必须是数字。") from exc

        fund = self.db.row("SELECT * FROM funds WHERE fund_code=?", (code,))
        holding = self.db.row(
            "SELECT * FROM holdings WHERE fund_code=? AND active=1 ORDER BY id LIMIT 1", (code,)
        )
        if fund:
            name = fund["fund_name"]
        elif not name:
            name = f"待自动识别（{code}）"
        if amount <= 0:
            raise DecisionDraftError("请填写这笔操作的金额；场外基金份额将在净值发布后联网估算。")

        return code, name, action, amount, units, percent, fund

    def add_candidate(self, candidate_id: int, as_of: datetime | None = None) -> int:
        row = self.db.row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
        if not row:
            raise DecisionDraftError("找不到这条AI板块建议。")
        from .services import latest_current_radar_run

        current_run = latest_current_radar_run(self.db, as_of=as_of)
        if not current_run or str(current_run.get("task_id") or "") != str(row.get("task_id") or ""):
            raise DecisionDraftError(
                "这条AI建议已不是今天当前严格C目录的结果；请完成当前目录研究后再加入草稿。"
            )
        day = self._day(as_of)
        amount = self.live_candidate_amount(row, day=day)
        code = str(row.get("fund_code") or "").strip()
        if amount <= 0:
            detail = str(row.get("execution_reason") or row.get("timing_status") or row.get("execution_status") or "当前没有可新增金额")
            raise DecisionDraftError(f"这条研究建议当前不能形成买入草稿：{detail}。")
        if not code:
            raise DecisionDraftError("这条板块建议尚未绑定可买基金，请先到“基金优选”核对并绑定。")
        index_row = self.db.row(
            """SELECT id FROM investable_indices
                 WHERE universe_id=? AND active=1 AND radar_enabled=1
                   AND catalog_role='日扫代表'""",
            (str(row.get("universe_id") or ""),),
        )
        strict_codes = set() if not index_row else {
            str(fund.get("fund_code") or "")
            for fund in PlatformCatalogService(self.db).strict_catalog_funds(int(index_row["id"]))
        }
        if code not in strict_codes:
            raise DecisionDraftError(
                "这只基金已不在该指数当前严格C白名单中；旧建议不能继续下单。"
            )
        self.db.link_strict_funds_to_library(int(index_row["id"]))
        library_fund = self.db.row("SELECT * FROM funds WHERE fund_code=?", (code,))
        if not library_fund:
            raise DecisionDraftError("严格C基金自动衔接基金库失败；未建立草稿，请导出诊断。")
        if not str(row.get("fund_name") or "").strip():
            row["fund_name"] = str(library_fund.get("fund_name") or "")
        now_dt = as_of or local_now()
        now = now_dt.isoformat(timespec="seconds")
        pricing_day = self._default_pricing_date(code, now_dt)
        return self._upsert_ai(
            day=day,
            source_key=f"candidate:{row['task_id']}:{row.get('universe_id') or row['id']}",
            source_kind="AI板块建议",
            task_id=row["task_id"], candidate_id=row["id"], fund_code=code,
            fund_name=str(row.get("fund_name") or ""), action="买入", amount=amount,
            units=0, percent=0, rationale=str(row.get("thesis") or ""),
            ai_advice=(
                f"AI排名{row['rank_no']}；研究结论{row.get('research_tilt') or row['status']}；"
                f"择时{row.get('timing_status') or '待核验'}；执行{row.get('execution_status') or '待核验'}；"
                f"目标占主动资金上限{float(row.get('target_allocation_pct') or row.get('allocation_pct') or 0):.1f}%；"
                "加入草稿时已按实时现金、同指数、相关性桶和主题上限重算金额；"
                f"风险：{row.get('risk') or '未说明'}"
            ), now=now, pricing_date=pricing_day,
        )

    def live_candidate_amount(self, row: dict[str, Any], day: str | None = None) -> float:
        """Recalculate a candidate's incremental buy from the live portfolio."""
        return self.live_candidate_amounts([row], day=day).get(int(row["id"]), 0.0)

    def live_candidate_amounts(
        self, rows: list[dict[str, Any]], day: str | None = None,
    ) -> dict[int, float]:
        """Return live amounts while leaving each seven-level view untouched."""
        return {
            candidate_id: round(float(row.get("suggested_amount") or 0), 2)
            for candidate_id, row in self.live_candidate_allocations(rows, day=day).items()
        }

    def live_candidate_allocations(
        self, rows: list[dict[str, Any]], day: str | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Return the complete live gate result used by UI, drafts and close.

        The returned copies may update execution status/reason and amount, but
        deliberately never replace ``research_tilt``.  Therefore stale or weak
        evidence can make an otherwise positive study non-actionable without
        relabelling the research conclusion as ``观察``.
        """
        if not rows:
            return {}
        prepared = []
        for source in rows:
            row = dict(source)
            target_pct = row.get("target_allocation_pct")
            if target_pct not in (None, ""):
                row["allocation_pct"] = target_pct
            # Legacy rows did not persist freshness.  Only current V3 rows can
            # be actionable; their execution status was already freshness-gated.
            row["freshness_ok"] = str(row.get("execution_status") or "") != "行情待更新"
            prepared.append(row)
        allocated = allocate_candidate_amounts(
            self.db, prepared, release_existing_candidate_drafts=True,
        )
        return {
            int(row["id"]): row
            for row in allocated if row.get("id") not in (None, "")
        }

    def live_holding_action_amount(self, advice: dict[str, Any]) -> float | None:
        action = str(advice.get("action") or "")
        percent = float(advice.get("sell_pct") or 0)
        if action not in {"减仓", "清仓"} or percent <= 0:
            return None
        holding = self.db.row(
            "SELECT units,latest_nav FROM holdings WHERE fund_code=? AND active=1 ORDER BY id LIMIT 1",
            (str(advice.get("fund_code") or ""),),
        )
        if not holding:
            return None
        percent = 100.0 if action == "清仓" else min(99.0, percent)
        return round(float(holding["units"]) * float(holding["latest_nav"]) * percent / 100, 2)

    def add_holding_action(
        self, task_id: str, fund_code: str, as_of: datetime | None = None,
    ) -> int:
        from .services import latest_current_radar_run

        current_run = latest_current_radar_run(self.db, as_of=as_of)
        if not current_run or str(current_run.get("task_id") or "") != str(task_id or ""):
            raise DecisionDraftError(
                "这条AI持仓建议不是今天当前且行情新鲜的完整研究；只能阅读，不能加入卖出草稿。"
            )
        advice = self.db.row(
            "SELECT * FROM holding_recommendations WHERE task_id=? AND fund_code=?",
            (task_id, fund_code),
        )
        if not advice:
            raise DecisionDraftError("这条旧研究只有文字建议，尚无可加入草稿的量化持仓动作。")
        action = str(advice.get("action") or "")
        percent = float(advice.get("sell_pct") or 0)
        if action not in {"减仓", "清仓"} or percent <= 0:
            raise DecisionDraftError("AI建议是持有或继续定投，不需要建立卖出草稿。")
        holding = self.db.row(
            "SELECT * FROM holdings WHERE fund_code=? AND active=1 ORDER BY id LIMIT 1", (fund_code,)
        )
        if not holding:
            raise DecisionDraftError("当前持仓中已找不到这只基金。")
        percent = 100.0 if action == "清仓" else min(99.0, percent)
        units = float(holding["units"]) * percent / 100
        amount = self.live_holding_action_amount(advice)
        if amount is None or amount <= 0:
            raise DecisionDraftError("当前实时持仓无法计算这笔卖出金额。")
        now_dt = as_of or local_now()
        now = now_dt.isoformat(timespec="seconds")
        day = self._day(as_of)
        pricing_day = self._default_pricing_date(fund_code, now_dt)
        return self._upsert_ai(
            day=day, source_key=f"holding:{task_id}:{fund_code}", source_kind="AI持仓建议",
            task_id=task_id, holding_recommendation_id=advice["id"], fund_code=fund_code,
            fund_name=str(advice.get("fund_name") or ""), action="清仓" if percent == 100 else "卖出",
            amount=amount, units=0, percent=0, rationale=str(advice.get("reason") or ""),
            ai_advice=f"AI动作：{action}；卖出该持仓{percent:.1f}%；风险：{advice.get('risk') or '未说明'}",
            now=now, pricing_date=pricing_day,
        )

    def _upsert_ai(self, *, day: str, source_key: str, source_kind: str, fund_code: str,
                   fund_name: str, action: str, amount: float, units: float, percent: float,
                   rationale: str, ai_advice: str, now: str, task_id: str = "",
                   candidate_id: int | None = None,
                   holding_recommendation_id: int | None = None,
                   pricing_date: str = "") -> int:
        if candidate_id is not None:
            counts = 1
        else:
            holding = self.db.row(
                "SELECT counts_toward_active_capital FROM holdings WHERE fund_code=? AND active=1 ORDER BY id LIMIT 1",
                (fund_code,),
            )
            fund = self.db.row("SELECT * FROM funds WHERE fund_code=?", (fund_code,))
            counts = int(holding["counts_toward_active_capital"]) if holding else (
                1 if fund and infer_active_capital(fund) else 0
            )
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM decision_drafts WHERE trade_date=? AND source_key=?", (day, source_key)
            ).fetchone()
            if existing and existing["status"] != "草稿":
                raise DecisionDraftError("这条AI建议今天已经写入记录，不能重复加入。")
            if existing:
                conn.execute(
                    """UPDATE decision_drafts SET fund_code=?,fund_name=?,action=?,planned_amount=?,
                       planned_units=0,planned_pct=0,rationale=?,ai_advice=?,pricing_date=?,
                       settlement_status='待联网补全',counts_toward_active_capital=?,updated_at=? WHERE id=?""",
                    (fund_code, fund_name, action, round(amount, 2), rationale, ai_advice,
                     self._validate_pricing_date(pricing_date or day),
                     counts, now, existing["id"]),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """INSERT INTO decision_drafts(
                       trade_date,source_kind,source_key,task_id,candidate_id,holding_recommendation_id,
                       fund_code,fund_name,action,planned_amount,planned_units,planned_pct,rationale,
                       ai_advice,status,created_at,updated_at,pricing_date,settlement_status,
                       counts_toward_active_capital
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (day, source_kind, source_key, task_id, candidate_id, holding_recommendation_id,
                  fund_code, fund_name, action, round(amount, 2), 0.0, 0.0,
                  rationale, ai_advice, "草稿", now, now,
                  self._validate_pricing_date(pricing_date or day), "待联网补全", counts),
            )
            return int(cursor.lastrowid)

    def update_draft(self, draft_id: int, **values: Any) -> None:
        row = self.db.row("SELECT * FROM decision_drafts WHERE id=?", (draft_id,))
        if not row or row["status"] != "草稿":
            raise DecisionDraftError("只有仍存活的草稿可以编辑。")
        if row.get("recurring_occurrence_id"):
            raise DecisionDraftError("自动定投金额由定投计划控制；如今日未扣款，请删除这条草稿。")
        code, name, action, amount, _units, _percent, fund = self._validate_manual(
            fund_code=values.get("fund_code", row["fund_code"]),
            fund_name=values.get("fund_name", row["fund_name"]),
            action=values.get("action", row["action"]),
            planned_amount=values.get("planned_amount", row["planned_amount"]),
            planned_units=values.get("planned_units", row["planned_units"]),
            planned_pct=values.get("planned_pct", row["planned_pct"]),
        )
        rationale = str(values.get("rationale", row["rationale"]))
        pricing_day = self._validate_pricing_date(values.get("pricing_date", row.get("pricing_date") or row["trade_date"]))
        now = local_now().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            if not fund:
                conn.execute(
                    """INSERT INTO funds(
                           fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,
                           purchase_fee_pct,status,notes,updated_at,name_status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (code, name, "", "", "", "待分类", 0.0, "待自动识别", "仅录入基金代码；系统空闲时联网补全名称。", now, "待自动识别"),
                )
            conn.execute(
                """UPDATE decision_drafts SET fund_code=?,fund_name=?,action=?,planned_amount=?,
                   planned_units=0,planned_pct=0,rationale=?,pricing_date=?,settlement_status='待联网补全',
                   settlement_nav=0,settlement_fee=0,settlement_units=0,
                   counts_toward_active_capital=?,updated_at=? WHERE id=?""",
                (
                    code, name, action, round(amount, 2), rationale.strip(), pricing_day,
                    int(row.get("counts_toward_active_capital") or 0), now, draft_id,
                ),
            )

    def validate_surviving_allocation(
        self, day: str | None = None, as_of: datetime | None = None,
    ) -> RiskResult:
        """Validate every live tactical buy before the close writes records."""
        day = day or local_today().isoformat()
        drafts = self.db.rows(
            """SELECT d.*,f.index_code,f.index_name,f.correlation_bucket
               FROM decision_drafts d LEFT JOIN funds f USING(fund_code)
               WHERE d.trade_date=? AND d.status='草稿'
                 AND d.action IN ('买入','定投','转换转入')
                 AND COALESCE(d.counts_toward_active_capital,0)=1
               ORDER BY d.id""",
            (day,),
        )
        if not drafts:
            return RiskResult("profit", "通过", ["没有需要占用主动资金的存活买入草稿。"])

        proposals: list[dict[str, Any]] = []
        execution_reasons: list[str] = []
        catalog = PlatformCatalogService(self.db)
        candidate_drafts = [row for row in drafts if row.get("candidate_id")]
        live_candidate_rows: dict[int, dict[str, Any]] = {}
        if candidate_drafts:
            candidate_ids = sorted({int(row["candidate_id"]) for row in candidate_drafts})
            marks = ",".join("?" for _ in candidate_ids)
            stored_candidates = self.db.rows(
                f"SELECT * FROM candidates WHERE id IN ({marks}) ORDER BY rank_no,id",
                candidate_ids,
            )
            live_candidate_rows = self.live_candidate_allocations(stored_candidates, day=day)
            found_ids = set(live_candidate_rows)
            for missing_id in sorted(set(candidate_ids) - found_ids):
                execution_reasons.append(
                    f"AI候选#{missing_id}已不存在，关联草稿不能绕过当前研究门槛入账。"
                )
        for row in drafts:
            identity = fund_exposure_identity(self.db, str(row.get("fund_code") or ""))
            proposals.append({
                **identity,
                "name": str(row.get("fund_name") or identity["index_name"]),
                "amount": float(row.get("planned_amount") or 0),
                "counts_toward_active_capital": 1,
            })
            if row.get("candidate_id"):
                candidate = live_candidate_rows.get(int(row["candidate_id"]))
                if candidate:
                    allowed = max(0.0, float(candidate.get("suggested_amount") or 0))
                    planned = max(0.0, float(row.get("planned_amount") or 0))
                    if str(row.get("action") or "") != "买入":
                        execution_reasons.append(
                            f"AI候选#{row['candidate_id']}草稿已被改成{row.get('action') or '未知操作'}；"
                            "请删除后按手工操作重新建立，不能借AI候选绕过门槛。"
                        )
                    if str(row.get("fund_code") or "") != str(candidate.get("fund_code") or ""):
                        execution_reasons.append(
                            f"AI候选#{row['candidate_id']}的草稿基金与当前候选不一致，不能入账。"
                        )
                    if allowed <= 0:
                        execution_reasons.append(
                            f"{row.get('fund_code')}当前不能入账："
                            f"{candidate.get('execution_reason') or candidate.get('execution_status') or '实时硬门槛未通过'}。"
                        )
                    elif planned > allowed + 0.005:
                        execution_reasons.append(
                            f"{row.get('fund_code')}草稿{planned:.2f}元超过同一实时门槛允许的"
                            f"{allowed:.2f}元。"
                        )
            mapping = self.db.row(
                """SELECT f.investable_index_id FROM investable_funds f
                   JOIN investable_indices i ON i.id=f.investable_index_id
                   WHERE f.fund_code=? AND f.active=1 AND i.active=1
                     AND f.relation_kind='同一指数'
                   ORDER BY f.radar_eligible DESC,f.quality_rank,f.id LIMIT 1""",
                (str(row.get("fund_code") or ""),),
            )
            if not mapping:
                execution_reasons.append(
                    f"{row.get('fund_code')}尚未精确映射到当前中证指数，不能写入主动买单。"
                )
            else:
                try:
                    catalog.require_fund_execution(
                        int(mapping["investable_index_id"]), str(row.get("fund_code") or ""),
                        intended_amount=float(row.get("planned_amount") or 0),
                        as_of=as_of, require_same_day=True,
                    )
                except ValueError as exc:
                    execution_reasons.append(f"{row.get('fund_code')}：{exc}")
        pending = self.db.rows(
            """SELECT t.*,f.index_code,f.index_name,f.correlation_bucket
               FROM trades t LEFT JOIN funds f USING(fund_code)
               WHERE t.status='待确认' AND t.action IN ('买入','定投','转换转入')
                 AND COALESCE(t.counts_toward_active_capital,0)=1"""
        )
        for row in pending:
            identity = fund_exposure_identity(self.db, str(row.get("fund_code") or ""))
            proposals.append({
                **identity, "name": identity["index_name"],
                "amount": float(row.get("amount") or 0),
                "counts_toward_active_capital": 1,
            })

        current = []
        for exposure in active_index_exposures(self.db).values():
            current.append({
                **exposure, "name": exposure["index_name"],
                "capital_amount": float(exposure.get("cost") or 0),
                "counts_toward_active_capital": 1,
            })
        result = validate_allocation(proposals, current, self.db.settings())
        capital = active_capital_summary(self.db)
        if float(capital["cash_unclamped"]) < -0.005:
            result.level = "danger"
            result.decision = "不通过"
            result.reasons.append(
                f"存活草稿和待确认买单已超出主动资金现金余额{abs(float(capital['cash_unclamped'])):.2f}元。"
            )
        if execution_reasons:
            result.level = "danger"
            result.decision = "不通过"
            result.reasons.extend(execution_reasons)
        return result

    def delete_draft(self, draft_id: int, as_of: datetime | None = None) -> None:
        now = (as_of or local_now()).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM decision_drafts WHERE id=?", (draft_id,)).fetchone()
            if not row or row["status"] != "草稿":
                raise DecisionDraftError("只有仍存活、尚未入账的草稿可以删除。")
            if row["recurring_occurrence_id"]:
                conn.execute(
                    """UPDATE recurring_occurrences SET status='已跳过',notes=?,updated_at=? WHERE id=?""",
                    ("用户从今日草稿删除：今日未扣款或已撤销。", now, row["recurring_occurrence_id"]),
                )
            conn.execute("DELETE FROM decision_drafts WHERE id=?", (draft_id,))

    def finalize_after_close(self, as_of: datetime | None = None) -> dict[str, int]:
        as_of = as_of or local_now()
        close_hour = int(float(self.db.setting("decision_close_hour", "15")))
        if as_of.hour < close_hour:
            raise DecisionDraftError(f"为避免把盘中撤单写成真实记录，请在{close_hour}:00收盘后再使用一键入账。")
        day = self._day(as_of)
        validation = self.validate_surviving_allocation(day, as_of=as_of)
        if validation.decision != "通过":
            raise DecisionDraftError("收盘入账前资金校验未通过：" + "；".join(validation.reasons))
        now = as_of.isoformat(timespec="seconds")
        result = {"trades": 0, "recurring": 0, "total": 0}
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decision_drafts WHERE trade_date=? AND status='草稿' ORDER BY id", (day,)
            ).fetchall()
            for row in rows:
                if row["recurring_occurrence_id"]:
                    conn.execute(
                        """UPDATE decision_drafts SET status='等待净值确认',result_note=?,posted_at=?,updated_at=?
                           WHERE id=? AND status='草稿'""",
                        ("已转入定投待确认流水；取得正式净值并核对支付宝扣款后才进入持仓。", now, now, row["id"]),
                    )
                    result["recurring"] += 1
                else:
                    fund = conn.execute("SELECT * FROM funds WHERE fund_code=?", (row["fund_code"],)).fetchone()
                    counts = int(row["counts_toward_active_capital"] or 0)
                    cursor = conn.execute(
                        """INSERT INTO trades(
                               trade_date,fund_code,action,amount,units,nav,fee,rationale,ai_advice,
                               result_pnl,created_at,confirm_date,status,source,nav_date,source_url,
                               recurring_occurrence_id,counts_toward_active_capital
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (day, row["fund_code"], row["action"], row["planned_amount"],
                         row["settlement_units"] or 0.0, row["settlement_nav"] or 0.0,
                         row["settlement_fee"] or 0.0, row["rationale"], row["ai_advice"], None, now,
                         "", "待确认", f"今日草稿·{row['source_kind']}", row["nav_date"] or "",
                         row["settlement_source_url"] or "", None, counts),
                    )
                    trade_id = int(cursor.lastrowid)
                    conn.execute(
                        """UPDATE decision_drafts SET status='已写入交易',trade_id=?,result_note=?,
                           posted_at=?,updated_at=? WHERE id=? AND status='草稿'""",
                        (trade_id, "已写入交易日志，等待支付宝确认净值、份额和费用。", now, now, row["id"]),
                    )
                    result["trades"] += 1
            result["total"] = len(rows)
        return result
