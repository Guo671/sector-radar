from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from contextlib import ExitStack
from datetime import datetime, time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from .catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from .config import APP_VERSION, DB_PATH, RUNTIME_DIR, app_root, local_now
from .database import Database
from .decision_drafts import DecisionDraftService
from .investable import DraftSettlementService
from .ledger import LedgerError, PortfolioLedgerService
from .orchestrator import ResearchOrchestrator
from .platform_catalog import PlatformCatalogService
from .portfolio import active_capital_summary
from .protocol import END_MARKER, START_MARKER, parse_result
from .services import (
    daily_workflow_status,
    holding_segment_review,
    import_radar_payload,
    latest_current_radar_run,
    realized_exit_trades,
    strategy_review_metrics,
)
from .trading_calendar import ChinaTradingCalendar


REPORT_SCHEMA = "SECTOR_RADAR_RELEASE_ACCEPTANCE_V1"


class ReleaseAcceptanceError(RuntimeError):
    pass


class _FixedNavProvider:
    """Deterministic public-NAV substitute used only by the isolated test."""

    name = "isolated_release_acceptance"

    def __init__(self, nav_date: str, nav: float):
        self.nav_date = nav_date
        self.nav = float(nav)

    def fetch(self, _fund_code: str, _start: str, _end: str):
        return {self.nav_date: self.nav}, "https://example.test/sector-radar/nav"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _formal_database_snapshot(path: Path) -> dict[str, Any]:
    """Read non-sensitive release evidence without running migrations."""

    path = Path(path)
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "sha256": "",
        "integrity": "missing",
        "schema_version": "",
        "radar_indices": None,
        "strict_funds": None,
    }
    if not path.is_file():
        return result
    result["sha256"] = _sha256(path)
    connection = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=15)
        result["integrity"] = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "settings" in tables:
            row = connection.execute(
                "SELECT value FROM settings WHERE key='schema_version'"
            ).fetchone()
            result["schema_version"] = str(row[0]) if row else ""
        if "investable_indices" in tables:
            result["radar_indices"] = int(connection.execute(
                """SELECT COUNT(*) FROM investable_indices
                     WHERE active=1 AND radar_enabled=1 AND catalog_role='日扫代表'"""
            ).fetchone()[0])
        if "investable_funds" in tables and "settings" in tables:
            result["strict_funds"] = int(connection.execute(
                """SELECT COUNT(*) FROM investable_funds
                     WHERE active=1 AND relation_kind='同一指数'
                       AND UPPER(share_class)='C'
                       AND strategy_kind IN ('被动ETF联接','被动指数')
                       AND passive_verified=1
                       AND verification_algorithm=?
                       AND (public_buyable_status IN (
                           '公开资料显示可申购','公开渠道可申购','已核对可申购','开放申购'
                       ) OR public_buyable_status LIKE '限购%元')"""
                , (STRICT_FIELD_SYNC_ALGORITHM_VERSION,)
            ).fetchone()[0])
    except Exception as exc:
        result["integrity"] = f"read_failed: {type(exc).__name__}: {exc}"
    finally:
        if connection is not None:
            connection.close()
    return result


def _append_check(
    report: dict[str, Any], name: str, condition: bool, detail: Any,
) -> None:
    report["checks"].append({
        "name": name,
        "passed": bool(condition),
        "detail": detail,
    })
    if not condition:
        raise ReleaseAcceptanceError(f"{name}：{detail}")


def _clear_isolated_user_state(db: Database) -> None:
    """Remove bundled examples from the temporary database only."""

    # Child tables must be cleared before the rows they audit.  This database is
    # created below a unique TemporaryDirectory and can never be the daily DB.
    tables = (
        "lot_consumptions",
        "ledger_postings",
        "cost_lots",
        "portfolio_equity_snapshots",
        "active_cash_ledger",
        "decision_drafts",
        "trades",
        "holdings",
        "recurring_occurrences",
        "recurring_plans",
        "holding_recommendations",
        "candidates",
        "radar_runs",
        "analysis_events",
        "analysis_jobs",
    )
    for table in tables:
        db.execute(f"DELETE FROM {table}")
    db.save_settings({
        "active_capital_limit": "8000",
        "active_cash_balance": "8000",
        "csi_fund_sync_algorithm_version": STRICT_FIELD_SYNC_ALGORITHM_VERSION,
    })


