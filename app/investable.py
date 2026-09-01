from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from .config import local_now
from .fund_discovery import (
    FundDiscoveryError, FundDiscoveryService, FundFeeSyncService,
    parse_fund_profile, parse_small_purchase_rate,
)
from .recurring import EastmoneyFundNavProvider, calculate_subscription


class InvestableDataError(RuntimeError):
    pass


_BACKGROUND_LOCK = threading.Lock()


@dataclass
class DraftEnrichmentResult:
    checked: int = 0
    names_resolved: int = 0
    fee_schedules_resolved: int = 0
    fee_schedule_failures: int = 0
    navs_resolved: int = 0
    waiting: int = 0
    failed: int = 0
    stopped: bool = False


@dataclass
class PendingTradeEnrichmentResult:
    checked: int = 0
    resolved: int = 0
    waiting: int = 0
    failed: int = 0
    stopped: bool = False
    errors: tuple[str, ...] = ()


class InvestableMappingService:
    """Keep research categories, quote proxies, investable indices and funds distinct."""

    def __init__(self, db):
        self.db = db

    def identity_for(self, universe_id: str) -> dict[str, Any] | None:
        return self.db.row(
            """SELECT i.*,p.proxy_code,p.proxy_name,p.security_id
               FROM investable_indices i
               LEFT JOIN proxy_mappings p ON p.universe_id=i.universe_id
               WHERE i.universe_id=? AND i.active=1
               ORDER BY i.display_order,i.id LIMIT 1""",
            (universe_id,),
        )

    def funds_for(self, universe_id: str) -> list[dict[str, Any]]:
        return self.db.rows(
            """SELECT f.*,i.official_index_code,i.official_index_name,i.ant_index_code,i.ant_index_name
               FROM investable_funds f JOIN investable_indices i ON i.id=f.investable_index_id
               WHERE i.universe_id=? AND i.active=1 AND f.active=1
               ORDER BY CASE f.strategy_kind WHEN '被动ETF联接' THEN 0 WHEN '被动指数' THEN 1 ELSE 2 END,
                        f.quality_rank,f.fund_code""",
            (universe_id,),
        )

    def coverage(self) -> dict[str, int]:
        row = self.db.row(
            """SELECT
                   (SELECT COUNT(*) FROM universe) AS universe_total,
                   (SELECT COUNT(DISTINCT universe_id) FROM investable_indices
                    WHERE active=1 AND verification_status<>'待核验') AS mapped_total,
                   (SELECT COUNT(*) FROM investable_funds WHERE active=1) AS fund_total,
                   (SELECT COUNT(*) FROM investable_funds
                    WHERE active=1 AND (ant_buyable_status LIKE '已核对%'
                         OR ant_buyable_status='用户已实际买入')) AS ant_verified_funds"""
        ) or {}
        return {key: int(value or 0) for key, value in row.items()}


