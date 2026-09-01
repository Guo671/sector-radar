from __future__ import annotations

"""Rehearse the latest real research task without touching user money or data.

The validator makes a SQLite backup of the installed daily database, removes
the encrypted API key from that backup, reuses the latest completed real task
and its first currently funded candidate, then executes the same domain
services used by the UI: candidate -> draft -> NAV enrichment -> close ->
pending trade -> confirmed ledger posting -> holding/cash/cost lot -> review.
The disposable database is deleted before the report is written.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import traceback
from contextlib import ExitStack
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import APP_VERSION, LOCAL_TZ
from app.database import Database
from app.decision_drafts import DecisionDraftService
from app.investable import DraftSettlementService
from app.ledger import PortfolioLedgerService
from app.portfolio import active_capital_summary
from app.services import holding_segment_review, latest_current_radar_run


SCHEMA = "SECTOR_RADAR_CURRENT_TASK_FLOW_CLONE_V1"


class FixedNavProvider:
    name = "isolated_current_task_flow"

    def __init__(self, nav_date: str, nav: float = 1.0):
        self.nav_date = nav_date
        self.nav = float(nav)

    def fetch(self, _fund_code: str, _start: str, _end: str):
        return {self.nav_date: self.nav}, "https://example.test/isolated-nav"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def readonly_integrity(path: Path) -> str:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def clone_without_credentials(source: Path, destination: Path) -> None:
    uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as source_connection:
        source_connection.execute("PRAGMA query_only=ON")
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)
            destination_connection.execute(
                """UPDATE settings SET value=''
                     WHERE key='deepseek_api_key_dpapi'
                        OR lower(key) LIKE '%api%key%'
                        OR lower(key) LIKE '%secret%'
                        OR lower(key) LIKE '%credential%'"""
            )
            destination_connection.commit()


def add_check(report: dict[str, Any], name: str, passed: bool, detail: Any) -> None:
    report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})


def clear_clone_user_state(db: Database) -> dict[str, int]:
    """Remove only user-account state from the disposable clone.

    The real task, evidence, candidates, observations and strict fund catalog
    remain intact.  This prevents trades placed after the research finished
    from retrospectively consuming the candidate's historical allocation.
    """

    tables = (
        "lot_consumptions",
        "ledger_postings",
        "cost_lots",
        "portfolio_equity_snapshots",
        "active_cash_ledger",
        "decision_drafts",
        # Occurrences reference both trades and plans, so they must be removed
        # after cost lots but before either parent table.
        "recurring_occurrences",
        "trades",
        "holdings",
        "recurring_plans",
    )
    before = {
        table: int((db.row(f"SELECT COUNT(*) AS n FROM {table}") or {}).get("n") or 0)
        for table in tables
    }
    for table in tables:
        db.execute(f"DELETE FROM {table}")
    db.save_settings({"active_capital_limit": "8000", "active_cash_balance": "8000"})
    return before


def validate(source_database: Path, report_path: Path) -> dict[str, Any]:
    source_database = source_database.resolve()
    report_path = report_path.resolve()
    before_hash = sha256(source_database)
    before_integrity = readonly_integrity(source_database)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "app_version": APP_VERSION,
        "status": "FAILED",
        "source_database": str(source_database),
        "report_path": str(report_path),
        "started_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "network_calls": 0,
        "formal_database_before": {
            "sha256": before_hash,
            "integrity": before_integrity,
        },
        "checks": [],
        "result": {},
        "error": "",
        "error_traceback": "",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="sector-radar-current-flow-") as folder:
            clone_path = Path(folder) / "current-flow.sqlite3"
            clone_without_credentials(source_database, clone_path)
            db = Database(clone_path)
            job = db.row(
                """SELECT * FROM analysis_jobs
                     WHERE mode='完整扫描' AND status='已完成'
                     ORDER BY id DESC LIMIT 1"""
            )
            if not job:
                raise RuntimeError("正式库没有已完成的完整扫描任务。")
            task_id = str(job.get("task_id") or "")
            candidate = db.row(
                """SELECT * FROM candidates
                     WHERE task_id=? AND suggested_amount>0
                       AND execution_status='可执行'
                     ORDER BY rank_no,id LIMIT 1""",
                (task_id,),
            )
            if not candidate:
                raise RuntimeError("最新完整任务没有可用于隔离演练的正金额候选。")

            operation_day = date.fromisoformat(str(job.get("research_date") or "")[:10])
            morning = datetime.combine(operation_day, time(14, 0), tzinfo=LOCAL_TZ)
            closed = datetime.combine(operation_day, time(16, 0), tzinfo=LOCAL_TZ)
            day = operation_day.isoformat()
            candidate_id = int(candidate["id"])
            fund_code = str(candidate.get("fund_code") or "")
            amount = float(candidate.get("suggested_amount") or 0)

            isolated_counts = clear_clone_user_state(db)
            add_check(
                report,
                "user_state_removed_from_clone_only",
                all(
                    int((db.row(f"SELECT COUNT(*) AS n FROM {table}") or {}).get("n") or 0) == 0
                    for table in isolated_counts
                ),
                {"removed_rows": isolated_counts, "formal_database": "untouched"},
            )

            add_check(
                report,
                "latest_real_task_reused",
                int(job.get("completed_items") or 0) == int(job.get("total_items") or 0)
                and int(job.get("total_items") or 0) > 0,
                {
                    "task_id": task_id,
                    "completed": int(job.get("completed_items") or 0),
                    "total": int(job.get("total_items") or 0),
                    "universe_version": str(job.get("universe_version") or ""),
                    "ranking_policy_version": str(job.get("ranking_policy_version") or ""),
                },
            )
            add_check(
                report,
                "real_candidate_reused",
                bool(fund_code and amount > 0),
                {
                    "candidate_id": candidate_id,
                    "rank": int(candidate.get("rank_no") or 0),
                    "sector": str(candidate.get("sector_name") or ""),
                    "fund_code": fund_code,
                    "research_tilt": str(candidate.get("research_tilt") or candidate.get("status") or ""),
                    "suggested_amount": amount,
                },
            )
            current = latest_current_radar_run(db, as_of=morning)
            add_check(
                report,
                "candidate_was_actionable_at_research_time",
                bool(current and str(current.get("task_id") or "") == task_id),
                {"as_of": morning.isoformat(), "task_id": task_id},
            )

            cash_before = float(active_capital_summary(db)["cash_before_reservations"])
            clock_targets = (
                "app.services.local_now",
                "app.ledger.local_now",
                "app.investable.local_now",
                "app.platform_catalog.local_now",
                "app.decision_drafts.local_now",
            )
            with ExitStack() as stack:
                for target in clock_targets:
                    stack.enter_context(patch(target, return_value=closed))
                drafts = DecisionDraftService(db)
                draft_id = drafts.add_candidate(candidate_id, as_of=morning)
                draft = db.row("SELECT * FROM decision_drafts WHERE id=?", (draft_id,)) or {}
                add_check(
                    report,
                    "ai_candidate_becomes_user_draft_only",
                    str(draft.get("task_id") or "") == task_id
                    and int(draft.get("candidate_id") or 0) == candidate_id
                    and str(draft.get("status") or "") == "草稿"
                    and not bool(draft.get("trade_id")),
                    {"draft_id": draft_id, "task_id": draft.get("task_id"), "candidate_id": draft.get("candidate_id")},
                )

                settlement = DraftSettlementService(db)
                settlement.nav_provider = FixedNavProvider(day, 1.0)
                enriched = settlement.enrich([draft_id])
                add_check(
                    report,
                    "draft_nav_enriched_without_network",
                    enriched.navs_resolved == 1 and enriched.failed == 0,
                    {"resolved": enriched.navs_resolved, "failed": enriched.failed},
                )

                close_result = drafts.finalize_after_close(as_of=closed)
                pending = db.row(
                    "SELECT * FROM trades WHERE id=(SELECT trade_id FROM decision_drafts WHERE id=?)",
                    (draft_id,),
                ) or {}
                add_check(
                    report,
                    "close_creates_pending_trade",
                    int(close_result.get("trades") or 0) >= 1
                    and str(pending.get("status") or "") == "待确认",
                    {"close_result": close_result, "trade_id": pending.get("id")},
                )

                ledger = PortfolioLedgerService(db)
                trade_id = int(pending["id"])
                units = round(amount, 4)
                posted = ledger.finalize_trade(
                    trade_id,
                    {
                        "trade_date": day,
                        "confirm_date": day,
                        "fund_code": fund_code,
                        "action": "买入",
                        "amount": amount,
                        "units": units,
                        "nav": 1.0,
                        "fee": 0.0,
                        "nav_date": day,
                        "status": "已确认",
                        "rationale": "现行真实研究任务的无资金风险克隆验收",
                        "counts_toward_active_capital": 1,
                    },
                )
                holding = db.row(
                    "SELECT * FROM holdings WHERE fund_code=? AND active=1 ORDER BY id LIMIT 1",
                    (fund_code,),
                ) or {}
                lot = db.row("SELECT * FROM cost_lots WHERE trade_id=?", (trade_id,)) or {}
                posting = db.row("SELECT * FROM ledger_postings WHERE trade_id=?", (trade_id,)) or {}
                cash_after = float(active_capital_summary(db)["cash_before_reservations"])
                add_check(
                    report,
                    "confirmed_trade_posts_holding_cash_and_cost_lot",
                    bool(posted.get("posted") and holding and lot and posting)
                    and abs(float(holding.get("units") or 0) - units) < 0.005
                    and abs(float(lot.get("gross_amount") or 0) - amount) < 0.005
                    and abs(cash_after - (cash_before - amount)) < 0.005,
                    {
                        "trade_id": trade_id,
                        "holding_units": float(holding.get("units") or 0),
                        "cash_before": cash_before,
                        "cash_after": cash_after,
                        "cost_lot_amount": float(lot.get("gross_amount") or 0),
                    },
                )

                review = holding_segment_review(db)
                open_rows = [
                    row for row in (review.get("open") or [])
                    if str(row.get("fund_code") or "") == fund_code
                ]
                add_check(
                    report,
                    "review_reads_confirmed_holding_segment",
                    len(open_rows) == 1,
                    {"fund_code": fund_code, "open_segments": len(open_rows)},
                )
                report["result"] = {
                    "task_id": task_id,
                    "candidate_id": candidate_id,
                    "draft_id": draft_id,
                    "trade_id": trade_id,
                    "fund_code": fund_code,
                    "amount": amount,
                    "cash_before": cash_before,
                    "cash_after": cash_after,
                    "review_open_segments": len(open_rows),
                }

        report["temporary_database_deleted"] = True
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["error_traceback"] = traceback.format_exc()
        report["temporary_database_deleted"] = not any(
            Path(tempfile.gettempdir()).glob("sector-radar-current-flow-*")
        )

    after_hash = sha256(source_database)
    after_integrity = readonly_integrity(source_database)
    report["formal_database_after"] = {
        "sha256": after_hash,
        "integrity": after_integrity,
    }
    add_check(
        report,
        "formal_database_unchanged",
        before_hash == after_hash and before_integrity == after_integrity == "ok",
        {"before": before_hash, "after": after_hash, "integrity": after_integrity},
    )
    report["finished_at"] = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    if not report["error"] and all(item["passed"] for item in report["checks"]):
        report["status"] = "PASSED"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="现行真实任务的无资金风险克隆闭环验收")
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    report = validate(arguments.source_db, arguments.report)
    print(json.dumps({
        "status": report["status"],
        "checks": len(report["checks"]),
        "failed": [item["name"] for item in report["checks"] if not item["passed"]],
        "error": report["error"],
        "report": report["report_path"],
    }, ensure_ascii=False))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
