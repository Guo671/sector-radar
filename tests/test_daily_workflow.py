from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from app.services import daily_workflow_status


class WorkflowDB:
    def __init__(self, *, market_days, drafts=(), recurring=(), pending_trades=0, snapshots=1,
                 market_job=None, checked_days=None, stale_drafts=0):
        self.market_days = dict(market_days)
        self.drafts = list(drafts)
        self.recurring = dict(recurring)
        self.pending_trades = int(pending_trades)
        self.snapshots = int(snapshots)
        self.market_job = market_job
        self.checked_days = list(checked_days or [])
        self.stale_drafts = int(stale_drafts)
        self.queries = []

    def setting(self, key, default=""):
        values = {
            "trading_calendar_closed_dates_json": "[]",
            "trading_calendar_open_dates_json": "[]",
            "trading_calendar_source_url": "",
            "decision_close_hour": "15",
        }
        return values.get(key, default)

    def rows(self, sql, params=()):
        if "GROUP BY p.universe_id" in sql:
            return [
                {"universe_id": item, "latest_day": self.market_days.get(item, "")}
                for item in params
            ]
        if "FROM candidates" in sql:
            return [
                {
                    "id": 1, "research_tilt": "推荐买", "suggested_amount": 1000,
                    "fund_code": "000001", "execution_status": "可执行",
                },
                {
                    "id": 2, "research_tilt": "观察", "suggested_amount": 0,
                    "fund_code": "000002", "execution_status": "可执行",
                },
            ]
        if "FROM decision_drafts" in sql:
            return list(self.drafts)
        if "FROM recurring_occurrences GROUP BY status" in sql:
            return [{"status": key, "n": value} for key, value in self.recurring.items()]
        raise AssertionError(f"Unexpected rows query: {sql}")

    def row(self, sql, params=()):
        self.queries.append(sql)
        if "FROM sector_metrics" in sql:
            if len(params) == 1:
                return {
                    "n": len(self.checked_days),
                    "min_day": min(self.checked_days, default=""),
                    "max_day": max(self.checked_days, default=""),
                }
            expected = params[1]
            ready = sum(value == expected for value in self.market_days.values())
            return {"n": ready, "latest_day": max(self.market_days.values(), default="")}
        if "FROM decision_drafts WHERE trade_date<" in sql:
            return {"n": self.stale_drafts}
        if "FROM trades WHERE status='待确认'" in sql:
            return {"n": self.pending_trades}
        if "FROM portfolio_equity_snapshots" in sql:
            return {"n": self.snapshots}
        if "FROM analysis_jobs" in sql and "全目录行情更新" in sql:
            return self.market_job
        raise AssertionError(f"Unexpected row query: {sql}")