class FundMetadataService:
    """Resolve six-digit fund codes using free public fund pages; never calls DeepSeek."""

    def __init__(self, db, timeout_seconds: int | None = None):
        self.db = db
        self.discovery = FundDiscoveryService(db, timeout_seconds=timeout_seconds)

    def resolve_one(self, fund_code: str) -> dict[str, Any]:
        code = str(fund_code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise InvestableDataError("基金代码必须是6位数字。")
        try:
            items = self.discovery._search(code)
        except FundDiscoveryError as exc:
            raise InvestableDataError(str(exc)) from exc
        exact: dict[str, Any] | None = None
        for item in items:
            base = item.get("FundBaseInfo") or {}
            item_code = str(item.get("CODE") or base.get("FCODE") or "")
            if item_code == code and base:
                exact = {
                    "fund_name": str(item.get("NAME") or base.get("SHORTNAME") or "").strip(),
                    "company": str(base.get("JJGS") or "").strip(),
                    "fund_type": str(base.get("FTYPE") or "").strip(),
                    "public_buyable": str(base.get("ISBUY") or "") == "1",
                }
                break
        if not exact or not exact["fund_name"]:
            raise InvestableDataError(f"公开基金搜索未找到代码 {code} 的有效基金资料。")
        source_url = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
        try:
            page = self.discovery._download(source_url).decode("utf-8", errors="replace")
            profile = parse_fund_profile(page)
            if profile.get("purchase_fee_pct") is None:
                fee_url = f"https://fundf10.eastmoney.com/jjfl_{code}.html"
                fee_page = self.discovery._download(fee_url).decode("utf-8", errors="replace")
                profile["purchase_fee_pct"] = parse_small_purchase_rate(fee_page)
        except Exception:
            profile = {}
        exact.update(profile)
        exact["fund_code"] = code
        exact["source_url"] = source_url
        now = local_now().isoformat(timespec="seconds")
        name = exact.get("fund_name") or exact.get("full_name") or code
        target = str(exact.get("target_index") or "")
        purchase_fee = exact.get("purchase_fee_pct")
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO funds(
                       fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,
                       purchase_fee_pct,status,notes,updated_at,name_status,profile_source_url,profile_checked_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fund_code) DO UPDATE SET
                       fund_name=excluded.fund_name,
                       index_name=CASE WHEN excluded.index_name<>'' THEN excluded.index_name ELSE funds.index_name END,
                       purchase_fee_pct=CASE WHEN excluded.purchase_fee_pct>=0 THEN excluded.purchase_fee_pct ELSE funds.purchase_fee_pct END,
                       status=CASE WHEN funds.status='持有' OR funds.status='定投' THEN funds.status ELSE excluded.status END,
                       notes=excluded.notes,updated_at=excluded.updated_at,name_status='已自动识别',
                       profile_source_url=excluded.profile_source_url,profile_checked_at=excluded.profile_checked_at""",
                (
                    code, name, "", target, "", "待分类", float(purchase_fee or 0),
                    "公开资料可申购" if exact["public_buyable"] else "公开资料待核验",
                    f"系统按基金代码自动识别；{exact.get('company') or '管理人待核验'}；{exact.get('fund_type') or '类型待核验'}。",
                    now, "已自动识别", source_url, now,
                ),
            )
            conn.execute(
                """UPDATE decision_drafts SET fund_name=?,updated_at=?
                   WHERE fund_code=? AND (fund_name='' OR fund_name LIKE '待自动识别%')""",
                (name, now, code),
            )
        return exact

    def resolve_pending(
        self,
        limit: int = 8,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[int, list[str]]:
        rows = self.db.rows(
            """SELECT fund_code FROM funds
               WHERE name_status IN ('待自动识别','识别失败') OR fund_name='' OR fund_name LIKE '待自动识别%'
               ORDER BY updated_at,fund_code LIMIT ?""",
            (max(1, min(50, int(limit))),),
        )
        resolved, errors = 0, []
        for position, row in enumerate(rows, 1):
            if stop_requested and stop_requested():
                break
            try:
                self.resolve_one(row["fund_code"])
                resolved += 1
            except Exception as exc:
                errors.append(f"{row['fund_code']}：{exc}")
                self.db.execute(
                    "UPDATE funds SET name_status='识别失败',profile_checked_at=? WHERE fund_code=?",
                    (local_now().isoformat(timespec="seconds"), row["fund_code"]),
                )
            if progress:
                progress(position, len(rows), str(row["fund_code"]))
        return resolved, errors

    def resolve_pending_if_idle(
        self,
        limit: int = 8,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[int, list[str]] | None:
        """Resolve pending names only when another idle resolver is not active.

        Thread ownership deliberately stays with the UI application.  Earlier
        versions spawned an untracked daemon thread here, so database restore
        and application shutdown could not see that a network/database worker
        was still running.  Returning ``None`` means another idle resolver owns
        the lock; callers may simply skip this opportunistic pass.
        """
        if not _BACKGROUND_LOCK.acquire(blocking=False):
            return None
        try:
            return self.resolve_pending(
                limit, stop_requested=stop_requested, progress=progress,
            )
        finally:
            _BACKGROUND_LOCK.release()


class DraftSettlementService:
    """Fetch published NAV for a draft's pricing day and calculate non-authoritative shares."""

    def __init__(self, db, timeout_seconds: int | None = None):
        self.db = db
        timeout = timeout_seconds or int(float(db.setting("market_timeout_seconds", "20")))
        self.nav_provider = EastmoneyFundNavProvider(timeout_seconds=timeout)
        self.metadata = FundMetadataService(db, timeout_seconds=timeout)
        self.fee_sync = FundFeeSyncService(db)

    def _sync_missing_fee_schedule(self, fund_code: str) -> tuple[str, str]:
        """Best-effort, free fee lookup; never blocks NAV/share enrichment."""
        if self.db.row("SELECT 1 FROM fee_tiers WHERE fund_code=? LIMIT 1", (fund_code,)):
            return "existing", ""
        try:
            report = self.fee_sync.sync([fund_code], only_missing=True)
        except Exception as exc:
            return "failed", str(exc)
        if self.db.row("SELECT 1 FROM fee_tiers WHERE fund_code=? LIMIT 1", (fund_code,)):
            return "updated", ""
        errors = report.get("errors") or []
        return "failed", str(errors[0] if errors else "公开费率页暂未识别到赎回费阶梯")

    def enrich(
        self,
        draft_ids: list[int] | None = None,
        day: str | None = None,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> DraftEnrichmentResult:
        params: list[Any] = []
        where = "1=1"
        if draft_ids:
            placeholders = ",".join("?" for _ in draft_ids)
            where += f" AND id IN ({placeholders})"
            params.extend(int(value) for value in draft_ids)
        elif day:
            where += " AND trade_date=?"
            params.append(day)
        rows = self.db.rows(f"SELECT * FROM decision_drafts WHERE {where} ORDER BY id", params)
        result = DraftEnrichmentResult()
        fee_checked: set[str] = set()
        for position, row in enumerate(rows, 1):
            if stop_requested and stop_requested():
                result.stopped = True
                break
            result.checked += 1
            try:
                fund = self.db.row("SELECT * FROM funds WHERE fund_code=?", (row["fund_code"],))
                if not fund or fund.get("name_status") in {"待自动识别", "识别失败"} or not fund.get("fund_name"):
                    self.metadata.resolve_one(row["fund_code"])
                    result.names_resolved += 1
                    fund = self.db.row("SELECT * FROM funds WHERE fund_code=?", (row["fund_code"],))
                fund_code = str(row["fund_code"])
                if fund_code not in fee_checked:
                    fee_state, _fee_error = self._sync_missing_fee_schedule(fund_code)
                    fee_checked.add(fund_code)
                    if fee_state == "updated":
                        result.fee_schedules_resolved += 1
                    elif fee_state == "failed":
                        result.fee_schedule_failures += 1
                pricing_date = str(row.get("pricing_date") or row["trade_date"])
                date.fromisoformat(pricing_date)
                navs, url = self.nav_provider.fetch(row["fund_code"], pricing_date, pricing_date)
                nav = float(navs.get(pricing_date) or 0)
                if nav <= 0:
                    self._set_waiting(row["id"], "正式净值尚未发布，请晚些再点；系统不会猜净值。")
                    result.waiting += 1
                    if progress:
                        progress(position, len(rows), f"{row['fund_code']} 等待净值")
                    continue
                amount = float(row.get("planned_amount") or 0)
                action = str(row.get("action") or "")
                fee = 0.0
                units = 0.0
                if action in {"买入", "定投"}:
                    fee_rate = float((fund or {}).get("purchase_fee_pct") or 0)
                    fee, units = calculate_subscription(amount, nav, fee_rate)
                    status = "净值已核对·份额按基金库费率估算"
                    note = "支付宝折扣费率与最终确认份额仍以成交页面为准。"
                elif action in {"卖出", "清仓", "转换转出"}:
                    known_units = float(row.get("planned_units") or row.get("settlement_units") or 0)
                    if known_units > 0:
                        units = known_units
                        gross_estimate = round(units * nav, 2)
                        status = "净值已核对·保留已填卖出份额"
                        note = (
                            f"已保留用户填写的 {units:.2f} 份；按正式净值估算赎回毛额"
                            f"约¥{gross_estimate:.2f}。赎回费和最终净到账额仍以支付宝确认为准。"
                        )
                    elif amount > 0:
                        units = round(amount / nav, 2)
                        status = "净值已核对·按目标金额反推卖出份额（估算）"
                        note = "未填写卖出份额，暂按目标金额反推；赎回费和最终净到账额仍以支付宝确认为准。"
                    else:
                        units = 0.0
                        status = "净值已核对·等待卖出份额"
                        note = "净值已取得，但卖出份额和到账金额均未知，未自动猜测。"
                else:
                    status = "净值已核对·其他操作待人工确认"
                    note = "其他操作不自动推导份额。"
                now = local_now().isoformat(timespec="seconds")
                self.db.execute(
                    """UPDATE decision_drafts SET nav_date=?,settlement_nav=?,settlement_fee=?,
                       settlement_units=?,planned_units=?,settlement_status=?,settlement_source_url=?,
                       settlement_checked_at=?,result_note=?,updated_at=? WHERE id=?""",
                    (pricing_date, nav, fee, units, units, status, url, now, note, now, row["id"]),
                )
                result.navs_resolved += 1
            except Exception as exc:
                self._set_waiting(row["id"], f"联网补全失败：{exc}", failed=True)
                result.failed += 1
            if progress:
                progress(position, len(rows), f"{row['fund_code']} {row['action']}")
        return result

    def _set_waiting(self, draft_id: int, note: str, failed: bool = False):
        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """UPDATE decision_drafts SET settlement_status=?,result_note=?,settlement_checked_at=?,updated_at=?
               WHERE id=?""",
            ("联网失败·可重试" if failed else "等待正式净值", note, now, now, draft_id),
        )

    def enrich_trade(self, trade_id: int) -> dict[str, Any]:
        """Fill a pending trade from its published pricing-day NAV.

        The result deliberately remains ``待确认``: public NAV and a published
        fee schedule can produce an estimate, but only the platform's final
        confirmation proves the actual fee and shares.
        """
        row = self.db.row("SELECT * FROM trades WHERE id=?", (int(trade_id),))
        if not row:
            raise InvestableDataError("找不到这笔交易。")
        posted = self.db.row(
            "SELECT 1 AS found FROM ledger_postings WHERE trade_id=?", (int(trade_id),)
        )
        if posted or str(row.get("status") or "") != "待确认":
            raise InvestableDataError(
                "联网补全只允许处理从未入账的待确认交易；已确认或已审计撤销的原记录必须保持不变。"
            )
        fund = self.db.row("SELECT * FROM funds WHERE fund_code=?", (row["fund_code"],))
        if not fund or fund.get("name_status") in {"待自动识别", "识别失败"}:
            self.metadata.resolve_one(row["fund_code"])
            fund = self.db.row("SELECT * FROM funds WHERE fund_code=?", (row["fund_code"],))
        fee_state, fee_error = self._sync_missing_fee_schedule(str(row["fund_code"]))
        pricing_date = str(row.get("nav_date") or row["trade_date"])
        try:
            pricing_date = date.fromisoformat(pricing_date).isoformat()
        except ValueError as exc:
            raise InvestableDataError("交易日必须是 YYYY-MM-DD。") from exc
        navs, url = self.nav_provider.fetch(row["fund_code"], pricing_date, pricing_date)
        nav = float(navs.get(pricing_date) or 0)
        if nav <= 0:
            raise InvestableDataError("该交易日的正式净值尚未发布，请晚些再点；原记录没有被修改。")
        amount = float(row.get("amount") or 0)
        action = str(row.get("action") or "")
        fee = float(row.get("fee") or 0)
        units_basis = "unknown"
        gross_estimate = None
        if action in {"买入", "定投", "转换转入"}:
            known_units = float(row.get("units") or 0)
            known_fee = float(row.get("fee") or 0)
            if known_units > 0:
                units = known_units
                fee = known_fee
                units_basis = "recorded"
                estimate_note = "公开净值已核对；保留用户已填写的实际份额和费用"
            else:
                fee_rate = float((fund or {}).get("purchase_fee_pct") or 0)
                fee, units = calculate_subscription(amount, nav, fee_rate)
                estimate_note = "公开净值＋基金库费率估算"
                units_basis = "estimated_from_amount"
        elif action in {"卖出", "清仓", "转换转出"}:
            known_units = float(row.get("units") or 0)
            if known_units > 0:
                units = known_units
                units_basis = "recorded"
                gross_estimate = round(units * nav, 2)
                estimate_note = (
                    f"已保留用户填写的卖出份额；按公开净值估算赎回毛额约¥{gross_estimate:.2f}；"
                    "赎回费和最终净到账额待平台确认"
                )
            elif amount > 0:
                units = round(amount / nav, 2)
                units_basis = "estimated_from_amount"
                gross_estimate = round(units * nav, 2)
                estimate_note = "未填写卖出份额，按目标金额和公开净值反推估算；未扣赎回费"
            else:
                units = 0.0
                estimate_note = "仅取得公开净值；卖出份额和净到账额待人工确认"
        else:
            units = float(row.get("units") or 0)
            estimate_note = "仅取得公开净值；份额待人工确认"
        now = local_now().isoformat(timespec="seconds")
        source_parts = list(dict.fromkeys(
            part.strip() for part in str(row.get("source") or "").split("；")
            if part.strip()
        ))
        for note_part in (part.strip() for part in estimate_note.split("；")):
            if note_part and note_part not in source_parts:
                source_parts.append(note_part)
        source = "；".join(source_parts)
        self.db.execute(
            """UPDATE trades SET units=?,nav=?,fee=?,nav_date=?,source_url=?,source=?,status='待确认'
               WHERE id=?""",
            (units, nav, fee, pricing_date, url, source, row["id"]),
        )
        return {
            "trade_id": row["id"], "fund_code": row["fund_code"], "nav_date": pricing_date,
            "nav": nav, "fee": fee, "units": units, "source_url": url,
            "action": action, "units_basis": units_basis, "gross_estimate": gross_estimate,
            "fee_schedule_status": fee_state, "fee_schedule_error": fee_error,
        }

    def enrich_pending_trades(
        self,
        trade_ids: list[int] | None = None,
        *,
        stop_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> PendingTradeEnrichmentResult:
        """Best-effort public-NAV fill for pending trades; never confirms them."""
        params: list[Any] = []
        where = "status='待确认'"
        if trade_ids:
            placeholders = ",".join("?" for _ in trade_ids)
            where += f" AND id IN ({placeholders})"
            params.extend(int(value) for value in trade_ids)
        rows = self.db.rows(
            f"SELECT id,fund_code FROM trades WHERE {where} ORDER BY trade_date,id",
            params,
        )
        resolved = waiting = failed = 0
        errors: list[str] = []
        stopped = False
        checked = 0
        for position, row in enumerate(rows, 1):
            if stop_requested and stop_requested():
                stopped = True
                break
            checked += 1
            try:
                self.enrich_trade(int(row["id"]))
                resolved += 1
            except InvestableDataError as exc:
                message = str(exc)
                if "正式净值尚未发布" in message:
                    waiting += 1
                else:
                    failed += 1
                    errors.append(f"交易#{row['id']} {row['fund_code']}：{message}")
            except Exception as exc:
                failed += 1
                errors.append(f"交易#{row['id']} {row['fund_code']}：{exc}")
            if progress:
                progress(position, len(rows), f"#{row['id']} {row['fund_code']}")
        return PendingTradeEnrichmentResult(
            checked=checked, resolved=resolved, waiting=waiting, failed=failed,
            stopped=stopped, errors=tuple(errors),
        )
