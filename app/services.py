from __future__ import annotations

import json
import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any

from .config import local_now
from .platform_catalog import PlatformCatalogService
from .portfolio import active_capital_summary
from .ranking import RANKING_FORMULA_VERSION
from .recommendation_performance import capture_recommendation_observations
from .universe import UNIVERSE_VERSION
from .trading_calendar import ChinaTradingCalendar


def _current_ranking_policy_snapshot(db) -> tuple[str, str]:
    # This snapshot is consulted by every current-run refresh.  Fetch all
    # settings once instead of opening one SQLite connection per field (the
    # previous implementation added fourteen round trips to a single page
    # refresh).
    settings = db.settings()

    def setting(key: str, default: str) -> str:
        return str(settings.get(key, default))

    version = setting("ranking_policy_version", RANKING_FORMULA_VERSION).strip()
    policy = {
        "trend_weight": float(setting("ranking_trend_weight", "0.25")),
        "entry_weight": float(setting("ranking_entry_weight", "0.25")),
        "catalyst_weight": float(setting("ranking_catalyst_weight", "0.20")),
        "relative_strength_weight": float(setting("ranking_relative_strength_weight", "0.15")),
        "volume_breadth_weight": float(setting("ranking_volume_breadth_weight", "0.15")),
        "crowding_penalty": float(setting("ranking_crowding_penalty", "0.45")),
        "data_confidence_min": float(setting("ranking_data_confidence_min", "70")),
        "candidate_min_score": float(setting("ranking_candidate_min_score", "45")),
        "pool_min": int(float(setting("ranking_pool_min", "20"))),
        "pool_max": int(float(setting("ranking_pool_max", "30"))),
        "pool_cluster_max": int(float(setting("ranking_pool_cluster_max", "4"))),
        "display_cluster_max": int(float(setting("ranking_display_cluster_max", "2"))),
        "funded_cluster_max": int(float(setting("ranking_funded_cluster_max", "1"))),
        "version": version,
    }
    return version, json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stored_protocol_version(raw_json: Any) -> str:
    """Read a stored run marker without trusting user-controlled JSON."""
    try:
        payload = json.loads(str(raw_json or "{}"))
    except (TypeError, json.JSONDecodeError):
        return ""
    return str(payload.get("protocol_version") or "").strip() if isinstance(payload, dict) else ""