def _prepare_fixture_catalog(
    db: Database, *, checked_at: str,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Enable one genuine same-index passive-C route in the isolated DB.

    New databases intentionally do not inherit the user's network-verified fund
    catalog.  The release test therefore promotes the bundled 930653/001632
    fixture only inside its temporary database.  This is enough to exercise all
    production hard gates without copying a key or claiming a live buyability
    check.
    """

    index_row = db.row(
        """SELECT id,universe_id,official_index_code,official_index_name,
                  ant_index_code,industry_group,allocation_cluster
             FROM investable_indices
            WHERE official_index_code='930653' AND active=1
              AND catalog_role='日扫代表'
            ORDER BY id DESC LIMIT 1"""
    )
    if not index_row:
        raise ReleaseAcceptanceError("隔离验收库缺少930653食品饮料叶子指数夹具。")
    mapping = db.row(
        """SELECT * FROM investable_funds
             WHERE investable_index_id=? AND fund_code='001632'
             ORDER BY id DESC LIMIT 1""",
        (int(index_row["id"]),),
    )
    if not mapping:
        raise ReleaseAcceptanceError("隔离验收库缺少001632同指数C类基金夹具。")

    db.execute(
        """UPDATE investable_funds
              SET fund_name='天弘中证食品饮料ETF联接C',
                  strategy_kind='被动ETF联接',share_class='C',
                  public_buyable_status='已核对可申购',
                  relation_kind='同一指数',passive_verified=1,active=1,
                  source_url='https://www.thfund.com.cn/fundinfo/001632',
                  platform_evidence_url='https://www.thfund.com.cn/fundinfo/001632',
                  platform_checked_at=?,verified_at=?,verification_algorithm=?
            WHERE id=?""",
        (
            checked_at,
            checked_at,
            STRICT_FIELD_SYNC_ALGORITHM_VERSION,
            int(mapping["id"]),
        ),
    )
    catalog = PlatformCatalogService(db)
    catalog.refresh_admission(int(index_row["id"]))
    universe = catalog.radar_universe(refresh_admission=False)
    selected = next(
        (
            row for row in universe
            if int(row.get("platform_index_id") or 0) == int(index_row["id"])
        ),
        None,
    )
    if not selected:
        raise ReleaseAcceptanceError("930653/001632夹具没有通过正式目录准入逻辑。")
    execution = catalog.execution_summary(
        as_of=datetime.fromisoformat(checked_at)
    )
    if int(execution.get("ready") or 0) != int(execution.get("total") or 0):
        raise ReleaseAcceptanceError(f"隔离目录执行复核未全通过：{execution}")
    funds = catalog.strict_catalog_funds(int(index_row["id"]))
    fund = next((row for row in funds if str(row.get("fund_code")) == "001632"), None)
    if not fund:
        raise ReleaseAcceptanceError("001632没有进入严格同指数、被动、C类白名单。")

    db.link_strict_funds_to_library(int(index_row["id"]))
    db.execute(
        """UPDATE funds SET fund_name='天弘中证食品饮料ETF联接C',
                  index_code='930653.CSI',index_name='中证食品饮料指数',
                  share_class='C',correlation_bucket='食品饮料',
                  purchase_fee_pct=0,status='可买',name_status='已核对',updated_at=?
            WHERE fund_code='001632'""",
        (checked_at,),
    )
    db.execute("DELETE FROM fee_tiers WHERE fund_code='001632'")
    db.execute(
        """INSERT INTO fee_tiers(
               fund_code,min_days,max_days,redeem_fee_pct,source_url,checked_at
           ) VALUES('001632',0,NULL,0,'https://example.test/sector-radar/fee',?)""",
        (checked_at,),
    )
    return selected, dict(fund), len(universe)


def _offline_validated_research(
    db: Database,
    *,
    task_id: str,
    job: dict[str, Any],
    index_row: dict[str, Any],
    fund: dict[str, Any],
    data_cutoff: str,
) -> tuple[dict[str, Any], str]:
    """Build, strictly validate and import one deterministic Pro-shaped result.

    Release acceptance must exercise the same protocol boundary as a paid scan,
    while remaining deterministic, offline and free.  The payload below is not
    presented as model research; it is a fixed contract fixture that still has
    to pass every production parser and immutable import gate.
    """

    category = str(index_row["category"])
    universe_id = str(index_row["index_code"])
    csi_code = str(index_row["csi_index_code"])
    index_name = str(index_row["index_name"])
    fund_code = str(fund["fund_code"])
    fund_name = str(fund["fund_name"])
    policy = json.loads(str(job["ranking_policy_json"]))
    source_specs = (
        ("S1", "中证指数离线验收资料", "https://www.csindex.com.cn/", "核对指数身份与代码"),
        ("S2", "中证指数行情验收资料", "https://www.csindex.com.cn/#/indices/family/list", "核对统一行情日期"),
        ("S3", "基金管理人离线验收资料", "https://www.thfund.com.cn/fundinfo/001632", "核对基金名称与份额类别"),
        ("S4", "基金公开申购验收资料", "https://www.thfund.com.cn/", "核对被动C类执行路径"),
        ("S5", "本地协议固定夹具", "https://example.test/sector-radar/protocol", "验证完整V3 JSON结构"),
    )
    sources = [
        {
            "id": source_id,
            "title": title,
            "url": url,
            "retrieved_at": data_cutoff,
            "supports": supports,
        }
        for source_id, title, url, supports in source_specs
    ]
    payload = {
        "protocol_version": "3.0",
        "task_kind": "night",
        "task_id": task_id,
        "task_status": "COMPLETE",
        "universe_version": str(job["universe_version"]),
        "data_cutoff": data_cutoff,
        "market_regime": "轮动震荡",
        "universe_total": 1,
        "analyzed_total": 1,
        "coverage_by_category": [
            {"category": category, "expected": 1, "analyzed": 1}
        ],
        "ranking_policy_version": str(job["ranking_policy_version"]),
        "ranking_policy": policy,
        "research_method": (
            "发布验收使用固定离线响应，逐项校验目录身份、五周期字段、七档分数、"
            "基金执行白名单与当前排名策略；不调用DeepSeek，也不构成投资建议。"
        ),
        "tool_calls_summary": [
            {"tool": "isolated_contract_fixture", "purpose": "验证正式Pro结果协议与入库链路", "items": 1}
        ],
        "audit": [
            {
                "id": universe_id,
                "name": index_name,
                "category": category,
                "proxy_code": csi_code,
                "proxy_name": index_name,
                "returns_pct": {"d1": 0.5, "d3": 1.2, "d5": 1.8, "d10": 2.6, "d20": 3.1},
                "volume_signal": "成交额温和放大",
                "breadth_signal": "真实扩散度在离线验收中明确标记为缺失",
                "position_20d": "价格区间位置55%",
                "crowding": "中等；仅用于协议验收",
                "catalyst": "固定离线验收催化字段",
                "research_tilt": "推荐买",
                "timing_status": "可以买",
                "execution_status": "可执行",
                "trend_score": 72,
                "entry_score": 76,
                "crowding_risk_score": 42,
                "data_confidence": 90,
                "opportunity_score": 78,
                "recommendation_score": 75,
                "status": "ANALYZED",
                "source_ids": ["S1", "S2", "S3", "S4", "S5"],
                "note": "离线发布验收，不代表真实行情结论",
            }
        ],
        "market_summary": "离线协议验收候选已通过严格校验。",
        "action_summary": "固定离线响应仅用于验证用户加入草稿后的完整闭环。",
        "candidates": [
            {
                "rank": 1,
                "universe_id": universe_id,
                "index_code": csi_code,
                "sector_name": index_name,
                "fund_code": fund_code,
                "fund_name": fund_name,
                "research_tilt": "推荐买",
                "timing_status": "可以买",
                "execution_status": "可执行",
                "trend_score": 72,
                "opportunity_score": 78,
                "recommendation_score": 75,
                "entry_score": 76,
                "crowding_risk_score": 42,
                "data_confidence": 90,
                "suggested_amount": 500,
                "allocation_pct": 6.25,
                "target_allocation_pct": 6.25,
                "correlation_bucket": str(
                    index_row.get("allocation_cluster") or category
                ),
                "thesis": "固定离线候选用于验证协议解析、导入和用户决策边界。",
                "catalyst": "固定离线验收催化字段",
                "crowding": "中等；仅用于协议验收",
                "risk": "该响应不含实时研究，不得用于真实下单。",
                "buy_conditions": "仅当用户在隔离验收中主动加入草稿",
                "abandon_conditions": "正式数据库、真实资金或联网环境参与时立即放弃",
            }
        ],
        "avoid_list": [],
        "fund_updates": [],
        "questions_for_intraday": ["当前涨跌幅"],
        "risk_flags": ["固定离线验收响应，不是实时研究"],
        "sources": sources,
    }
    raw_text = (
        f"{START_MARKER}\n"
        f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
        f"{END_MARKER}"
    )
    known_funds = {
        str(row["fund_code"])
        for row in db.rows("SELECT fund_code FROM funds")
    }
    parsed = parse_result(
        raw_text,
        known_funds,
        float(db.setting("active_capital_limit", "8000")),
        [index_row],
    )
    if not parsed.ok or not parsed.payload:
        raise ReleaseAcceptanceError(
            "离线Pro协议夹具未通过正式校验：" + "；".join(parsed.errors)
        )
    import_radar_payload(db, parsed.payload, raw_text, parsed.warnings)
    return parsed.payload, raw_text


def _run_isolated_flow(db: Database, report: dict[str, Any]) -> None:
    actual = local_now()
    calendar = ChinaTradingCalendar.from_db(db)
    operation_day = calendar.latest_completed_trading_day(actual)
    tzinfo = actual.tzinfo
    morning = datetime.combine(operation_day, time(14, 0), tzinfo=tzinfo)
    closed = datetime.combine(operation_day, time(16, 0), tzinfo=tzinfo)
    day = operation_day.isoformat()
    research_cutoff = calendar.latest_completed_trading_day(morning).isoformat()
    timestamp = closed.isoformat(timespec="seconds")
    report["simulated_operation_day"] = day

    # Keep every module-local clock on one completed trading day.  This makes the
    # release test deterministic on weekends and before 15:00 without changing
    # the operating system clock or the user's database.
    clock_targets = (
        "app.orchestrator.local_now",
        "app.services.local_now",
        "app.ledger.local_now",
        "app.investable.local_now",
        "app.platform_catalog.local_now",
        "app.decision_drafts.local_now",
    )
    with ExitStack() as stack:
        for target in clock_targets:
            stack.enter_context(patch(target, return_value=closed))

        _clear_isolated_user_state(db)
        _append_check(
            report,
            "isolated_state_clean",
            all(
                int((db.row(f"SELECT COUNT(*) AS n FROM {table}") or {}).get("n") or 0) == 0
                for table in ("trades", "holdings", "decision_drafts", "ledger_postings")
            ),
            "临时库无交易、持仓、草稿和过账记录",
        )

        index_row, fund, catalog_size = _prepare_fixture_catalog(
            db, checked_at=timestamp,
        )
        report["isolated_catalog_size"] = catalog_size
        _append_check(
            report,
            "strict_c_execution_route",
            str(fund.get("fund_code") or "") == "001632" and catalog_size == 1,
            "930653.CSI → 001632，同指数＋被动＋C类＋隔离当日可执行",
        )

        orchestrator = ResearchOrchestrator(db)
        task_id = orchestrator.create_job("完整扫描")
        job = db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        if not job:
            raise ReleaseAcceptanceError("完整扫描任务夹具没有创建。")
        # The research is available to the user at 14:00, so its valid market
        # cutoff is the last session completed by that time.  Import under the
        # same explicit clock instead of borrowing the later settlement clock.
        with patch("app.services.local_now", return_value=morning):
            _offline_validated_research(
                db,
                task_id=task_id,
                job=job,
                index_row=index_row,
                fund=fund,
                data_cutoff=research_cutoff,
            )
        imported_job = db.row(
            "SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,)
        ) or {}
        candidate = db.row(
            "SELECT * FROM candidates WHERE task_id=? ORDER BY rank_no LIMIT 1",
            (task_id,),
        )
        if not candidate:
            raise ReleaseAcceptanceError("严格校验后的离线Pro结果没有生成候选。")
        candidate_id = int(candidate["id"])
        _append_check(
            report,
            "validated_protocol_import",
            bool(
                imported_job.get("status") == "已完成"
                and imported_job.get("stage") == "完成"
                and int(imported_job.get("completed_items") or 0)
                == int(imported_job.get("total_items") or 0)
                and str(candidate.get("fund_code") or "") == "001632"
                and str(candidate.get("research_tilt") or "") == "推荐买"
                and float(candidate.get("score") or 0) == 75
            ),
            {
                "summary": "固定Pro形状JSON已通过正式V3校验、当前目录/策略隔离和候选入库",
                "job_status": imported_job.get("status"),
                "job_stage": imported_job.get("stage"),
                "completed": imported_job.get("completed_items"),
                "total": imported_job.get("total_items"),
                "fund_code": candidate.get("fund_code"),
                "research_tilt": candidate.get("research_tilt"),
                "score": candidate.get("score"),
            },
        )
        current_run = latest_current_radar_run(db, as_of=morning)
        _append_check(
            report,
            "current_scan_isolated",
            bool(current_run and current_run.get("task_id") == task_id),
            f"完整目录快照 {int(job['total_items'])}/{int(job['total_items'])}",
        )

        drafts = DecisionDraftService(db)
        draft_id = drafts.add_candidate(candidate_id, as_of=morning)
        trades_before_close = int(
            (db.row("SELECT COUNT(*) AS n FROM trades") or {}).get("n") or 0
        )
        _append_check(
            report,
            "ai_advice_requires_user_draft",
            trades_before_close == 0,
            f"用户加入草稿#{draft_id}后仍没有真实交易",
        )

        settlement = DraftSettlementService(db)
        settlement.nav_provider = _FixedNavProvider(day, 1.0)
        enrichment = settlement.enrich([draft_id])
        _append_check(
            report,
            "published_nav_enrichment",
            enrichment.navs_resolved == 1 and enrichment.failed == 0,
            {
                "checked": enrichment.checked,
                "resolved": enrichment.navs_resolved,
                "failed": enrichment.failed,
            },
        )
        _append_check(
            report,
            "draft_does_not_create_holding",
            db.row(
                "SELECT id FROM holdings WHERE fund_code='001632' AND active=1"
            ) is None,
            "净值补全仍只是草稿",
        )

        close_result = drafts.finalize_after_close(as_of=closed)
        pending = db.row(
            """SELECT * FROM trades
                 WHERE source LIKE '今日草稿%' ORDER BY id DESC LIMIT 1"""
        )
        _append_check(
            report,
            "close_creates_pending_trade_only",
            bool(
                close_result == {"trades": 1, "recurring": 0, "total": 1}
                and pending
                and pending.get("status") == "待确认"
                and db.row(
                    "SELECT id FROM holdings WHERE fund_code='001632' AND active=1"
                ) is None
            ),
            "15:00后只写入一笔待确认交易，持仓仍未变化",
        )

        ledger = PortfolioLedgerService(db)
        posted = ledger.finalize_trade(
            int(pending["id"]),
            {
                "trade_date": day,
                "confirm_date": day,
                "fund_code": "001632",
                "action": "买入",
                "amount": 500,
                "units": 500,
                "nav": 1.0,
                "fee": 0,
                "nav_date": day,
                "status": "已确认",
                "rationale": "用户核对平台确认单后的隔离发布验收",
                "counts_toward_active_capital": 1,
            },
        )
        holding = db.row(
            "SELECT * FROM holdings WHERE fund_code='001632' AND active=1"
        )
        lot = db.row(
            "SELECT * FROM cost_lots WHERE trade_id=?", (int(pending["id"]),)
        )
        posting = db.row(
            "SELECT * FROM ledger_postings WHERE trade_id=?", (int(pending["id"]),)
        )
        _append_check(
            report,
            "confirmed_trade_posts_atomically",
            bool(
                posted.get("posted")
                and holding
                and lot
                and posting
                and float(holding["units"]) == 500
                and float(holding["cost"]) == 500
            ),
            "确认后同时生成持仓、成本批次和不可变过账",
        )

        capital = active_capital_summary(db)
        _append_check(
            report,
            "cash_ledger_reconciles",
            abs(float(capital["cash_before_reservations"]) - 7500.0) < 0.005,
            {
                "limit": float(capital["limit"]),
                "cash_before_reservations": float(capital["cash_before_reservations"]),
                "market_exposure": float(capital["market_exposure"]),
            },
        )

        # A release is not transaction-safe merely because a buy can be posted.
        # Exercise the exact user-facing partial-exit path as a second, separate
        # close-confirm cycle: draft -> pending trade -> immutable FIFO posting.
        sell_draft_id = drafts.add_manual(
            fund_code="001632",
            fund_name="天弘中证食品饮料ETF联接C",
            action="卖出",
            planned_amount=220,
            rationale="用户盘中决定部分卖出的隔离发布验收",
            pricing_date=day,
            as_of=closed,
            counts_toward_active_capital=1,
        )
        sell_close = drafts.finalize_after_close(as_of=closed)
        sell_pending = db.row(
            "SELECT * FROM trades WHERE id=(SELECT trade_id FROM decision_drafts WHERE id=?)",
            (sell_draft_id,),
        )
        if not sell_pending:
            raise ReleaseAcceptanceError("部分卖出草稿没有生成待确认交易。")
        sell_posted = ledger.finalize_trade(
            int(sell_pending["id"]),
            {
                "trade_date": day,
                "confirm_date": day,
                "fund_code": "001632",
                "action": "卖出",
                "amount": 220,
                "units": 200,
                "nav": 1.1,
                "fee": 0,
                "nav_date": day,
                "status": "已确认",
                "rationale": "用户核对平台卖出确认单后的隔离发布验收",
                "counts_toward_active_capital": 1,
            },
        )
        holding = db.row(
            "SELECT * FROM holdings WHERE fund_code='001632' AND active=1"
        )
        lot = db.row(
            "SELECT * FROM cost_lots WHERE trade_id=?", (int(pending["id"]),)
        )
        consumption = db.row(
            "SELECT * FROM lot_consumptions WHERE exit_trade_id=? ORDER BY sequence_no LIMIT 1",
            (int(sell_pending["id"]),),
        )
        capital = active_capital_summary(db)
        realized = [
            row for row in realized_exit_trades(db)
            if int(row["id"]) == int(sell_pending["id"])
        ]
        realized_metrics = strategy_review_metrics(realized, 8000)
        _append_check(
            report,
            "partial_sell_posts_fifo_cash_and_review",
            bool(
                sell_close == {"trades": 1, "recurring": 0, "total": 1}
                and sell_posted.get("posted")
                and holding
                and lot
                and consumption
                and abs(float(holding["units"]) - 300.0) < 0.005
                and abs(float(holding["cost"]) - 300.0) < 0.005
                and abs(float(lot["remaining_units"]) - 300.0) < 0.005
                and abs(float(consumption["units"]) - 200.0) < 0.005
                and abs(float(consumption["cost_basis"]) - 200.0) < 0.005
                and abs(float(capital["cash_before_reservations"]) - 7720.0) < 0.005
                and int(realized_metrics.get("trade_count") or 0) == 1
                and int(realized_metrics.get("unknown_trade_count") or 0) == 0
                and abs(float(realized_metrics.get("pnl_total") or 0) - 20.0) < 0.005
            ),
            {
                "remaining_units": float(holding["units"] if holding else 0),
                "remaining_cost": float(holding["cost"] if holding else 0),
                "active_cash": float(capital["cash_before_reservations"]),
                "realized_pnl": float(realized_metrics.get("pnl_total") or 0),
            },
        )

        immutable_edit_blocked = False
        altered = dict(sell_pending)
        altered.update({
            "status": "已确认",
            "confirm_date": day,
            "amount": 221,
            "units": 200,
            "nav": 1.1,
            "fee": 0,
            "nav_date": day,
        })
        try:
            ledger.assert_trade_mutation_allowed(int(sell_pending["id"]), altered)
        except LedgerError:
            immutable_edit_blocked = True
        _append_check(
            report,
            "posted_trade_requires_reversal_before_edit",
            immutable_edit_blocked,
            "已入账卖出金额不能直接改写；必须先撤销入账再建立更正交易",
        )

        review = holding_segment_review(db)
        _append_check(
            report,
            "review_reads_posted_holding_segment",
            len(review.get("open") or []) == 1,
            "复盘已读取1个真实开放持有段，不把草稿或待确认交易当成交",
        )
        workflow = daily_workflow_status(db, as_of=closed)
        _append_check(
            report,
            "seven_step_workflow_reaches_review",
            len(workflow.get("steps") or []) == 7
            and str(workflow["steps"][-1].get("state") or "") in {"ready", "done"},
            [str(step.get("status") or "") for step in workflow.get("steps") or []],
        )

        report["isolated_result"] = {
            "task_id": task_id,
            "candidate_id": candidate_id,
            "draft_id": draft_id,
            "trade_id": int(pending["id"]),
            "sell_draft_id": sell_draft_id,
            "sell_trade_id": int(sell_pending["id"]),
            "fund_code": "001632",
            "holding_units": float(holding["units"]),
            "holding_cost": float(holding["cost"]),
            "active_cash": float(capital["cash_before_reservations"]),
            "realized_pnl": float(realized_metrics.get("pnl_total") or 0),
            "open_review_segments": len(review.get("open") or []),
        }


def default_report_path() -> Path:
    folder = app_root() / "validation"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = local_now().strftime("%Y%m%d-%H%M%S")
    return folder / f"release-acceptance-{APP_VERSION}-{stamp}.json"


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def run_release_acceptance(
    *,
    report_path: Path | None = None,
    runtime_dir: Path | None = None,
    formal_db_path: Path | None = None,
) -> dict[str, Any]:
    """Run the release chain in a disposable DB and leave a JSON receipt."""

    report_path = Path(report_path) if report_path else default_report_path()
    runtime_dir = Path(runtime_dir) if runtime_dir else RUNTIME_DIR
    formal_db_path = Path(formal_db_path) if formal_db_path else DB_PATH
    runtime_dir.mkdir(parents=True, exist_ok=True)
    started = local_now()
    formal_before = _formal_database_snapshot(formal_db_path)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "app_version": APP_VERSION,
        "status": "RUNNING",
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": "",
        "mode": "isolated_offline_release_flow",
        "network_calls": 0,
        "formal_database_before": formal_before,
        "formal_database_after": {},
        "formal_database_unchanged": False,
        "temporary_database_deleted": False,
        "checks": [],
        "error": "",
        "report_path": str(report_path),
    }
    failure: BaseException | None = None
    temp_database: Path | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="sector-radar-release-acceptance-", dir=runtime_dir,
        ) as folder:
            temp_database = Path(folder) / "release-acceptance.sqlite3"
            database = Database(temp_database)
            _run_isolated_flow(database, report)
    except BaseException as exc:
        failure = exc
        report["status"] = "FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"

    report["temporary_database_deleted"] = bool(
        temp_database is not None and not temp_database.exists()
    )
    formal_after = _formal_database_snapshot(formal_db_path)
    report["formal_database_after"] = formal_after
    formal_integrity_ok = (
        not bool(formal_before.get("exists"))
        or (
            formal_before.get("integrity") == "ok"
            and formal_after.get("integrity") == "ok"
        )
    )
    formal_unchanged = (
        formal_before.get("exists") == formal_after.get("exists")
        and formal_before.get("sha256") == formal_after.get("sha256")
    )
    report["formal_database_unchanged"] = formal_unchanged
    report["checks"].append({
        "name": "formal_database_integrity",
        "passed": formal_integrity_ok,
        "detail": (
            "正式数据库验收前后完整性均为ok"
            if formal_before.get("exists") else "当前入口没有正式数据库，已跳过只读完整性检查"
        ),
    })
    report["checks"].append({
        "name": "formal_database_unchanged",
        "passed": formal_unchanged,
        "detail": "验收只使用临时数据库，正式数据库文件哈希保持不变",
    })
    report["checks"].append({
        "name": "temporary_database_deleted",
        "passed": bool(report["temporary_database_deleted"]),
        "detail": "隔离验收数据库已从运行缓存删除",
    })
    if failure is None and not formal_integrity_ok:
        failure = ReleaseAcceptanceError("正式数据库完整性检查没有通过。")
        report["status"] = "FAILED"
        report["error"] = str(failure)
    if failure is None and not formal_unchanged:
        failure = ReleaseAcceptanceError("验收期间正式数据库发生变化。")
        report["status"] = "FAILED"
        report["error"] = str(failure)
    if failure is None and not report["temporary_database_deleted"]:
        failure = ReleaseAcceptanceError("隔离验收数据库没有成功删除。")
        report["status"] = "FAILED"
        report["error"] = str(failure)
    if failure is None:
        report["status"] = "PASSED"
    report["finished_at"] = local_now().isoformat(timespec="seconds")
    _write_report(report_path, report)
    if failure is not None:
        raise ReleaseAcceptanceError(
            f"发布验收失败；报告已保存到：{report_path}；原因：{failure}"
        ) from failure
    return report