class DailyWorkflowStatusTests(unittest.TestCase):
    now = datetime(2026, 8, 12, 13, 0)
    catalog = [{"index_code": "C000001"}, {"index_code": "C000002"}]
    complete_run = {
        "task_id": "today-current-catalog", "universe_total": 2, "analyzed_total": 2,
        "data_cutoff": "2026-08-11",
    }

    def _status(self, db, run, *, as_of=None):
        with patch("app.services.PlatformCatalogService") as catalog_service, patch(
            "app.services.research_run_status", return_value={"current": run}
        ):
            catalog_service.return_value.radar_universe.return_value = list(self.catalog)
            return daily_workflow_status(db, as_of=as_of or self.now)

    def test_complete_current_catalog_builds_seven_clickable_states(self):
        db = WorkflowDB(
            market_days={"C000001": "2026-08-11", "C000002": "2026-08-11"},
            drafts=[{"status": "草稿"}],
            recurring={"已入账": 16},
        )

        result = self._status(db, self.complete_run)

        self.assertEqual(len(result["steps"]), 7)
        self.assertEqual([item["title"] for item in result["steps"]], [
            "更新数据", "AI扫描", "查看推荐", "加入草稿", "收盘确认", "更新持仓", "查看复盘",
        ])
        self.assertEqual(result["steps"][0]["state"], "done")
        self.assertEqual(result["steps"][1]["state"], "done")
        self.assertEqual(result["steps"][2]["detail"], "主榜 2｜正向 1｜可执行 1")
        self.assertEqual(result["steps"][3]["status"], "存活 1 项")
        self.assertEqual(result["next_step"]["key"], "recommend")

    def test_one_stale_index_forces_update_data_before_scan_route(self):
        db = WorkflowDB(
            market_days={"C000001": "2026-08-11", "C000002": "2026-08-08"},
        )

        result = self._status(db, None)

        self.assertEqual(result["market_ready"], 1)
        self.assertEqual(result["steps"][0]["status"], "需更新")
        self.assertEqual(result["steps"][0]["state"], "todo")
        self.assertEqual(result["next_step"]["key"], "data")
        self.assertEqual(result["steps"][2]["state"], "locked")

    def test_completed_scan_uses_strict_metric_evidence_not_provider_name(self):
        db = WorkflowDB(
            market_days={"C000001": "2026-08-11", "C000002": "2026-08-11"},
        )

        result = self._status(db, self.complete_run)

        self.assertEqual(result["market_ready"], 2)
        self.assertEqual(result["steps"][0]["state"], "done")

    def test_recurring_and_pending_trades_keep_holding_update_as_real_todo(self):
        db = WorkflowDB(
            market_days={"C000001": "2026-08-11", "C000002": "2026-08-11"},
            recurring={"已入账": 16, "可确认入账": 2, "待净值": 2},
            pending_trades=3,
        )

        result = self._status(db, self.complete_run)
        holding_step = result["steps"][5]

        self.assertEqual(holding_step["state"], "todo")
        self.assertEqual(holding_step["status"], "仍有待办")
        self.assertIn("待确认交易 3", holding_step["detail"])
        self.assertIn("定投待办 4（可确认 2）", holding_step["detail"])
        self.assertEqual(holding_step["page"], "trades")
        self.assertEqual(result["next_step"]["key"], "holdings")
        self.assertIn("账本待办优先", result["next_reason"])
        self.assertTrue(any("status='待确认'" in sql for sql in db.queries))
        self.assertFalse(any("status<>'已确认'" in sql for sql in db.queries))

    def test_recurring_only_holding_todo_opens_recurring_page(self):
        db = WorkflowDB(
            market_days={"C000001": "2026-08-11", "C000002": "2026-08-11"},
            recurring={"已入账": 16, "待净值": 2},
        )

        result = self._status(db, self.complete_run)

        self.assertEqual(result["steps"][5]["page"], "recurring")

    def test_complete_market_check_one_session_behind_waits_for_official_publication(self):
        db = WorkflowDB(
            market_days={"C000001": "2026-08-11", "C000002": "2026-08-11"},
            market_job={
                "task_id": "today-market-all", "status": "行情更新完成",
                "total_items": 2, "completed_items": 2,
            },
            checked_days=["2026-08-11", "2026-08-11"],
        )

        result = self._status(db, None, as_of=datetime(2026, 8, 12, 17, 0))

        self.assertTrue(result["publication_pending"])
        self.assertEqual(result["steps"][0]["status"], "已检查·待发布")
        self.assertEqual(result["steps"][0]["state"], "waiting")
        self.assertIn("无需重复点击", result["steps"][0]["detail"])
        self.assertEqual(result["steps"][1]["status"], "暂缓扫描")
        self.assertEqual(result["steps"][1]["state"], "locked")
        self.assertEqual(result["next_step"]["key"], "draft")

    def test_stale_draft_is_explicit_and_prioritised_without_becoming_today_operation(self):
        db = WorkflowDB(
            market_days={"C000001": "2026-08-11", "C000002": "2026-08-11"},
            stale_drafts=2,
        )

        result = self._status(db, self.complete_run)

        draft_step = result["steps"][3]
        self.assertEqual(draft_step["status"], "跨日待处理 2 项")
        self.assertIn("确认原日扣款", draft_step["detail"])
        self.assertIn("续做或放弃", draft_step["detail"])
        self.assertEqual(draft_step["state"], "todo")
        self.assertEqual(result["next_step"]["key"], "draft")
        self.assertEqual(result["stale_drafts"], 2)
        self.assertIn("跨日草稿优先", result["next_reason"])


if __name__ == "__main__":
    unittest.main()
