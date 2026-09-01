import json
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from unittest.mock import patch

from app.config import local_now
from app.catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from app.csi_catalog import CSI_DAILY_TARGETS
from app.database import Database
from app.fund_catalog_sync import CSIFieldFundSyncService, CSIOfficialProductService
from app.fund_discovery import FundDiscoveryError
from app.orchestrator import ResearchOrchestrator
from app.platform_catalog import FundExecutionCheck, PlatformCatalogService
from app.protocol import build_task_package
from app.recurring import RecurringInvestmentService
from app.services import (
    _current_ranking_policy_snapshot, dashboard_metrics, import_radar_payload,
    latest_current_radar_run, latest_readable_radar_run, research_run_status,
)
from app.trading_calendar import ChinaTradingCalendar
from app.universe import UNIVERSE_VERSION


class DictNavProvider:
    name = "test_nav"

    def __init__(self, values):
        self.values = values

    def fetch(self, _code, _start, _end):
        return dict(self.values), "https://example.test/nav"


class SafetyCatalogCalendarTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.db = Database(self.root / "test.sqlite3")
        self.catalog = PlatformCatalogService(self.db)
        self.index = self.catalog.list_indices()[0]
        self._insert_strict_c("199991")
        self.db.save_settings({
            "csi_fund_sync_algorithm_version": STRICT_FIELD_SYNC_ALGORITHM_VERSION,
        })
        self.catalog.refresh_admission(self.index["id"])

    def tearDown(self):
        self.folder.cleanup()

    def test_task_package_exports_only_strategy_whitelist(self):
        self.db.save_settings({
            "deepseek_api_key_dpapi": "SECRET_ENCRYPTED_BLOB",
            "recurring_last_sync_at": "LOCAL_DEVICE_STATE",
            "active_capital_limit": "8123",
        })
        task_dir = self.root / "tasks"
        task_dir.mkdir()
        with patch("app.protocol.TASK_DIR", task_dir):
            text, path = build_task_package(self.db, "night")
        self.assertTrue(path.exists())
        self.assertIn('"active_capital_limit": "8123"', text)
        self.assertNotIn("SECRET_ENCRYPTED_BLOB", text)
        self.assertNotIn("deepseek_api_key_dpapi", text)
        self.assertNotIn("LOCAL_DEVICE_STATE", text)
        self.assertNotIn("recurring_last_sync_at", text)

    def test_task_package_never_reuses_archived_research(self):
        old_day = (local_now().date() - timedelta(days=1)).isoformat()
        payload = self._web_payload("archived-web-pack", f"{old_day} 22:00 +08:00")
        payload["market_summary"] = "ARCHIVED_186_MUST_NOT_LEAK"
        import_radar_payload(self.db, payload, json.dumps(payload, ensure_ascii=False), [])
        task_dir = self.root / "archived-tasks"
        task_dir.mkdir()
        with patch("app.protocol.TASK_DIR", task_dir):
            text, _path = build_task_package(self.db, "night")
        self.assertNotIn("ARCHIVED_186_MUST_NOT_LEAK", text)
        self.assertIn('"previous_run": null', text)
        self.assertIn('"previous_candidates": []', text)

    def test_exchange_holidays_cutoff_and_qdii_delay(self):
        calendar = ChinaTradingCalendar()
        self.assertFalse(calendar.is_trading_day(date(2026, 10, 2)))
        self.assertTrue(calendar.is_trading_day(date(2026, 10, 8)))
        before = calendar.resolve_order(datetime(2026, 10, 8, 14, 59), fund_kind="普通场外")
        self.assertEqual(before.pricing_date, date(2026, 10, 8))
        self.assertEqual(before.estimated_confirm_date, date(2026, 10, 9))
        cutoff = calendar.resolve_order(datetime(2026, 10, 8, 15, 0), fund_kind="QDII")
        self.assertEqual(cutoff.pricing_date, date(2026, 10, 9))
        self.assertEqual(cutoff.estimated_confirm_date, date(2026, 10, 13))

    def test_calendar_overrides_are_source_backed_and_reloadable(self):
        calendar = ChinaTradingCalendar.update_overrides(
            self.db,
            closed_dates=["2026-08-10"],
            open_dates=["2026-08-15"],
            source_url="https://www.sse.com.cn/example-notice",
            retrieved_at=datetime(2026, 8, 9, 12, 0),
        )
        self.assertFalse(calendar.is_trading_day(date(2026, 8, 10)))
        self.assertTrue(calendar.is_trading_day(date(2026, 8, 15)))
        reloaded = ChinaTradingCalendar.from_db(self.db)
        self.assertFalse(reloaded.is_trading_day(date(2026, 8, 10)))
        self.assertTrue(reloaded.is_trading_day(date(2026, 8, 15)))

    def test_recurring_missing_nav_rolls_to_first_real_pricing_date(self):
        service = RecurringInvestmentService(
            self.db, DictNavProvider({"2026-08-06": 8.0}), ChinaTradingCalendar(),
        )
        service.generate_due(date(2026, 8, 5))
        result = service.sync_pending_navs()
        row = self.db.row("SELECT * FROM recurring_occurrences")
        self.assertEqual(result["shifted"], 1)
        self.assertEqual(row["pricing_date"], "2026-08-06")
        self.assertEqual(row["confirm_date"], "2026-08-10")  # QDII T+2 trading days
        self.assertEqual(row["status"], "可确认入账")

    def test_trading_day_plan_skips_exchange_holiday(self):
        self.db.execute(
            "UPDATE recurring_plans SET start_date='2026-09-24',end_date='2026-09-28'"
        )
        service = RecurringInvestmentService(self.db, DictNavProvider({}), ChinaTradingCalendar())
        service.generate_due(date(2026, 9, 28))
        dates = [row["scheduled_date"] for row in self.db.rows(
            "SELECT scheduled_date FROM recurring_occurrences ORDER BY scheduled_date"
        )]
        self.assertEqual(dates, ["2026-09-24", "2026-09-28"])

    def _insert_strict_c(self, code="100001"):
        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO investable_funds(
                   investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                   public_buyable_status,active,passive_verified,radar_eligible,relation_kind,
                    source_url,verified_at,platform_evidence_url,platform_checked_at,
                    verification_algorithm)
                VALUES(?,?,?,'被动ETF联接','C','公开渠道可申购',1,1,0,'同一指数',?,?,?,?,?)""",
            (
                self.index["id"], code, "严格同指数ETF联接C", "https://fund.example/profile",
                now, "https://fund.example/buyability", now,
                STRICT_FIELD_SYNC_ALGORITHM_VERSION,
            ),
        )
        return code

    def test_executable_fund_has_ttl_and_lightweight_recheck(self):
        code = self._insert_strict_c()
        now = local_now()
        fresh = self.catalog.check_fund_execution(self.index["id"], code, as_of=now)
        self.assertTrue(fresh.executable, fresh.reasons)
        stale = self.catalog.check_fund_execution(
            self.index["id"], code, as_of=now + timedelta(days=8),
        )
        self.assertFalse(stale.executable)
        self.assertEqual(stale.state, "TTL已过期")

        blocked = self.catalog.revalidate_fund_execution(
            self.index["id"], code, as_of=now + timedelta(days=8),
            checker=lambda fund: {
                "fund_code": fund["fund_code"], "status": "暂停申购",
                "source_url": "https://fund.example/current-notice",
            },
        )
        self.assertFalse(blocked.executable)
        self.assertEqual(blocked.state, "暂停申购")

        limited = self.catalog.revalidate_fund_execution(
            self.index["id"], code, as_of=now + timedelta(days=8, minutes=1),
            intended_amount=500,
            checker=lambda fund: {
                "fund_code": fund["fund_code"], "status": "限购", "purchase_limit_amount": 1000,
                "source_url": "https://fund.example/current-limit",
            },
        )
        self.assertTrue(limited.executable, limited.reasons)
        too_large = self.catalog.check_fund_execution(
            self.index["id"], code, intended_amount=1500,
            as_of=now + timedelta(days=8, minutes=2), require_same_day=True,
        )
        self.assertFalse(too_large.executable)
        self.assertEqual(too_large.state, "限购待核对")

    def test_paid_scan_is_blocked_until_every_index_has_same_day_execution(self):
        code = self._insert_strict_c()
        stale = (local_now() - timedelta(days=2)).isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE investable_funds SET active=0,radar_eligible=0 "
            "WHERE investable_index_id=? AND fund_code<>?",
            (self.index["id"], code),
        )
        self.db.execute(
            "UPDATE investable_funds SET platform_checked_at=?,verified_at=? "
            "WHERE investable_index_id=?",
            (stale, stale, self.index["id"]),
        )
        self.catalog.refresh_admission(self.index["id"])
        before = self.catalog.execution_summary()
        self.assertEqual(before["total"], 1)
        self.assertEqual(before["ready"], 0)
        with self.assertRaisesRegex(ValueError, "付费扫描前置门槛"):
            ResearchOrchestrator(self.db).create_job("完整扫描")
        self.assertEqual(
            self.db.row("SELECT COUNT(*) n FROM analysis_jobs")["n"], 0,
        )

        calls = []
        result = self.catalog.revalidate_radar_execution(
            checker=lambda fund: calls.append(fund["fund_code"]) or {
                "fund_code": fund["fund_code"],
                "status": "公开渠道可申购",
                "source_url": "https://fund.example/current",
            },
        )
        self.assertEqual(result["ready"], 1)
        self.assertEqual(calls, [code])
        task_id = ResearchOrchestrator(self.db).create_job("完整扫描")
        self.assertTrue(task_id)

    def test_daily_execution_stop_prevents_first_fund_status_request(self):
        calls = []
        result = self.catalog.revalidate_radar_execution(
            checker=lambda fund: calls.append(fund["fund_code"]) or {},
            stop_requested=lambda: True,
        )
        self.assertTrue(result["stopped"])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(calls, [])

    def test_daily_execution_refresh_is_bounded_concurrent_and_deduplicated(self):
        self.db.save_settings({"fund_execution_refresh_workers": "3"})
        rows = [
            {
                "platform_index_id": position,
                "index_code": f"U{position:03d}",
                "ant_index_code": f"{930000 + position}.CSI",
                "official_index_name": f"并发测试{position}",
            }
            for position in range(1, 7)
        ]
        codes = ["900001", "900001", "900003", "900004", "900005", "900006"]
        strict = {
            position: [{"fund_code": codes[position - 1]}]
            for position in range(1, 7)
        }
        lock = Lock()
        calls: list[str] = []
        active = 0
        max_active = 0

        def checker(fund):
            nonlocal active, max_active
            with lock:
                calls.append(fund["fund_code"])
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return {
                "fund_code": fund["fund_code"],
                "status": "公开渠道可申购",
                "source_url": "https://fund.example/current",
            }

        unavailable = FundExecutionCheck(False, "TTL已过期", (), "", "")
        executable = FundExecutionCheck(True, "可执行", (), "", "")
        with (
            patch.object(self.catalog, "radar_universe", return_value=rows),
            patch.object(self.catalog, "strict_catalog_funds_by_index", return_value=strict),
            patch.object(self.catalog, "evaluate_fund_execution", return_value=unavailable),
            patch.object(self.catalog, "revalidate_fund_execution", return_value=executable),
            patch.object(self.catalog, "refresh_admission"),
            patch.object(self.catalog, "execution_summary", return_value={
                "total": 6, "ready": 6, "ready_ids": [], "missing": [],
            }),
        ):
            result = self.catalog.revalidate_radar_execution(checker=checker)

        self.assertEqual(result["checked"], 6)
        self.assertEqual(result["network_attempts"], 5)
        self.assertEqual(result["network_funds"], 5)
        self.assertEqual(calls.count("900001"), 1)
        self.assertGreaterEqual(max_active, 2)
        self.assertLessEqual(max_active, 3)

    def test_collection_states_and_force_refresh_are_distinct(self):
        code = self.index["official_index_code"]
        calls = []

        def official(_url):
            calls.append(_url)
            return json.dumps({"code": "200", "data": [{
                "productCode": "510001", "fundName": "测试行业ETF", "indexCode": code,
            }]}).encode()

        official_service = CSIOfficialProductService(self.db, downloader=official)
        official_service.sync_one(self.index)
        state = next(row for row in self.catalog.list_indices() if row["id"] == self.index["id"])
        self.assertEqual(state["collection_state"], "官方根产品")
        with patch(
            "app.fund_catalog_sync.PlatformCatalogService.list_indices", return_value=[self.index],
        ):
            skipped = official_service.sync_all(incomplete_only=True)
            forced = official_service.sync_all(incomplete_only=True, force_refresh=True)
        self.assertEqual(skipped.skipped, 1)
        self.assertEqual(forced.completed, 1)
        self.assertEqual(len(calls), 2)

        search_row = [{
            "CODE": "000001", "NAME": "相似ETF联接C",
            "FundBaseInfo": {"FTYPE": "ETF联接基金", "ISBUY": "1"},
        }]
        field_service = CSIFieldFundSyncService(
            self.db,
            searcher=lambda _query: search_row,
            profile_loader=lambda _code: {
                "full_name": "相似ETF联接C", "target_index": "另一条指数",
                "benchmark": "另一条指数收益率", "source_url": "https://fund.example/000001",
            },
        )
        self.assertEqual(field_service.sync_one(self.index), 0)
        retained = next(row for row in self.catalog.list_indices() if row["id"] == self.index["id"])
        self.assertNotEqual(retained["collection_state"], "已确认无C")
        self.assertTrue(self.catalog.strict_catalog_funds(self.index["id"]))

    def test_network_failure_is_not_mislabelled_as_no_c(self):
        service = CSIFieldFundSyncService(
            self.db,
            searcher=lambda _query: (_ for _ in ()).throw(FundDiscoveryError("network down")),
            profile_loader=lambda _code: {},
        )
        with self.assertRaises(Exception):
            service.sync_one(self.index)
        row = next(item for item in self.catalog.list_indices() if item["id"] == self.index["id"])
        self.assertEqual(row["collection_state"], "网络失败")

    def _web_payload(self, task_id: str, cutoff: str) -> dict:
        audit = [{
            "id": item["index_code"],
            "name": item["index_name"],
            "category": item["category"],
            "proxy_code": item["csi_index_code"],
            "proxy_name": item["index_name"],
            "returns_pct": {"d1": 0, "d3": 0, "d5": 0, "d10": 0, "d20": 0},
        } for item in self.catalog.radar_universe()]
        return {
            "task_id": task_id,
            "task_status": "COMPLETE",
            "universe_version": UNIVERSE_VERSION,
            "data_cutoff": cutoff,
            "market_regime": "测试",
            "universe_total": len(audit),
            "analyzed_total": len(audit),
            "audit": audit,
            "market_summary": "网页研究导入测试",
            "candidates": [],
            "fund_updates": [],
        }

    def test_current_complete_web_import_creates_exact_actionable_job(self):
        today = local_now().date().isoformat()
        cutoff = ChinaTradingCalendar.from_db(self.db).latest_completed_trading_day(local_now()).isoformat()
        payload = self._web_payload("web-current-complete", f"{cutoff} 15:00 +08:00")
        policy_version, policy_json = _current_ranking_policy_snapshot(self.db)
        payload.update({
            "protocol_version": "3.0",
            "ranking_policy_version": policy_version,
            "ranking_policy": json.loads(policy_json),
        })
        for rank,item in enumerate(payload["audit"],start=1):
            item.update({
                "status":"ANALYZED", "local_rank":rank, "pool_route":"趋势确认·不过热",
                "research_tilt":"观察", "timing_status":"等待回调", "execution_status":"可执行",
                "trend_score":50, "entry_score":50, "crowding_risk_score":30,
                "data_confidence":90, "opportunity_score":50, "recommendation_score":45,
                "score_components":{"policy_version":policy_version,"freshness_ok":True},
            })
        import_radar_payload(self.db, payload, json.dumps(payload, ensure_ascii=False), [])
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (payload["task_id"],))
        self.assertIsNotNone(job)
        self.assertEqual(job["mode"], "完整扫描")
        self.assertEqual(job["status"], "已完成")
        self.assertEqual(job["research_date"], today)
        self.assertEqual(
            json.loads(job["universe_ids_json"]),
            [item["index_code"] for item in self.catalog.radar_universe()],
        )
        current = latest_current_radar_run(self.db)
        self.assertIsNotNone(current)
        self.assertEqual(current["task_id"], payload["task_id"])

    def test_previous_session_research_is_visible_but_never_actionable(self):
        cutoff_day = ChinaTradingCalendar.from_db(self.db).latest_completed_trading_day(local_now())
        payload = self._web_payload("publication-lagged", f"{cutoff_day.isoformat()} 15:00 +08:00")
        policy_version, policy_json = _current_ranking_policy_snapshot(self.db)
        payload.update({
            "protocol_version": "3.0",
            "ranking_policy_version": policy_version,
            "ranking_policy": json.loads(policy_json),
        })
        for rank, item in enumerate(payload["audit"], start=1):
            item.update({
                "status": "ANALYZED", "local_rank": rank, "pool_route": "观察池",
                "research_tilt": "观察", "timing_status": "行情待更新", "execution_status": "行情待更新",
                "trend_score": 50, "entry_score": 50, "crowding_risk_score": 30,
                "data_confidence": 40, "opportunity_score": 45, "recommendation_score": 40,
                "score_components": {"policy_version": policy_version, "freshness_ok": False},
            })
        import_radar_payload(self.db, payload, json.dumps(payload, ensure_ascii=False), [])
        previous = ChinaTradingCalendar.from_db(self.db).previous_trading_day(cutoff_day).isoformat()
        self.db.execute(
            "UPDATE radar_runs SET data_cutoff=? WHERE task_id=?",
            (previous, payload["task_id"]),
        )

        self.assertIsNone(latest_current_radar_run(self.db))
        readable = latest_readable_radar_run(self.db)
        self.assertIsNotNone(readable)
        self.assertEqual(readable["task_id"], payload["task_id"])
        state = research_run_status(self.db)
        self.assertTrue(state["read_only"])
        self.assertFalse(state["actionable"])
        self.assertEqual(state["research"]["task_id"], payload["task_id"])
        self.assertEqual(dashboard_metrics(self.db, research_state=state)["latest_run"]["task_id"], payload["task_id"])

    def test_old_ranking_formula_isolated_with_exact_upgrade_reason(self):
        """A same-catalog V3 success must never masquerade as missing data."""
        orchestrator = ResearchOrchestrator(self.db)
        task_id = orchestrator.create_job("无API模拟")
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        total = int(job["total_items"])
        old_policy = {
            "version": "four-dimension-v3",
            "trend_weight": 0.25,
            "entry_weight": 0.25,
            "catalyst_weight": 0.20,
            "relative_strength_weight": 0.15,
            "volume_breadth_weight": 0.15,
            "crowding_penalty": 0.45,
            "data_confidence_min": 70.0,
            "candidate_min_score": 45.0,
            "pool_min": 20,
            "pool_max": 30,
            "pool_cluster_max": 4,
            "display_cluster_max": 2,
            "funded_cluster_max": 1,
        }
        old_policy_json = json.dumps(
            old_policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        today = local_now().date().isoformat()
        now = local_now().isoformat(timespec="seconds")
        cutoff = ChinaTradingCalendar.from_db(self.db).latest_completed_trading_day(local_now()).isoformat()
        self.db.execute(
            """UPDATE analysis_jobs
                  SET mode='完整扫描',stage='完成',status='已完成',completed_items=?,
                      ranking_policy_version='four-dimension-v3',ranking_policy_json=?,
                      research_date=?,updated_at=?
                WHERE task_id=?""",
            (total, old_policy_json, today, now, task_id),
        )
        self.db.execute(
            """INSERT INTO radar_runs(
                   task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                   summary,raw_json,imported_at,ranking_policy_version,ranking_policy_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, cutoff, "测试", total, total, "旧策略研究", "{}", now,
                "four-dimension-v3", old_policy_json,
            ),
        )

        state = research_run_status(self.db)
        self.assertFalse(state["actionable"])
        self.assertIsNone(state["research"])
        self.assertTrue(state["requires_new_scan"])
        self.assertEqual(state["current_policy_version"], "four-dimension-v4")
        self.assertEqual(state["latest_complete"]["task_id"], task_id)
        self.assertEqual(state["latest_complete"]["ranking_policy_version"], "four-dimension-v3")
        self.assertIn("使用 four-dimension-v3", state["reason"])
        self.assertIn("当前软件要求 four-dimension-v4", state["reason"])
        self.assertIn("重新完成一次完整扫描", state["reason"])

    def test_old_or_inexact_web_import_stays_archived_without_job(self):
        old_day = (local_now().date() - timedelta(days=1)).isoformat()
        old = self._web_payload("web-old", f"{old_day} 22:00 +08:00")
        import_radar_payload(self.db, old, json.dumps(old, ensure_ascii=False), [])
        self.assertIsNone(self.db.row("SELECT * FROM analysis_jobs WHERE task_id='web-old'"))

        incomplete = self._web_payload(
            "web-inexact", f"{local_now().date().isoformat()} 14:30 +08:00",
        )
        incomplete["audit"] = incomplete["audit"][:-1]
        import_radar_payload(self.db, incomplete, json.dumps(incomplete, ensure_ascii=False), [])
        self.assertIsNone(self.db.row("SELECT * FROM analysis_jobs WHERE task_id='web-inexact'"))
        self.assertIsNone(latest_current_radar_run(self.db))

    def test_legacy_hundred_and_186_item_results_are_archived_after_catalog_shrinks(self):
        self.assertLess(len(self.catalog.radar_universe()), len(CSI_DAILY_TARGETS))
        today = local_now().date().isoformat()
        payload = self._web_payload("legacy-100-after-shrink", f"{today} 14:30 +08:00")
        payload["audit"] = [{
            "id": item.universe_id,
            "name": item.name,
            "category": item.industry,
            "proxy_code": item.code,
            "proxy_name": item.name,
            "returns_pct": {"d1": 0, "d3": 0, "d5": 0, "d10": 0, "d20": 0},
        } for item in CSI_DAILY_TARGETS]
        payload["universe_total"] = len(payload["audit"])
        payload["analyzed_total"] = len(payload["audit"])
        payload["market_summary"] = "旧100项目录不得执行"
        import_radar_payload(self.db, payload, json.dumps(payload, ensure_ascii=False), [])
        self.assertIsNone(self.db.row(
            "SELECT * FROM analysis_jobs WHERE task_id='legacy-100-after-shrink'"
        ))
        self.assertIsNone(latest_current_radar_run(self.db))
        self.assertEqual(
            self.db.row("SELECT summary FROM radar_runs WHERE task_id='legacy-100-after-shrink'")["summary"],
            "旧100项目录不得执行",
        )

        legacy_rows = self.db.rows(
            "SELECT index_code,index_name,category FROM universe WHERE index_code LIKE 'U%' ORDER BY index_code"
        )
        self.assertEqual(len(legacy_rows), 186)
        legacy_186 = self._web_payload("legacy-186-after-shrink", f"{today} 14:31 +08:00")
        legacy_186["audit"] = [{
            "id": row["index_code"], "name": row["index_name"], "category": row["category"],
            "proxy_code": row["index_code"], "proxy_name": row["index_name"],
            "returns_pct": {"d1": 0, "d3": 0, "d5": 0, "d10": 0, "d20": 0},
        } for row in legacy_rows]
        legacy_186["universe_total"] = 186
        legacy_186["analyzed_total"] = 186
        legacy_186["market_summary"] = "旧186项目录不得执行"
        import_radar_payload(self.db, legacy_186, json.dumps(legacy_186, ensure_ascii=False), [])
        self.assertIsNone(self.db.row(
            "SELECT * FROM analysis_jobs WHERE task_id='legacy-186-after-shrink'"
        ))
        self.assertIsNone(latest_current_radar_run(self.db))

    def test_fund_discovery_ui_never_selects_candidates_from_latest_archived_run(self):
        source = (Path(__file__).parents[1] / "app" / "ui.py").read_text(encoding="utf-8")
        page = source[source.index("class FundDiscoveryPage"):source.index("class FundPage")]
        self.assertIn("latest_current_radar_run(self.db)", page)
        self.assertNotIn("SELECT task_id FROM radar_runs ORDER BY imported_at DESC", page)
        self.assertIn("force_refresh=force_refresh", source[source.index("class AuditPage"):source.index("class FundDiscoveryPage")])
        self.assertIn("当日可申购复核", page)


if __name__ == "__main__":
    unittest.main()