def _archive_legacy_v2_payload(
    db,
    payload: dict[str, Any],
    raw_text: str,
    warnings: list[str] | None,
) -> dict[str, Any]:
    """Archive a V2 result while guaranteeing that it can never execute.

    V2 lacked the independent seven-level/timing/execution states and frozen
    four-dimensional policy.  Its raw JSON remains useful historical evidence,
    but importing it must not create an actionable job, candidate rows, audit
    rows, holding actions, or allocation advice.  A V2 payload with the same
    task id as an existing V3 run is recorded only in ``ai_imports`` and never
    overwrites or deletes the V3 run.
    """
    now = local_now().isoformat(timespec="seconds")
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("V2只读归档缺少task_id。")
    status = "通过（V2只读归档）"
    detail = "旧V2结果已按只读历史归档；未生成任务、候选、行动或审计落库。"
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT raw_json,ranking_policy_version FROM radar_runs WHERE task_id=?",
            (task_id,),
        ).fetchone()
        existing_job = conn.execute(
            "SELECT ranking_policy_version FROM analysis_jobs WHERE task_id=?",
            (task_id,),
        ).fetchone()
        existing_is_v3 = bool(
            (
                existing
                and (
                    _stored_protocol_version(existing["raw_json"]) == "3.0"
                    or str(existing["ranking_policy_version"] or "").strip()
                )
            )
            or (
                existing_job
                and str(existing_job["ranking_policy_version"] or "").strip()
            )
        )
        if existing_is_v3:
            status = "通过（V2冲突只读留痕）"
            detail = "同task_id已有V3研究；V2仅写入导入日志，未覆盖V3任何数据。"
        else:
            # Keep one searchable run header plus the complete raw artifact.
            # There are deliberately no derived rows: interpreting V2 as V3 is
            # exactly the unsafe state this migration is intended to prevent.
            conn.execute(
                """INSERT INTO radar_runs(
                       task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                       ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at)
                   VALUES(?,?,?,?,?,'','{}',?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     data_cutoff=excluded.data_cutoff,
                     market_regime=excluded.market_regime,
                     universe_total=excluded.universe_total,
                     analyzed_total=excluded.analyzed_total,
                     ranking_policy_version='',ranking_policy_json='{}',
                     summary=excluded.summary,raw_json=excluded.raw_json,
                     imported_at=excluded.imported_at""",
                (
                    task_id,
                    str(payload.get("data_cutoff") or ""),
                    str(payload.get("market_regime") or "历史V2"),
                    int(payload.get("universe_total") or 0),
                    int(payload.get("analyzed_total") or 0),
                    str(payload.get("market_summary") or "V2只读历史"),
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            # Sanitize rows left by pre-v27 importers with the same V2 task id.
            # No corresponding V3 run can reach this branch.
            conn.execute("DELETE FROM analysis_jobs WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM candidates WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM radar_audit WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM holding_recommendations WHERE task_id=?", (task_id,))
            conn.execute(
                """UPDATE radar_runs
                      SET action_summary='',no_buy_reason='',action_json='{}',action_updated_at=''
                    WHERE task_id=?""",
                (task_id,),
            )
        errors = "\n".join([*(warnings or []), detail])
        conn.execute(
            "INSERT INTO ai_imports(imported_at,task_id,validation_status,errors,raw_text) VALUES (?,?,?,?,?)",
            (now, task_id, status, errors, raw_text),
        )
    return {"task_id": task_id, "read_only": True, "stored_run": not existing_is_v3}


_STALE_CAPITAL_CLAIM = re.compile(
    r"(?:(?:当前)?(?:主动)?可用资金|剩余资金|主动资金可用额度)"
    r"\s*(?:(?:仅剩|剩余|约为|为|约|还有|只剩|[:：])\s*)*"
    r"[¥￥]?\s*\d[\d,]*(?:\.\d+)?\s*元?",
    re.IGNORECASE,
)


def current_capital_snapshot(db) -> dict[str, float]:
    """Return the one authoritative active-capital snapshot stored with AI actions."""
    summary = active_capital_summary(db)
    snapshot = {
        "limit": round(float(summary["limit"]), 2),
        "used": round(float(summary.get("market_exposure", summary.get("used", 0))), 2),
        "available": round(float(summary.get("cash_available", summary.get("available", 0))), 2),
        "market_exposure": round(float(summary.get("market_exposure", summary.get("used", 0))), 2),
        "committed_cost": round(float(summary.get("committed_cost", 0)), 2),
        "cash_available": round(float(summary.get("cash_available", summary.get("available", 0))), 2),
        "cash_before_reservations": round(float(summary.get("cash_before_reservations", 0)), 2),
        "reserved": round(float(summary.get("reserved", 0)), 2),
    }
    return snapshot


def _catalog_snapshot_ids(db) -> list[str]:
    """Return the exact ordered, currently investable scan catalog.

    The old implementation returned a compile-time list of 100 research rows.
    That made an archived 100-item result look current even after the executable
    catalog had deliberately shrunk.  Policy version plus this ordered ID hash now
    isolate every membership change without rewriting historical records.
    """
    return [
        str(row["index_code"])
        for row in PlatformCatalogService(db).radar_universe(refresh_admission=False)
    ]


def _catalog_snapshot_hash(db, values: list[str]) -> str:
    """Hash both index membership and the exact strict-C execution whitelist."""
    catalog = PlatformCatalogService(db)
    index_by_universe: dict[str, int] = {}
    if values:
        marks = ",".join("?" for _ in values)
        index_by_universe = {
            str(row["universe_id"]): int(row["id"])
            for row in db.rows(
                f"""SELECT id,universe_id FROM investable_indices
                      WHERE active=1 AND catalog_role='日扫代表'
                        AND universe_id IN ({marks})""",
                values,
            )
        }
    strict_by_index = catalog.strict_catalog_funds_by_index(
        list(index_by_universe.values())
    )
    manifest = []
    for universe_id in values:
        index_id = index_by_universe.get(str(universe_id))
        codes = [] if index_id is None else sorted(
            str(fund.get("fund_code") or "")
            for fund in strict_by_index.get(index_id, [])
            if str(fund.get("fund_code") or "")
        )
        manifest.append({"index_id": str(universe_id), "strict_c_funds": codes})
    payload = {
        "field_algorithm": db.setting("csi_fund_sync_algorithm_version", ""),
        "items": manifest,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _compatible_radar_runs(
    db, *, require_today: bool = True,
    expected_ids: list[str] | None = None,
    expected_hash: str | None = None,
    policy_snapshot: tuple[str, str] | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return complete runs for today's exact catalog/policy, newest first.

    Freshness is intentionally not decided here.  Callers must explicitly choose
    between an actionable run and a read-only publication-lagged run.
    """
    expected_ids = _catalog_snapshot_ids(db) if expected_ids is None else expected_ids
    expected_hash = (
        _catalog_snapshot_hash(db, expected_ids)
        if expected_hash is None else expected_hash
    )
    clock = as_of or local_now()
    today = clock.date().isoformat()
    policy_version, policy_json = (
        _current_ranking_policy_snapshot(db)
        if policy_snapshot is None else policy_snapshot
    )
    rows = db.rows(
        """SELECT r.*,j.mode,j.stage AS job_stage,j.status AS job_status,
                  j.universe_version,j.research_date,j.universe_ids_json,j.universe_hash,
                  j.ranking_policy_version AS job_ranking_policy_version,
                  j.ranking_policy_json AS job_ranking_policy_json
             FROM radar_runs r JOIN analysis_jobs j ON j.task_id=r.task_id
            WHERE j.mode='完整扫描' AND j.status='已完成'
            ORDER BY r.imported_at DESC"""
    )
    compatible = []
    for row in rows:
        if str(row.get("universe_version") or "") != UNIVERSE_VERSION:
            continue
        if require_today and str(row.get("research_date") or "") != today:
            continue
        if str(row.get("universe_hash") or "") != expected_hash:
            continue
        if str(row.get("ranking_policy_version") or "") != policy_version:
            continue
        if str(row.get("job_ranking_policy_version") or "") != policy_version:
            continue
        if str(row.get("ranking_policy_json") or "") != policy_json:
            continue
        if str(row.get("job_ranking_policy_json") or "") != policy_json:
            continue
        try:
            values = json.loads(row.get("universe_ids_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if values != expected_ids:
            continue
        if int(row.get("universe_total") or 0) != len(expected_ids):
            continue
        if int(row.get("analyzed_total") or 0) != len(expected_ids):
            continue
        compatible.append(row)
    return compatible


def latest_current_radar_run(
    db, *, require_today: bool = True, as_of: datetime | None = None,
) -> dict[str, Any] | None:
    """Return only a fresh, complete and currently actionable research run.

    Historical, partial and publication-lagged runs can never silently become
    actionable merely because they have the newest timestamp.
    """
    clock = as_of or local_now()
    expected_cutoff = (
        ChinaTradingCalendar.from_db(db)
        .latest_completed_trading_day(clock)
        .isoformat()
    )
    return next(
        (
            row for row in _compatible_radar_runs(
                db, require_today=require_today, as_of=clock,
            )
            if str(row.get("data_cutoff") or "")[:10] == expected_cutoff
        ),
        None,
    )


def latest_readable_radar_run(db, *, require_today: bool = True) -> dict[str, Any] | None:
    """Return today's complete research even while official bars are one day late.

    This function is for read-only pages only.  Execution services must continue
    to call :func:`latest_current_radar_run`.
    """
    calendar = ChinaTradingCalendar.from_db(db)
    expected_day = calendar.latest_completed_trading_day(local_now())
    allowed = {
        expected_day.isoformat(),
        calendar.previous_trading_day(expected_day).isoformat(),
    }
    return next(
        (
            row for row in _compatible_radar_runs(db, require_today=require_today)
            if str(row.get("data_cutoff") or "")[:10] in allowed
        ),
        None,
    )


def research_run_status(db) -> dict[str, Any]:
    """Summarise actionable and archived research without mixing the two.

    A blank current-research page must explain *which* compatibility gate failed.
    The previous generic message mentioned only old 186/66 item pools, which hid
    the much more common upgrade case: the latest complete run can use the exact
    same catalog but an older ranking formula.  Keep the old run read-only and
    expose a concise migration diagnosis instead of making it look as if the
    scan disappeared.
    """
    expected_ids = _catalog_snapshot_ids(db)
    expected_total = len(expected_ids)
    expected_hash = _catalog_snapshot_hash(db, expected_ids)
    policy_version, policy_json = _current_ranking_policy_snapshot(db)
    compatible = _compatible_radar_runs(
        db,
        expected_ids=expected_ids,
        expected_hash=expected_hash,
        policy_snapshot=(policy_version, policy_json),
    )
    calendar = ChinaTradingCalendar.from_db(db)
    expected_day = calendar.latest_completed_trading_day(local_now())
    current = next(
        (
            row for row in compatible
            if str(row.get("data_cutoff") or "")[:10] == expected_day.isoformat()
        ),
        None,
    )
    readable_days = {
        expected_day.isoformat(),
        calendar.previous_trading_day(expected_day).isoformat(),
    }
    research = current or next(
        (
            row for row in compatible
            if str(row.get("data_cutoff") or "")[:10] in readable_days
        ),
        None,
    )
    total = int((db.row("SELECT COUNT(*) AS n FROM radar_runs") or {}).get("n") or 0)
    read_only = bool(research and not current)
    latest_complete = db.row(
        """SELECT r.task_id,r.data_cutoff,r.universe_total,r.analyzed_total,r.imported_at,
                  r.ranking_policy_version,j.research_date,j.universe_version,
                  j.ranking_policy_version AS job_ranking_policy_version,
                  j.status AS job_status
             FROM radar_runs r JOIN analysis_jobs j ON j.task_id=r.task_id
            WHERE j.mode='完整扫描' AND j.status='已完成'
            ORDER BY r.imported_at DESC LIMIT 1"""
    )

    latest_summary = None
    if latest_complete:
        latest_summary = {
            "task_id": str(latest_complete.get("task_id") or ""),
            "research_date": str(latest_complete.get("research_date") or ""),
            "data_cutoff": str(latest_complete.get("data_cutoff") or "")[:10],
            "universe_total": int(latest_complete.get("universe_total") or 0),
            "analyzed_total": int(latest_complete.get("analyzed_total") or 0),
            "ranking_policy_version": str(
                latest_complete.get("ranking_policy_version")
                or latest_complete.get("job_ranking_policy_version")
                or "未记录"
            ),
        }

    missing_reason = ""
    if not research:
        if latest_summary:
            latest_policy = latest_summary["ranking_policy_version"]
            completed = latest_summary["analyzed_total"]
            latest_total = latest_summary["universe_total"]
            latest_date = latest_summary["research_date"] or "日期未记录"
            prefix = (
                f"最近一次完整成功研究是 {latest_date} 的 {completed}/{latest_total} 项，"
                f"使用 {latest_policy}。"
            )
            if latest_policy != policy_version:
                missing_reason = (
                    prefix
                    + f"当前软件要求 {policy_version}，旧策略结果已隔离为只读；"
                    "需要按当前策略重新完成一次完整扫描。"
                )
            elif latest_date != local_now().date().isoformat():
                missing_reason = (
                    prefix
                    + "它不是今天的研究，不能沿用为今日行动；需要先更新行情，再新建今日完整扫描。"
                )
            elif latest_total != expected_total or completed != expected_total:
                missing_reason = (
                    prefix
                    + f"当前严格C类目录为 {expected_total} 项，目录成员或基金白名单已经变化；"
                    "旧结果不能生成行动，需要新建完整扫描。"
                )
            else:
                missing_reason = (
                    prefix
                    + "其目录、策略或行情截止日未通过当前严格校验，已隔离为只读；"
                    "请更新数据后新建完整扫描。"
                )
        else:
            missing_reason = (
                f"尚无今天当前{expected_total}项目录的完整成功研究；"
                "旧186项、旧66项、跨日和部分结果均已隔离为只读历史。"
            )
    return {
        "current": current,
        "research": research,
        "actionable": bool(current),
        "read_only": read_only,
        "read_only_reason": (
            "中证官网日线仍在分批发布：本轮研究可阅读，但行情不是最近已完成交易日；"
            "所有买入金额为0，不能加入AI草稿。"
            if read_only else ""
        ),
        "archived_count": max(0, total - (1 if research else 0)),
        "reason": "" if research else missing_reason,
        "current_policy_version": policy_version,
        "current_catalog_total": expected_total,
        "latest_complete": latest_summary,
        "requires_new_scan": not bool(research),
    }


def action_capital_context(run: dict[str, Any] | None, metrics: dict[str, Any]) -> dict[str, Any]:
    """Compare a saved AI action's capital snapshot with the live portfolio.

    Legacy actions did not save a snapshot and therefore cannot safely claim that a
    particular amount of capital is available.  They are deliberately treated as stale.
    """
    current = {
        "limit": round(float(metrics.get("active_limit") or 0), 2),
        "used": round(float(metrics.get("active_market_exposure", metrics.get("active_value")) or 0), 2),
        "available": round(float(metrics.get("active_cash_available", metrics.get("active_available")) or 0), 2),
    }
    if not run:
        return {"current": current, "saved": None, "stale": False, "reason": ""}
    try:
        payload = json.loads(run.get("action_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    saved = payload.get("capital_snapshot")
    if not isinstance(saved, dict) or not all(key in saved for key in ("limit", "used", "available")):
        return {
            "current": current, "saved": None, "stale": True,
            "reason": "这份AI行动没有保存主动资金快照，不能继续沿用其中的资金数字。",
        }
    try:
        normalized = {key: round(float(saved[key]), 2) for key in ("limit", "used", "available")}
    except (TypeError, ValueError):
        return {
            "current": current, "saved": None, "stale": True,
            "reason": "这份AI行动的主动资金快照无效。",
        }
    stale = any(abs(normalized[key] - current[key]) >= 0.01 for key in current)
    return {
        "current": current,
        "saved": normalized,
        "stale": stale,
        "reason": "主动持仓或可用额度已变化，旧AI金额必须重新计算。" if stale else "",
    }


def remove_stale_capital_claim(text: str) -> str:
    """Remove a numerical capital premise that no longer matches the live ledger."""
    cleaned = _STALE_CAPITAL_CLAIM.sub("", str(text or ""))
    cleaned = re.sub(r"^[\s，,；;。、]*(?:且|并且|同时)?[\s，,；;。、]*", "", cleaned)
    return cleaned.strip() or "原结论依赖已经失效的资金数据，需要重新计算。"


def strategy_review_metrics(trades: list[dict[str, Any]], active_capital: float) -> dict[str, Any]:
    """Compute reconciled exit statistics without inventing portfolio drawdown.

    A sequence of realised exits added to an arbitrary capital limit is not an
    equity curve: it omits open-position NAV changes, deposits and withdrawals.
    Keep that diagnostic under an explicit name and leave portfolio drawdown
    unknown here.  ``portfolio_equity_drawdown`` is the sole source for the UI's
    real drawdown metric.
    """
    known = [row for row in trades if row.get("result_pnl") is not None]
    pnls = [float(row["result_pnl"]) for row in known]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    equity = peak = float(active_capital)
    realized_sequence_drawdown_pct = 0.0
    cumulative = []
    running = 0.0
    for value in pnls:
        running += value
        cumulative.append(running)
        equity = float(active_capital) + running
        peak = max(peak, equity)
        if peak > 0:
            realized_sequence_drawdown_pct = max(
                realized_sequence_drawdown_pct, (peak - equity) / peak * 100
            )
    return {
        "trade_count": len(known),
        "unknown_trade_count": len(trades) - len(known),
        "amount_total": sum(float(row.get("amount") or 0) for row in trades),
        "pnl_total": sum(pnls),
        "win_rate_pct": len(wins) / len(known) * 100 if known else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0),
        "max_drawdown_pct": None,
        "realized_sequence_drawdown_pct": realized_sequence_drawdown_pct,
        "cumulative": cumulative,
    }


def daily_portfolio_equity(db) -> dict[str, Any]:
    """Rebuild end-of-NAV-day tactical equity from reconciled cash and units.

    The first trustworthy cash-ledger anchor (or the latest later structural
    correction) starts one measurement epoch.  Confirmed buy/sell cash events
    are replayed on their pricing dates and units are valued only with an exact,
    sourced NAV for that same date.  A missing NAV skips the whole day instead
    of carrying yesterday's value forward.  Legacy exits that never touched the
    cash ledger remain available to holding-segment P/L, but cannot contaminate
    the portfolio drawdown curve.
    """
    anchor = db.row(
        """SELECT * FROM active_cash_ledger
            WHERE event_kind IN ('OPENING_BALANCE','HOLDING_RECLASSIFICATION','POSTING_REVERSAL')
            ORDER BY id DESC LIMIT 1"""
    )
    if not anchor:
        return {
            "series": [], "candidate_days": 0, "complete_days": 0,
            "coverage_pct": None, "missing_days": [], "start_date": "", "end_date": "",
            "reason": "尚未建立可核对的主动现金账本起点。",
        }
    try:
        opening_cash = float(anchor.get("balance_after"))
    except (TypeError, ValueError):
        return {
            "series": [], "candidate_days": 0, "complete_days": 0,
            "coverage_pct": None, "missing_days": [], "start_date": "", "end_date": "",
            "reason": "主动现金账本起点损坏。",
        }
    anchor_id = int(anchor["id"])
    anchor_day = _review_day(anchor.get("created_at"))
    cash_events = db.rows(
        """SELECT c.*,t.trade_date,t.nav_date,t.fund_code,t.units,t.nav,t.source_url,
                  t.status,t.counts_toward_active_capital,p.posting_kind,p.reversed_at
             FROM active_cash_ledger c
             LEFT JOIN trades t ON t.id=c.trade_id
             LEFT JOIN ledger_postings p ON p.trade_id=c.trade_id
            WHERE c.id>? ORDER BY c.id""",
        (anchor_id,),
    )
    supported = {"CONFIRMED_BUY", "CONFIRMED_SELL", "CAPITAL_LIMIT_ADJUSTMENT"}
    unsupported = [row for row in cash_events if str(row.get("event_kind") or "") not in supported]
    if unsupported:
        return {
            "series": [], "candidate_days": 0, "complete_days": 0,
            "coverage_pct": None, "missing_days": [], "start_date": anchor_day, "end_date": "",
            "reason": "现金账本起点后仍有结构性更正，无法安全重建同一测量区间。",
        }

    trade_events: list[dict[str, Any]] = []
    flow_events: list[dict[str, Any]] = []
    invalid_reasons: list[str] = []
    for raw in cash_events:
        row = dict(raw)
        kind = str(row.get("event_kind") or "")
        if kind == "CAPITAL_LIMIT_ADJUSTMENT":
            day = _review_day(row.get("created_at"))
            if not day:
                invalid_reasons.append(f"现金流水#{int(row['id'])}缺少有效日期")
                continue
            flow_events.append({
                "day": day, "cash_event_id": int(row["id"]),
                "cash_delta": float(row.get("amount") or 0),
                "external_delta": float(row.get("amount") or 0),
            })
            continue
        if not row.get("trade_id") or not row.get("posting_kind"):
            invalid_reasons.append(f"现金流水#{int(row['id'])}缺少统一交易入账记录")
            continue
        if str(row.get("reversed_at") or "") or str(row.get("status") or "") != "已确认":
            invalid_reasons.append(f"现金流水#{int(row['id'])}对应交易已撤销或未确认")
            continue
        if not int(row.get("counts_toward_active_capital") or 0):
            invalid_reasons.append(f"现金流水#{int(row['id'])}与交易资金分类不一致")
            continue
        day = _review_day(row.get("nav_date") or row.get("trade_date"))
        units = float(row.get("units") or 0)
        if not day or units <= 0:
            invalid_reasons.append(f"交易#{int(row['trade_id'])}缺少有效定价日或份额")
            continue
        sign = 1.0 if str(row["posting_kind"]) == "买入" else -1.0
        trade_events.append({
            "day": day, "cash_event_id": int(row["id"]),
            "trade_id": int(row["trade_id"]), "fund_code": str(row["fund_code"]),
            "unit_delta": sign * units, "cash_delta": float(row.get("amount") or 0),
            "trade_nav": float(row.get("nav") or 0),
            "trade_source_url": str(row.get("source_url") or ""),
        })
    if invalid_reasons:
        return {
            "series": [], "candidate_days": 0, "complete_days": 0,
            "coverage_pct": None, "missing_days": [], "start_date": anchor_day, "end_date": "",
            "reason": "；".join(invalid_reasons[:8]),
        }

    current_rows = db.rows(
        """SELECT fund_code,SUM(units) AS units
             FROM holdings
            WHERE active=1 AND COALESCE(counts_toward_active_capital,0)=1
            GROUP BY fund_code"""
    )
    current_units = {str(row["fund_code"]): float(row.get("units") or 0) for row in current_rows}
    fund_codes = set(current_units)
    fund_codes.update(event["fund_code"] for event in trade_events)
    initial_units = {code: float(current_units.get(code, 0)) for code in fund_codes}
    for event in trade_events:
        initial_units[event["fund_code"]] -= float(event["unit_delta"])
    tolerance = 1e-5
    negative = {code: units for code, units in initial_units.items() if units < -tolerance}
    if negative:
        detail = "、".join(f"{code}:{units:.4f}" for code, units in sorted(negative.items()))
        return {
            "series": [], "candidate_days": 0, "complete_days": 0,
            "coverage_pct": None, "missing_days": [], "start_date": anchor_day, "end_date": "",
            "reason": f"现金账本起点的份额无法反向对账（{detail}）。",
        }
    for code in initial_units:
        if abs(initial_units[code]) <= tolerance:
            initial_units[code] = 0.0

    all_events = sorted(
        [*trade_events, *flow_events], key=lambda row: (row["day"], row["cash_event_id"]),
    )
    dated = [row["day"] for row in all_events if row.get("day")]
    start_day = min([value for value in [anchor_day, *dated] if value], default="")
    if not start_day:
        return {
            "series": [], "candidate_days": 0, "complete_days": 0,
            "coverage_pct": None, "missing_days": [], "start_date": "", "end_date": "",
            "reason": "现金账本起点没有有效日期。",
        }

    nav_by_fund: dict[str, dict[str, tuple[float, str, str]]] = {code: {} for code in fund_codes}
    if fund_codes:
        marks = ",".join("?" for _ in fund_codes)
        nav_rows = db.rows(
            f"""SELECT fund_code,nav_date,nav,source,source_url
                  FROM fund_nav_history
                 WHERE fund_code IN ({marks}) AND nav_date>=?
                 ORDER BY nav_date,fund_code""",
            (*sorted(fund_codes), start_day),
        )
        for row in nav_rows:
            day = _review_day(row.get("nav_date"))
            nav = float(row.get("nav") or 0)
            source = str(row.get("source") or "")
            source_url = str(row.get("source_url") or "")
            if day and nav > 0 and source and source_url:
                nav_by_fund[str(row["fund_code"])][day] = (nav, source, source_url)
    # User-confirmed transactions are also exact same-day valuation evidence.
    for event in trade_events:
        if event["trade_nav"] > 0:
            nav_by_fund[event["fund_code"]].setdefault(
                event["day"],
                (event["trade_nav"], f"已确认交易#{event['trade_id']}", event["trade_source_url"]),
            )

    # Coverage must be measured against the exchange calendar, not merely the
    # dates that happened to arrive from a provider.  Otherwise a day on which
    # every provider failed would disappear from both the curve and the
    # missing-data count.  This curve is limited to the last completed A-share
    # session; an intraday/incomplete NAV is never treated as a closing value.
    calendar = ChinaTradingCalendar.from_db(db)
    latest_completed = calendar.latest_completed_trading_day(local_now())
    first_day = date.fromisoformat(start_day)
    candidate_dates: set[str] = set()
    cursor = first_day
    while cursor <= latest_completed:
        if calendar.is_trading_day(cursor):
            candidate_dates.add(cursor.isoformat())
        cursor += timedelta(days=1)
    future_events = sorted({
        row["day"] for row in all_events
        if row.get("day") and date.fromisoformat(row["day"]) > latest_completed
    })
    if future_events:
        return {
            "series": [], "candidate_days": len(candidate_dates), "complete_days": 0,
            "coverage_pct": None, "missing_days": [], "start_date": start_day,
            "end_date": latest_completed.isoformat(),
            "reason": "现金账本含有晚于最近已收盘交易日的定价流水，需先核对："
                      + "、".join(future_events[:8]),
        }
    units_state = dict(initial_units)
    cash = opening_cash
    external_flow = 0.0
    events_by_day: dict[str, list[dict[str, Any]]] = {}
    for event in all_events:
        events_by_day.setdefault(event["day"], []).append(event)
    series: list[dict[str, Any]] = []
    missing_days: list[dict[str, Any]] = []
    for day in sorted(candidate_dates):
        for event in events_by_day.get(day, []):
            cash += float(event.get("cash_delta") or 0)
            external_flow += float(event.get("external_delta") or 0)
            if event.get("fund_code"):
                code = str(event["fund_code"])
                units_state[code] = float(units_state.get(code, 0)) + float(event["unit_delta"])
                if abs(units_state[code]) <= tolerance:
                    units_state[code] = 0.0
                elif units_state[code] < -tolerance:
                    missing_days.append({"date": day, "missing": [f"{code}份额变为负数"]})
                    units_state[code] = 0.0
        open_codes = sorted(code for code, units in units_state.items() if units > tolerance)
        missing = [code for code in open_codes if day not in nav_by_fund.get(code, {})]
        if missing:
            missing_days.append({"date": day, "missing": missing})
            continue
        holdings_value = sum(
            units_state[code] * nav_by_fund[code][day][0] for code in open_codes
        )
        series.append({
            "valuation_date": day, "cash": round(cash, 10),
            "holdings_value": round(holdings_value, 10),
            "total_equity": round(cash + holdings_value, 10),
            "external_flow_total": round(external_flow, 10),
            "open_funds": len(open_codes),
            "fund_units": {code: round(units_state[code], 10) for code in open_codes},
            "nav_sources": {code: nav_by_fund[code][day][1] for code in open_codes},
        })

    expected_cash_row = db.row("SELECT value FROM settings WHERE key='active_cash_balance'") or {}
    try:
        expected_cash = float(expected_cash_row.get("value"))
    except (TypeError, ValueError):
        expected_cash = None
    unit_mismatch = {
        code: (units_state.get(code, 0.0), current_units.get(code, 0.0))
        for code in fund_codes
        if abs(float(units_state.get(code, 0)) - float(current_units.get(code, 0))) > 0.011
    }
    reconciliation_reason = ""
    if expected_cash is None or abs(cash - expected_cash) > 0.011:
        reconciliation_reason = "重建后的现金与当前真实现金余额不一致。"
    if unit_mismatch:
        reconciliation_reason += "重建后的份额与当前持仓不一致。"
    if reconciliation_reason:
        series = []
    candidate_count = len(candidate_dates)
    return {
        "series": series, "candidate_days": candidate_count,
        "complete_days": len(series),
        "coverage_pct": len(series) / candidate_count * 100 if candidate_count else None,
        "missing_days": missing_days, "start_date": start_day,
        "end_date": max(candidate_dates) if candidate_dates else "",
        "reason": reconciliation_reason,
        "anchor_kind": str(anchor.get("event_kind") or ""),
        "anchor_at": str(anchor.get("created_at") or ""),
    }


def _drawdown_from_equity_rows(rows: list[dict[str, Any]], date_key: str) -> dict[str, Any]:
    if len(rows) < 2:
        return {
            "sample_count": len(rows), "max_drawdown_pct": None, "series": rows,
            "peak_at": "", "trough_at": "", "peak_equity": None, "trough_equity": None,
        }
    initial_flow = float(rows[0].get("external_flow_total") or 0)
    peak = None
    peak_at = ""
    maximum_peak_at = ""
    maximum_trough_at = ""
    maximum_peak_equity = None
    maximum_trough_equity = None
    maximum = 0.0
    series = []
    for row in rows:
        adjusted = float(row.get("total_equity") or 0) - (
            float(row.get("external_flow_total") or 0) - initial_flow
        )
        if peak is None or adjusted > peak:
            peak = adjusted
            peak_at = str(row.get(date_key) or "")
        drawdown = ((peak - adjusted) / peak * 100) if peak and peak > 0 else 0.0
        if drawdown > maximum:
            maximum = drawdown
            maximum_peak_at = peak_at
            maximum_trough_at = str(row.get(date_key) or "")
            maximum_peak_equity = peak
            maximum_trough_equity = adjusted
        series.append({**row, "adjusted_equity": adjusted, "drawdown_pct": drawdown})
    return {
        "sample_count": len(rows), "max_drawdown_pct": maximum, "series": series,
        "peak_at": maximum_peak_at, "trough_at": maximum_trough_at,
        "peak_equity": maximum_peak_equity, "trough_equity": maximum_trough_equity,
    }


def portfolio_equity_drawdown(db) -> dict[str, Any]:
    """Prefer exact daily reconstruction; fall back to unique recorded days."""
    daily = daily_portfolio_equity(db)
    daily_rows = list(daily.get("series") or [])
    if len(daily_rows) >= 2:
        result = _drawdown_from_equity_rows(daily_rows, "valuation_date")
        return {
            **result, "basis": "正式净值日线＋主动现金账本",
            "coverage_pct": daily.get("coverage_pct"),
            "candidate_days": daily.get("candidate_days", 0),
            "complete_days": daily.get("complete_days", 0),
            "start_date": daily.get("start_date", ""), "end_date": daily.get("end_date", ""),
            "missing_days": daily.get("missing_days", []), "reason": daily.get("reason", ""),
        }

    # Older databases may not yet have enough exact daily NAV coverage.  Keep
    # one latest observation per date; repeated button clicks on one day are not
    # independent drawdown samples.
    table = db.row(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='portfolio_equity_snapshots'"
    )
    if not table:
        return {
            "sample_count": 0, "max_drawdown_pct": None, "series": [],
            "basis": "样本不足", "coverage_pct": daily.get("coverage_pct"),
            "candidate_days": daily.get("candidate_days", 0),
            "complete_days": daily.get("complete_days", 0), "reason": daily.get("reason", ""),
        }
    rows = db.rows(
        """SELECT id,captured_at,total_equity,external_flow_total
             FROM portfolio_equity_snapshots ORDER BY captured_at,id"""
    )
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = _review_day(row.get("captured_at"))
        if day:
            by_day[day] = {**row, "snapshot_date": day}
    unique_rows = [by_day[day] for day in sorted(by_day)]
    result = _drawdown_from_equity_rows(unique_rows, "snapshot_date")
    return {
        **result, "basis": "已记录组合快照（每日只取最后一次）" if unique_rows else "样本不足",
        "coverage_pct": daily.get("coverage_pct"),
        "candidate_days": daily.get("candidate_days", 0),
        "complete_days": daily.get("complete_days", 0),
        "start_date": daily.get("start_date", ""), "end_date": daily.get("end_date", ""),
        "missing_days": daily.get("missing_days", []),
        "reason": daily.get("reason", "") or (
            "正式净值日线不足2个完整日期，暂用已记录组合快照。" if unique_rows else ""
        ),
    }


def _review_day(value: Any) -> str:
    """Return one valid ISO date or an empty string for legacy/invalid text."""
    raw = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return ""


def _inclusive_calendar_days(start: str, end: str) -> int | None:
    if not start or not end:
        return None
    try:
        return max(1, (date.fromisoformat(end) - date.fromisoformat(start)).days + 1)
    except ValueError:
        return None


def _finish_holding_segment(segment: dict[str, Any], end_date: str) -> dict[str, Any]:
    """Freeze one zero-to-zero holding segment without inventing unknown P/L."""
    result = dict(segment)
    result["end_date"] = end_date
    result["holding_days"] = _inclusive_calendar_days(result.get("start_date", ""), end_date)
    exits = list(result.pop("_exits", []))
    result["exit_trade_ids"] = [int(row["id"]) for row in exits]
    result["exit_details"] = [
        {
            "id": int(row["id"]),
            "trade_date": str(row.get("event_date") or _review_day(row.get("trade_date"))),
            "action": str(row.get("action") or "卖出"),
            "amount": float(row.get("amount") or 0),
            "units": float(row.get("units") or 0),
            "fee": float(row.get("fee") or 0),
            "result_pnl": row.get("result_pnl"),
            "result_pnl_status": str(row.get("result_pnl_status") or ""),
            "source": str(row.get("source") or "手工记录"),
        }
        for row in exits
    ]
    result["exit_count"] = len(exits)
    result["exit_amount"] = sum(float(row.get("amount") or 0) for row in exits)
    result["exit_units"] = sum(float(row.get("units") or 0) for row in exits)
    result["fee_total"] = float(result.get("buy_fee_total") or 0) + sum(
        float(row.get("fee") or 0) for row in exits
    )

    unknown_reasons = list(result.pop("_unknown_reasons", []))
    unknown_exits = [
        row for row in exits
        if row.get("result_pnl") is None or str(row.get("result_pnl_status") or "") != "已核算"
    ]
    if unknown_exits:
        unknown_reasons.append(
            "退出流水中有待核算盈亏：" + "、".join(f"#{int(row['id'])}" for row in unknown_exits)
        )

    canonical_pnl = None
    if exits and not unknown_exits:
        canonical_pnl = sum(float(row["result_pnl"]) for row in exits)
    invested = result.get("invested_amount")
    cashflow_pnl = (
        float(result["exit_amount"]) - float(invested)
        if invested is not None else None
    )
    if canonical_pnl is not None and cashflow_pnl is not None:
        tolerance = max(0.05, abs(canonical_pnl) * 0.001, abs(cashflow_pnl) * 0.001)
        if abs(canonical_pnl - cashflow_pnl) > tolerance:
            unknown_reasons.append(
                f"逐笔已核算盈亏{canonical_pnl:.2f}与完整段净现金流{cashflow_pnl:.2f}不一致"
            )
            canonical_pnl = None

    if unknown_reasons:
        canonical_pnl = None
    result["result_pnl"] = canonical_pnl
    result["cashflow_pnl"] = cashflow_pnl
    result["return_pct"] = (
        canonical_pnl / float(invested) * 100
        if canonical_pnl is not None and invested is not None and float(invested) > 0 else None
    )
    result["pnl_status"] = "已核算" if canonical_pnl is not None and not unknown_reasons else "待核算"
    result["pnl_reason"] = "；".join(dict.fromkeys(reason for reason in unknown_reasons if reason))
    result["is_complete"] = True
    result["remaining_units"] = 0.0
    return result


def holding_segment_review(db) -> dict[str, Any]:
    """Rebuild complete active-strategy holding segments from immutable postings.

    One segment starts when a fund moves from zero units to a positive holding
    and ends only when its units return to zero.  Adds and partial exits stay
    inside that same segment and therefore cannot inflate the win rate.  Actual
    buy cash outflow and sale net receipt already include the user's confirmed
    fees; fees are never subtracted a second time here.
    """
    postings = db.rows(
        """SELECT t.*,p.posting_kind,p.posting_seq,p.metadata_json,
                  COALESCE(f.fund_name,'未知基金') AS fund_name
             FROM ledger_postings p
             JOIN trades t ON t.id=p.trade_id
             LEFT JOIN funds f USING(fund_code)
            WHERE COALESCE(p.reversed_at,'')=''
              AND COALESCE(t.status,'已确认')='已确认'
              AND COALESCE(t.counts_toward_active_capital,0)=1
              AND p.posting_kind IN ('买入','卖出')
            ORDER BY t.fund_code,p.posting_seq,t.id"""
    )
    active_holdings = db.rows(
        """SELECT h.*,COALESCE(f.fund_name,'未知基金') AS fund_name
             FROM holdings h LEFT JOIN funds f USING(fund_code)
            WHERE h.active=1 AND COALESCE(h.counts_toward_active_capital,0)=1
            ORDER BY h.fund_code,h.id"""
    )
    fund_scope = {str(row["fund_code"]) for row in postings}
    fund_scope.update(str(row["fund_code"]) for row in active_holdings)
    orphan_lots = []
    if fund_scope:
        placeholders = ",".join("?" for _ in fund_scope)
        orphan_lots = db.rows(
            f"""SELECT l.*,COALESCE(f.fund_name,'未知基金') AS fund_name
                  FROM cost_lots l
                  LEFT JOIN ledger_postings p ON p.trade_id=l.trade_id
                  LEFT JOIN funds f USING(fund_code)
                 WHERE l.fund_code IN ({placeholders})
                   AND (l.trade_id IS NULL OR p.trade_id IS NULL)
                 ORDER BY l.fund_code,COALESCE(NULLIF(l.pricing_date,''),l.confirmed_date),l.id""",
            tuple(sorted(fund_scope)),
        )

    holding_starts: dict[str, str] = {}
    for row in db.rows(
        """SELECT fund_code,MIN(confirmed_date) AS first_date
             FROM holdings WHERE COALESCE(counts_toward_active_capital,0)=1
            GROUP BY fund_code"""
    ):
        holding_starts[str(row["fund_code"])] = _review_day(row.get("first_date"))

    events_by_fund: dict[str, list[dict[str, Any]]] = {}
    names: dict[str, str] = {}
    for lot in orphan_lots:
        code = str(lot["fund_code"])
        source = str(lot.get("source") or "")
        amount = float(lot.get("gross_amount") or 0)
        cost_known = amount > 0 or any(marker in source for marker in ("赠送", "gift", "Gift"))
        lot_day = _review_day(lot.get("pricing_date") or lot.get("confirmed_date"))
        # A zero-cost gift can have a reliable cost but an unknown acquisition
        # date.  Do not turn a placeholder migration date into a fake holding
        # duration for the entire zero-to-zero segment.
        date_known = not any(marker in source for marker in ("日期未知", "date unknown", "Date unknown"))
        event_day = lot_day if date_known else ""
        names[code] = str(lot.get("fund_name") or "未知基金")
        events_by_fund.setdefault(code, []).append({
            "kind": "buy", "event_date": event_day,
            # Orphan lots describe the position that existed before the
            # immutable posting ledger was introduced.  They must precede all
            # posted trades even when a legacy date is blank or was corrected.
            "sort_key": (0, event_day, int(lot["id"])),
            "units": float(lot.get("units") or 0), "amount": amount,
            "fee": float(lot.get("fee") or 0), "trade_id": None,
            "cost_known": cost_known, "source": source or "历史成本批次",
        })
    for row in postings:
        code = str(row["fund_code"])
        names[code] = str(row.get("fund_name") or "未知基金")
        event_date = _review_day(row.get("nav_date") or row.get("trade_date"))
        events_by_fund.setdefault(code, []).append({
            **row,
            "kind": "buy" if str(row["posting_kind"]) == "买入" else "sell",
            "event_date": event_date,
            "trade_id": int(row["id"]),
            # Posting sequence is the accounting order.  A later confirmation
            # may legitimately carry an earlier pricing date and must not be
            # replayed ahead of a transaction that was already posted.
            "sort_key": (1, int(row.get("posting_seq") or 0), int(row["id"])),
            "cost_known": True,
        })

    completed: list[dict[str, Any]] = []
    open_segments: list[dict[str, Any]] = []
    for code in sorted(fund_scope):
        events = sorted(events_by_fund.get(code, []), key=lambda item: item["sort_key"])
        balance = 0.0
        segment_no = 0
        current: dict[str, Any] | None = None

        def new_segment(start_date: str, *, unknown_reason: str = "") -> dict[str, Any]:
            nonlocal segment_no
            segment_no += 1
            return {
                "segment_id": f"{code}:{segment_no}", "fund_code": code,
                "fund_name": names.get(code, "未知基金"), "start_date": start_date,
                "end_date": "", "buy_count": 0, "exit_count": 0,
                "buy_trade_ids": [], "exit_trade_ids": [],
                "invested_amount": 0.0, "buy_fee_total": 0.0,
                "opening_units": 0.0, "peak_units": 0.0,
                "_exits": [], "_unknown_reasons": [unknown_reason] if unknown_reason else [],
                "is_complete": False,
            }

        for event in events:
            units = max(0.0, float(event.get("units") or 0))
            if units <= 1e-9:
                continue
            day = str(event.get("event_date") or "")
            if event["kind"] == "buy":
                if current is None:
                    current = new_segment(day)
                    balance = 0.0
                elif not day:
                    # Any earlier unit with an unknown acquisition date makes
                    # the start of this entire continuous holding segment
                    # unknown, even if the main paid lot has a known date.
                    current["start_date"] = ""
                current["buy_count"] += 1
                if event.get("trade_id") is not None:
                    current["buy_trade_ids"].append(int(event["trade_id"]))
                current["buy_fee_total"] += float(event.get("fee") or 0)
                if event.get("cost_known") and current.get("invested_amount") is not None:
                    current["invested_amount"] += float(event.get("amount") or 0)
                else:
                    current["invested_amount"] = None
                    current["_unknown_reasons"].append(
                        f"{day or '日期未知'}的历史开仓成本没有可靠来源"
                    )
                balance += units
                current["opening_units"] += units
                current["peak_units"] = max(float(current["peak_units"]), balance)
                continue

            if current is None:
                start = holding_starts.get(code, "") or day
                current = new_segment(start, unknown_reason="缺少可核对的历史开仓流水")
                balance = units
                current["opening_units"] = units
                current["peak_units"] = units
                current["invested_amount"] = None
            tolerance = max(1e-6, balance * 1e-7)
            if units > balance + tolerance:
                missing_units = units - balance
                balance += missing_units
                current["opening_units"] += missing_units
                current["peak_units"] = max(float(current["peak_units"]), balance)
                current["invested_amount"] = None
                current["_unknown_reasons"].append(
                    f"退出份额比已重建持仓多{missing_units:.4f}份"
                )
            current["_exits"].append(event)
            balance = max(0.0, balance - units)
            tolerance = max(1e-6, float(current["peak_units"]) * 1e-7)
            if balance <= tolerance:
                completed.append(_finish_holding_segment(current, day))
                current = None
                balance = 0.0

        if current is not None:
            current["remaining_units"] = balance
            current["holding_days"] = _inclusive_calendar_days(
                str(current.get("start_date") or ""), local_now().date().isoformat(),
            )
            current["pnl_status"] = "仍在持有"
            open_segments.append(current)

    # A pre-ledger active holding may have neither a posting nor a cost lot.  It
    # is still an open segment, but must never be presented as a completed win.
    open_codes = {str(row["fund_code"]) for row in open_segments}
    for holding in active_holdings:
        code = str(holding["fund_code"])
        if code in open_codes or code in events_by_fund:
            continue
        open_segments.append({
            "segment_id": f"{code}:legacy-open", "fund_code": code,
            "fund_name": str(holding.get("fund_name") or "未知基金"),
            "start_date": _review_day(holding.get("confirmed_date")), "end_date": "",
            "buy_count": 0, "exit_count": 0, "buy_trade_ids": [], "exit_trade_ids": [],
            "invested_amount": float(holding.get("cost") or 0),
            "remaining_units": float(holding.get("units") or 0),
            "holding_days": _inclusive_calendar_days(
                _review_day(holding.get("confirmed_date")), local_now().date().isoformat(),
            ),
            "pnl_status": "仍在持有", "pnl_reason": "旧持仓没有逐笔开仓流水",
            "is_complete": False,
        })

    completed.sort(key=lambda row: (str(row.get("end_date") or ""), row["fund_code"], row["segment_id"]))
    open_segments.sort(key=lambda row: (str(row.get("start_date") or ""), row["fund_code"]))
    return {"completed": completed, "open": open_segments}


def holding_segment_metrics(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise only fully closed holding segments; unknown is never zero."""
    known = [
        row for row in segments
        if row.get("result_pnl") is not None and str(row.get("pnl_status") or "") == "已核算"
    ]
    pnls = [float(row["result_pnl"]) for row in known]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    running = 0.0
    cumulative = []
    for value in pnls:
        running += value
        cumulative.append(running)
    holding_days = [
        int(row["holding_days"]) for row in segments if row.get("holding_days") is not None
    ]
    return {
        "segment_count": len(segments), "known_segment_count": len(known),
        "unknown_segment_count": len(segments) - len(known),
        "exit_amount_total": sum(float(row.get("exit_amount") or 0) for row in segments),
        "pnl_total": sum(pnls),
        "win_rate_pct": len(wins) / len(known) * 100 if known else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else None),
        "average_holding_days": sum(holding_days) / len(holding_days) if holding_days else None,
        "cumulative": cumulative,
    }


def realized_exit_trades(db) -> list[dict[str, Any]]:
    """Return only confirmed exits that actually close or reduce a strategy position."""
    return db.rows(
        """SELECT t.*,COALESCE(f.fund_name,'未知基金') AS fund_name,
                  COALESCE((SELECT MIN(h.confirmed_date) FROM holdings h
                             WHERE h.fund_code=t.fund_code),t.trade_date) AS position_start
             FROM trades t LEFT JOIN funds f USING(fund_code)
            WHERE t.action IN ('卖出','清仓','转换','转换转出')
              AND COALESCE(t.status,'已确认')='已确认'
              AND COALESCE(t.counts_toward_active_capital,0)=1
            ORDER BY t.trade_date,t.id"""
    )


def import_radar_payload(db, payload: dict[str, Any], raw_text: str, warnings: list[str] | None = None):
    protocol_version = str(payload.get("protocol_version") or "").strip()
    # Unversioned historical callers predate the protocol field.  Fail closed:
    # they receive the same raw-only treatment as explicit V2 rather than being
    # guessed into an executable V3 result.
    if protocol_version in {"", "2.0"}:
        return _archive_legacy_v2_payload(db, payload, raw_text, warnings)
    if protocol_version != "3.0":
        raise ValueError("研究结果缺少受支持的协议版本；仅V3可执行，V2只能只读归档。")
    now = local_now().isoformat(timespec="seconds")
    task_id = payload["task_id"]
    summary = payload.get("market_summary", "")
    capital_snapshot = current_capital_snapshot(db)
    expected_ids = _catalog_snapshot_ids(db)
    expected_hash = _catalog_snapshot_hash(db, expected_ids)
    today = local_now().date().isoformat()
    calendar = ChinaTradingCalendar.from_db(db)
    expected_cutoff = calendar.latest_completed_trading_day(local_now()).isoformat()
    policy_version, policy_json = _current_ranking_policy_snapshot(db)
    payload_policy = payload.get("ranking_policy")
    payload_policy_json = (
        json.dumps(payload_policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if isinstance(payload_policy, dict) else ""
    )
    audit_ids = [str(item.get("id") or "").strip() for item in payload.get("audit", [])]
    # A validated web result has no locally-created analysis_jobs row.  Create the
    # immutable job snapshot only when the imported result is unquestionably the
    # current, complete catalog.  Older/partial imports remain readable history and
    # cannot become actionable merely because they were imported most recently.
    current_complete_import = (
        bool(expected_ids)
        and str(payload.get("task_status") or "") == "COMPLETE"
        and str(payload.get("universe_version") or "") == UNIVERSE_VERSION
        and str(payload.get("data_cutoff") or "").strip()[:10] == expected_cutoff
        and str(payload.get("ranking_policy_version") or "") == policy_version
        and payload_policy_json == policy_json
        and int(payload.get("universe_total") or 0) == len(expected_ids)
        and int(payload.get("analyzed_total") or 0) == len(expected_ids)
        and len(audit_ids) == len(expected_ids)
        and len(set(audit_ids)) == len(expected_ids)
        and set(audit_ids) == set(expected_ids)
    )
    with db.connect() as conn:
        if current_complete_import:
            conn.execute(
                """INSERT INTO analysis_jobs(
                       task_id,mode,stage,status,total_items,completed_items,cost_limit_cny,
                       estimated_cost_cny,universe_version,research_date,universe_ids_json,
                       universe_hash,ranking_policy_version,ranking_policy_json,error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     mode=excluded.mode,stage=excluded.stage,status=excluded.status,
                     total_items=excluded.total_items,completed_items=excluded.completed_items,
                     universe_version=excluded.universe_version,research_date=excluded.research_date,
                     universe_ids_json=excluded.universe_ids_json,universe_hash=excluded.universe_hash,
                     ranking_policy_version=excluded.ranking_policy_version,
                     ranking_policy_json=excluded.ranking_policy_json,
                     error=excluded.error,updated_at=excluded.updated_at""",
                (
                    task_id, "完整扫描", "完成", "已完成", len(expected_ids), len(expected_ids),
                    0.0, 0.0, UNIVERSE_VERSION, today,
                    json.dumps(expected_ids, ensure_ascii=False, separators=(",", ":")),
                    expected_hash, policy_version, policy_json, "", now, now,
                ),
            )
        conn.execute(
            """INSERT INTO radar_runs(task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                   ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(task_id) DO UPDATE SET
                 data_cutoff=excluded.data_cutoff, market_regime=excluded.market_regime,
                 universe_total=excluded.universe_total, analyzed_total=excluded.analyzed_total,
                 ranking_policy_version=excluded.ranking_policy_version,
                 ranking_policy_json=excluded.ranking_policy_json,
                 summary=excluded.summary, raw_json=excluded.raw_json, imported_at=excluded.imported_at""",
            (
                task_id,
                payload["data_cutoff"],
                payload["market_regime"],
                payload["universe_total"],
                payload["analyzed_total"],
                str(payload.get("ranking_policy_version") or ""),
                payload_policy_json or "{}",
                summary,
                json.dumps(payload, ensure_ascii=False),
                now,
            ),
        )
        conn.execute("DELETE FROM radar_audit WHERE task_id=?", (task_id,))
        for item in payload.get("audit", []):
            returns = item.get("returns_pct", {}) or {}
            conn.execute(
                """INSERT INTO radar_audit(task_id,universe_id,sector_name,category,proxy_code,proxy_name,
                   d1,d3,d5,d10,d20,volume_signal,breadth_signal,position_20d,crowding,catalyst,
                   source_ids_json,note,local_rank,pool_route,research_tilt,timing_status,execution_status,
                   opportunity_score,trend_score,entry_score,crowding_risk_score,data_confidence,
                   recommendation_score,score_components_json,local_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, str(item.get("id", "")), str(item.get("name", "")), str(item.get("category", "")),
                    str(item.get("proxy_code", "")), str(item.get("proxy_name", "")),
                    float(returns.get("d1", 0) or 0), float(returns.get("d3", 0) or 0),
                    float(returns.get("d5", 0) or 0), float(returns.get("d10", 0) or 0),
                    float(returns.get("d20", 0) or 0), str(item.get("volume_signal", "")),
                    str(item.get("breadth_signal", "")), str(item.get("position_20d", "")),
                    str(item.get("crowding", "")), str(item.get("catalyst", "")),
                    json.dumps(item.get("source_ids", []), ensure_ascii=False), str(item.get("note", "")),
                    int(item.get("local_rank", 0) or 0), str(item.get("pool_route", "")),
                    str(item.get("research_tilt", "观察")), str(item.get("timing_status", "等待回调")),
                    str(item.get("execution_status", "不可执行")),
                    float(item.get("opportunity_score", 0) or 0), float(item.get("trend_score", 0) or 0),
                    float(item.get("entry_score", 0) or 0), float(item.get("crowding_risk_score", 0) or 0),
                    float(item.get("data_confidence", 0) or 0), float(item.get("recommendation_score", 0) or 0),
                    json.dumps(item.get("score_components", {}), ensure_ascii=False, separators=(",", ":")),
                    str(item.get("local_reason") or "本地四维可复算评分"),
                ),
            )
        _replace_candidates(conn, task_id, payload.get("candidates", []), now)
        # Only a complete, current V3 run enters the immutable performance
        # ledger.  Old/partial/web archives remain readable but cannot pollute
        # the algorithm's forward-return statistics.  This is observation only:
        # it never creates drafts, trades, holdings or cash postings.
        if current_complete_import:
            capture_recommendation_observations(conn, task_id, now, calendar)
        _replace_holding_recommendations(conn, task_id, payload, now, "完整扫描")
        action_summary = str(payload.get("action_summary") or summary)
        positive = [item for item in payload.get("candidates", []) if float(item.get("suggested_amount", 0) or 0) > 0]
        no_buy_reason = str(payload.get("no_buy_reason") or "")
        if not positive and not no_buy_reason:
            no_buy_reason = "AI本轮没有给出正金额买入建议；请阅读市场总结与风险旗标，不要把空候选误解为程序失败。"
        action_json = {
            key: payload.get(key, [] if key.endswith("actions") else "")
            for key in ("action_summary", "no_buy_reason", "holding_actions")
        }
        action_json["capital_snapshot"] = capital_snapshot
        conn.execute(
            """UPDATE radar_runs SET action_summary=?,no_buy_reason=?,action_json=?,action_updated_at=?
               WHERE task_id=?""",
            (action_summary, no_buy_reason, json.dumps(action_json, ensure_ascii=False), now, task_id),
        )
        for update in payload.get("fund_updates", []):
            code = str(update.get("fund_code", "")).strip()
            name = str(update.get("fund_name", "")).strip()
            if code and name:
                exists = conn.execute("SELECT 1 FROM funds WHERE fund_code=?", (code,)).fetchone()
                if not exists:
                    conn.execute(
                        """INSERT INTO funds(fund_code,fund_name,index_code,index_name,share_class,
                           correlation_bucket,purchase_fee_pct,status,notes,updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            code,
                            name,
                            str(update.get("index_code", "")),
                            str(update.get("index_name", "")),
                            str(update.get("share_class", "")),
                            str(update.get("correlation_bucket", "")),
                            float(update.get("purchase_fee_pct", 0) or 0),
                            "AI建议-待核验",
                            "由AI结果提出；使用前必须人工核验支付宝可买性和费率。",
                            now,
                        ),
                    )
        conn.execute(
            "INSERT INTO ai_imports(imported_at,task_id,validation_status,errors,raw_text) VALUES (?,?,?,?,?)",
            (now, task_id, "通过", "\n".join(warnings or []), raw_text),
        )
    return {"task_id": task_id, "read_only": False, "stored_run": True}


def log_failed_import(db, raw_text: str, errors: list[str]):
    db.execute(
        "INSERT INTO ai_imports(imported_at,validation_status,errors,raw_text) VALUES (?,?,?,?)",
        (local_now().isoformat(timespec="seconds"), "失败", "\n".join(errors), raw_text),
    )


def _text(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


def _replace_candidates(conn, task_id: str, candidates: list[dict[str, Any]], now: str):
    conn.execute("DELETE FROM candidates WHERE task_id=?", (task_id,))
    for number, item in enumerate(candidates, start=1):
        conn.execute(
            """INSERT INTO candidates(
               task_id,rank_no,universe_id,index_code,sector_name,fund_code,fund_name,status,score,
               allocation_pct,target_allocation_pct,suggested_amount,correlation_bucket,thesis,catalyst,crowding,risk,
               buy_conditions,abandon_conditions,research_tilt,timing_status,execution_status,
               opportunity_score,entry_score,crowding_risk_score,data_confidence,pool_route,
               suppression_reason,execution_reason,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, int(item.get("rank", number) or number), str(item.get("universe_id", "")).strip(),
                str(item.get("index_code", "")).strip(), str(item.get("sector_name", "")).strip(),
                str(item.get("fund_code", "")).strip(), str(item.get("fund_name", "")).strip(),
                str(item.get("research_tilt") or item.get("status") or "观察"),
                float(item.get("recommendation_score", item.get("score", 0)) or 0),
                float(item.get("allocation_pct", 0) or 0),
                float(item.get("target_allocation_pct", item.get("allocation_pct", 0)) or 0),
                float(item.get("suggested_amount", 0) or 0),
                str(item.get("correlation_bucket", "")), _text(item.get("thesis", "")),
                _text(item.get("catalyst", "")), _text(item.get("crowding", "")), _text(item.get("risk", "")),
                _text(item.get("buy_conditions", "")), _text(item.get("abandon_conditions", "")),
                str(item.get("research_tilt") or item.get("status") or "观察"),
                str(item.get("timing_status") or "等待回调"),
                str(item.get("execution_status") or "不可执行"),
                float(item.get("opportunity_score", 0) or 0), float(item.get("entry_score", 0) or 0),
                float(item.get("crowding_risk_score", 0) or 0), float(item.get("data_confidence", 0) or 0),
                str(item.get("pool_route") or ""), str(item.get("suppression_reason") or ""),
                str(item.get("execution_reason") or ""), now,
            ),
        )


def _holding_rows(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT h.*,f.fund_name,f.index_name,f.correlation_bucket
           FROM holdings h JOIN funds f USING(fund_code)
           WHERE h.active=1 AND COALESCE(h.counts_toward_active_capital,0)=1 ORDER BY h.fund_code"""
    ).fetchall()
    return {str(row["fund_code"]): dict(row) for row in rows}


def _replace_holding_recommendations(conn, task_id: str, payload: dict[str, Any], now: str, source: str):
    holdings = _holding_rows(conn)
    active_limit_row = conn.execute("SELECT value FROM settings WHERE key='active_capital_limit'").fetchone()
    active_limit = float(active_limit_row[0] if active_limit_row else 8000)
    actions = payload.get("holding_actions")
    explicit = isinstance(actions, list) and bool(actions)
    if not explicit:
        actions = payload.get("fund_updates", [])
    conn.execute("DELETE FROM holding_recommendations WHERE task_id=?", (task_id,))
    for item in actions if isinstance(actions, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("fund_code") or "").strip()
        holding = holdings.get(code)
        if not holding:
            continue
        market_value = float(holding["units"]) * float(holding["latest_nav"])
        raw_pct = item.get("sell_pct") if explicit else None
        sell_pct = None if raw_pct in (None, "") else max(0.0, min(100.0, float(raw_pct)))
        sell_amount = None if sell_pct is None else round(market_value * sell_pct / 100, 2)
        action = str(item.get("action") or "持有")
        reason = _text(item.get("reason") or item.get("thesis") or "")
        conn.execute(
            """INSERT INTO holding_recommendations(
               task_id,fund_code,fund_name,sleeve,market_value,position_pct_active,action,sell_pct,
               estimated_sell_amount,reason,risk,source,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, code, str(holding.get("fund_name") or item.get("fund_name") or ""),
             str(holding.get("sleeve") or ""), market_value,
             (market_value / active_limit * 100) if active_limit > 0 else 0,
             action, sell_pct, sell_amount, reason, _text(item.get("risk") or ""), source, now),
        )


def save_action_advice(db, task_id: str, action: dict[str, Any]):
    current = latest_current_radar_run(db)
    if not current or str(current.get("task_id") or "") != str(task_id):
        raise ValueError(
            "这轮研究已不是今天当前严格C目录的完整结果；基金映射或目录已变化，禁止写入AI行动。"
        )
    now = local_now().isoformat(timespec="seconds")
    action_payload = dict(action)
    action_payload["capital_snapshot"] = current_capital_snapshot(db)
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM radar_runs WHERE task_id=?", (task_id,)).fetchone():
            raise ValueError("找不到可补全的完整研究轮次。")
        _replace_candidates(conn, task_id, action.get("candidates", []), now)
        _replace_holding_recommendations(conn, task_id, action, now, "AI补全今日行动")
        conn.execute(
            """UPDATE radar_runs SET action_summary=?,no_buy_reason=?,action_json=?,action_updated_at=?
               WHERE task_id=?""",
            (str(action.get("action_summary") or ""), str(action.get("no_buy_reason") or ""),
             json.dumps(action_payload, ensure_ascii=False), now, task_id),
        )


def holding_recommendations_for_run(db, run: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not run:
        return []
    rows = db.rows(
        "SELECT * FROM holding_recommendations WHERE task_id=? ORDER BY sleeve,fund_code", (run["task_id"],),
    )
    if rows:
        return rows
    try:
        payload = json.loads(run.get("raw_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return []
    holdings = {row["fund_code"]: row for row in db.rows(
        """SELECT h.*,f.fund_name FROM holdings h JOIN funds f USING(fund_code)
           WHERE h.active=1 AND COALESCE(h.counts_toward_active_capital,0)=1
           ORDER BY h.sleeve,h.fund_code"""
    )}
    active_limit = float(db.setting("active_capital_limit", "8000"))
    result = []
    for item in payload.get("fund_updates", []):
        if not isinstance(item, dict):
            continue
        code = str(item.get("fund_code") or "")
        holding = holdings.get(code)
        if not holding:
            continue
        value = float(holding["units"]) * float(holding["latest_nav"])
        result.append({
            "task_id": run["task_id"], "fund_code": code, "fund_name": holding["fund_name"],
            "sleeve": holding["sleeve"], "market_value": value,
            "position_pct_active": value / active_limit * 100 if active_limit > 0 else 0,
            "action": str(item.get("action") or "持有"), "sell_pct": None,
            "estimated_sell_amount": None, "reason": _text(item.get("reason") or ""),
            "risk": "", "source": "旧研究文字建议", "updated_at": run.get("imported_at", ""),
        })
    return result


def dashboard_metrics(db, research_state: dict[str, Any] | None = None) -> dict[str, Any]:
    holdings = db.rows(
        """SELECT h.*,f.fund_name,f.purchase_fee_pct FROM holdings h JOIN funds f USING(fund_code)
           WHERE h.active=1"""
    )
    total_value = sum(float(h["units"]) * float(h["latest_nav"]) for h in holdings)
    total_cost = sum(float(h["cost"]) for h in holdings)
    active_summary = active_capital_summary(db)
    run_state = research_state if research_state is not None else research_run_status(db)
    run = (
        run_state.get("research") or run_state.get("current")
        if research_state is not None else run_state.get("current")
    )
    candidates = []
    holding_actions = []
    if run:
        candidates = db.rows("SELECT * FROM candidates WHERE task_id=? ORDER BY rank_no", (run["task_id"],))
        holding_actions = holding_recommendations_for_run(db, run)
    return {
        "holdings_count": len(holdings),
        "total_value": total_value,
        "total_cost": total_cost,
        "unrealized": total_value - total_cost,
        "active_value": active_summary.get("market_exposure", active_summary["used"]),
        "active_market_exposure": active_summary.get("market_exposure", active_summary["used"]),
        "active_committed_cost": active_summary.get("committed_cost", 0),
        "active_cash_before_reservations": active_summary.get("cash_before_reservations", active_summary["available"]),
        "active_reserved": active_summary.get("reserved", 0),
        "active_reserved_drafts": active_summary.get("reserved_drafts", 0),
        "active_reserved_pending": active_summary.get("reserved_pending_trades", 0),
        "active_cash_available": active_summary.get("cash_available", active_summary["available"]),
        "active_available": active_summary.get("cash_available", active_summary["available"]),
        "active_over_limit": active_summary.get("over_limit", 0),
        "active_cash_basis": active_summary.get("cash_basis", ""),
        "active_limit": active_summary["limit"],
        "latest_run": run,
        "research_status": run_state,
        "candidate_count": len(candidates),
        "buy_count": sum(float(row.get("suggested_amount", 0) or 0) > 0 for row in candidates),
        "holding_sell_count": sum(float(row.get("sell_pct", 0) or 0) > 0 for row in holding_actions),
        "holding_actions": holding_actions,
        "candidates": candidates,
    }


def daily_workflow_status(db, *, as_of: datetime | None = None) -> dict[str, Any]:
    """Build the seven-step daily route from authoritative local state.

    This is intentionally read-only.  It never infers cash from market value,
    never treats an AI opinion as a user operation, and never calls the
    network merely because the dashboard was opened.
    """
    moment = as_of or local_now()
    today = moment.date().isoformat()
    calendar = ChinaTradingCalendar.from_db(db)
    expected_cutoff = calendar.latest_completed_trading_day(moment).isoformat()
    catalog = PlatformCatalogService(db).radar_universe(refresh_admission=False)
    catalog_ids = [str(row.get("index_code") or "") for row in catalog]
    placeholders = ",".join("?" for _ in catalog_ids)
    run_state = research_run_status(db)
    run = run_state.get("current")
    run_complete = bool(
        run
        and int(run.get("universe_total") or 0) == len(catalog_ids)
        and int(run.get("analyzed_total") or 0) == len(catalog_ids)
        and str(run.get("data_cutoff") or "")[:10] == expected_cutoff
    )
    market_ready = 0
    market_latest = ""
    market_checked = 0
    market_checked_cutoff = ""
    if catalog_ids:
        # A completed current scan owns the strongest evidence: one strictly
        # validated metric per frozen catalog item.  Its provider may be a
        # reviewed fallback and therefore need not equal proxy_mappings.provider.
        if run_complete:
            metric_row = db.row(
                """SELECT COUNT(DISTINCT universe_id) AS n,MAX(data_cutoff) AS latest_day
                     FROM sector_metrics
                    WHERE task_id=? AND is_simulated=0 AND data_cutoff=?
                      AND freshness_state='fresh'""",
                (run["task_id"], expected_cutoff),
            ) or {}
            market_ready = min(len(catalog_ids), int(metric_row.get("n") or 0))
            market_latest = str(metric_row.get("latest_day") or "")
        else:
            rows = db.rows(
                f"""SELECT p.universe_id,MAX(b.trade_date) AS latest_day
                      FROM proxy_mappings p
                      LEFT JOIN market_bars b ON b.security_id=p.security_id
                     WHERE p.active=1 AND p.confidence>=0.75 AND p.mapping_kind<>'待核验'
                       AND p.universe_id IN ({placeholders})
                     GROUP BY p.universe_id""",
                catalog_ids,
            )
            latest_by_id = {str(row["universe_id"]): str(row.get("latest_day") or "") for row in rows}
            market_ready = sum(latest_by_id.get(item) == expected_cutoff for item in catalog_ids)
            market_latest = max(latest_by_id.values(), default="")
        latest_market_job = db.row(
            """SELECT task_id,status,total_items,completed_items
                 FROM analysis_jobs
                WHERE mode='全目录行情更新' AND research_date=?
                ORDER BY id DESC LIMIT 1""",
            (today,),
        ) or {}
        if (
            latest_market_job.get("status") == "行情更新完成"
            and int(latest_market_job.get("total_items") or 0) == len(catalog_ids)
            and int(latest_market_job.get("completed_items") or 0) == len(catalog_ids)
        ):
            checked_row = db.row(
                """SELECT COUNT(DISTINCT universe_id) AS n,MIN(data_cutoff) AS min_day,
                          MAX(data_cutoff) AS max_day
                     FROM sector_metrics WHERE task_id=? AND is_simulated=0""",
                (latest_market_job["task_id"],),
            ) or {}
            if str(checked_row.get("min_day") or "") == str(checked_row.get("max_day") or ""):
                market_checked = min(len(catalog_ids), int(checked_row.get("n") or 0))
                market_checked_cutoff = str(checked_row.get("max_day") or "")
    previous_cutoff = calendar.previous_trading_day(
        datetime.fromisoformat(expected_cutoff).date()
    ).isoformat()
    publication_pending = bool(
        catalog_ids
        and market_ready < len(catalog_ids)
        and market_checked == len(catalog_ids)
        and market_checked_cutoff == previous_cutoff
    )
    candidates = db.rows(
        "SELECT * FROM candidates WHERE task_id=? ORDER BY rank_no",
        (run["task_id"],),
    ) if run_complete else []
    positive_tiers = {"非常推荐买", "推荐买", "有点推荐买"}
    recommended = sum(str(row.get("research_tilt") or "") in positive_tiers for row in candidates)
    executable = sum(
        float(row.get("suggested_amount") or 0) > 0
        and bool(str(row.get("fund_code") or "").strip())
        and str(row.get("execution_status") or "") not in {"不可执行", "待核验"}
        for row in candidates
    )

    draft_rows = db.rows(
        "SELECT * FROM decision_drafts WHERE trade_date=? ORDER BY id",
        (today,),
    )
    alive = sum(str(row.get("status") or "") == "草稿" for row in draft_rows)
    posted = sum(str(row.get("status") or "") != "草稿" for row in draft_rows)
    stale_drafts = int((db.row(
        "SELECT COUNT(*) AS n FROM decision_drafts WHERE trade_date<? AND status='草稿'",
        (today,),
    ) or {}).get("n") or 0)
    close_hour = int(float(db.setting("decision_close_hour", "15")))
    close_open = moment.hour >= close_hour
    pending_trades = int((db.row(
        "SELECT COUNT(*) AS n FROM trades WHERE status='待确认'"
    ) or {}).get("n") or 0)
    recurring_counts = {
        str(row["status"]): int(row["n"] or 0)
        for row in db.rows("SELECT status,COUNT(*) AS n FROM recurring_occurrences GROUP BY status")
    }
    recurring_ready = recurring_counts.get("可确认入账", 0)
    recurring_waiting = sum(
        count for status, count in recurring_counts.items()
        if status not in {"已入账", "已跳过"}
    )
    holding_tasks = pending_trades + recurring_waiting
    holding_page = "trades" if pending_trades else "recurring" if recurring_waiting else "holdings"
    snapshot_row = db.row("SELECT COUNT(*) AS n FROM portfolio_equity_snapshots") or {}
    snapshot_count = int(snapshot_row.get("n") or 0)

    def step(key, title, page, status, detail, state):
        return {
            "key": key, "title": title, "page": page, "status": status,
            "detail": detail, "state": state,
        }

    if catalog_ids and market_ready == len(catalog_ids):
        data_status, data_detail, data_state = (
            "已就绪", f"行情 {market_ready}/{len(catalog_ids)}｜截至 {expected_cutoff}", "done"
        )
    elif publication_pending:
        data_status, data_detail, data_state = (
            "已检查·待发布",
            f"官网仍截至 {market_checked_cutoff}｜无需重复点击",
            "waiting",
        )
    else:
        data_status, data_detail, data_state = (
            "需更新", f"行情 {market_ready}/{len(catalog_ids)}｜应截至 {expected_cutoff}", "todo"
        )
    if run_complete:
        scan_status, scan_state = "已完成", "done"
    elif publication_pending:
        scan_status, scan_state = "暂缓扫描", "locked"
    else:
        scan_status, scan_state = "待扫描", "todo"

    steps = [
        step(
            "data", "更新数据", "market",
            data_status, data_detail, data_state,
        ),
        step(
            "scan", "AI扫描", "api",
            scan_status,
            ("等待中证官网发布今日行情" if publication_pending else
             f"研究 {int(run.get('analyzed_total') or 0) if run else 0}/{len(catalog_ids)}｜{expected_cutoff}"),
            scan_state,
        ),
        step(
            "recommend", "查看推荐", "radar",
            "可查看" if run_complete else "等待扫描",
            f"主榜 {len(candidates)}｜正向 {recommended}｜可执行 {executable}",
            "ready" if run_complete else "locked",
        ),
        step(
            "draft", "加入草稿", "decision",
            (f"跨日待处理 {stale_drafts} 项" if stale_drafts else
             f"存活 {alive} 项" if alive else "按需添加"),
            ("定投确认原日扣款；其他意向确认续做或放弃" if stale_drafts else
             "只有加入草稿才是你的拟操作"),
            "todo" if stale_drafts else "ready" if run_complete else "todo",
        ),
        step(
            "close", "收盘确认", "decision",
            f"已写入 {posted} 项" if posted else ("可以处理" if close_open else f"等待 {close_hour}:00"),
            f"存活草稿 {alive}｜撤销项不会入账",
            "done" if posted and not alive else "ready" if close_open and alive else "waiting",
        ),
        step(
            "holdings", "更新持仓", holding_page,
            "仍有待办" if holding_tasks else "账本已同步",
            f"待确认交易 {pending_trades}｜定投待办 {recurring_waiting}（可确认 {recurring_ready}）",
            "todo" if holding_tasks else "done",
        ),
        step(
            "review", "查看复盘", "review",
            "可查看" if snapshot_count else "等待样本",
            f"组合快照 {snapshot_count}｜未知盈亏不按0计算",
            "ready" if snapshot_count else "waiting",
        ),
    ]
    # A pending real-world transaction is more urgent than creating an optional
    # new intention.  Likewise an old draft needs an explicit real-world
    # outcome and must not silently become today's order.  When neither backlog
    # exists, retain the ordinary left-to-right daily route.
    actionable_ledger_tasks = pending_trades + recurring_ready
    if actionable_ledger_tasks:
        next_step = steps[5]
        next_reason = "账本待办优先：先核对支付宝正式结果，再安排新的研究"
    elif stale_drafts:
        next_step = steps[3]
        next_reason = "跨日草稿优先：先确认原日真实结果，避免误当成今日操作"
    else:
        next_step = next(
            (item for item in steps if item["state"] in {"todo", "ready"}),
            steps[-1],
        )
        next_reason = "按日常流程从左到右推进"
    return {
        "as_of": moment.isoformat(timespec="seconds"),
        "today": today,
        "expected_cutoff": expected_cutoff,
        "catalog_total": len(catalog_ids),
        "market_ready": market_ready,
        "market_latest": market_latest,
        "market_checked": market_checked,
        "market_checked_cutoff": market_checked_cutoff,
        "publication_pending": publication_pending,
        "run_complete": run_complete,
        "stale_drafts": stale_drafts,
        "actionable_ledger_tasks": actionable_ledger_tasks,
        "steps": steps,
        "next_step": next_step,
        "next_reason": next_reason,
    }
