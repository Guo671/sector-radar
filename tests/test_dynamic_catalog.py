import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from app.config import local_now
from app.catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from app.database import Database
from app.detailed_research import DetailedResearchPromptService
from app.decision_drafts import DecisionDraftError, DecisionDraftService
from app.orchestrator import ResearchOrchestrator
from app.platform_catalog import PlatformCatalogService
from app.protocol import build_task_package
from app.services import latest_current_radar_run, save_action_advice
from app.trading_calendar import ChinaTradingCalendar


class DynamicPlatformCatalogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name) / "catalog.sqlite3")
        self.catalog = PlatformCatalogService(self.db)
        self.baseline = len(self.catalog.radar_universe())

    def tearDown(self):
        self.folder.cleanup()

    def add_complete_index(self, code="930001"):
        index_id = self.catalog.add_candidate(
            industry_group="测试行业",
            platform_code=f"{code}.CSI",
            platform_name="测试行业主题",
            official_code=code,
            official_name="测试行业正式指数",
            official_source_url=f"https://www.csindex.com.cn/#/indices/family/detail?indexCode={code}",
            evidence_kind="中证官方事实表",
            platform_source_url=f"https://www.csindex.com.cn/#/indices/family/detail?indexCode={code}",
            security_id=f"2.{code}",
            proxy_code=code,
            proxy_name="测试行业正式指数",
            mapping_confidence=0.98,
        )
        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO investable_funds(
                   investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                   public_buyable_status,ant_buyable_status,quality_rank,source_url,source_note,
                   verified_at,active,passive_verified,platform_evidence_url,platform_checked_at,
                   radar_eligible,relation_kind,verification_algorithm
                ) VALUES(?,?,?,?,?,'公开渠道可申购','未核对平台实时状态',1,?,?,?,1,1,?,?,0,'同一指数',?)""",
            (
                index_id, "000001", "测试指数ETF联接C", "被动ETF联接", "C",
                "https://example.com/fund", "测试资料", now,
                "https://example.com/fund", now, STRICT_FIELD_SYNC_ALGORITHM_VERSION,
            ),
        )
        self.catalog.refresh_admission(index_id)
        return index_id

    def test_new_index_without_strict_c_does_not_enter_dynamic_scan(self):
        index_id = self.catalog.add_candidate(
            industry_group="新行业", platform_code="930001.CSI", platform_name="新行业主题",
            official_code="930001", official_name="新行业正式指数",
            official_source_url="https://www.csindex.com.cn/#/indices/family/detail?indexCode=930001",
            evidence_kind="中证官方事实表", security_id="2.930001",
            proxy_code="930001", proxy_name="新行业正式指数", mapping_confidence=0.98,
        )
        row = self.catalog.index(index_id)
        self.assertEqual(row["universe_id"], "C930001")
        self.assertEqual(row["ant_index_code"], "930001.CSI")
        self.assertTrue(self.catalog.check(index_id).admitted)
        self.assertEqual(len(self.catalog.radar_universe()), self.baseline)
        refreshed = self.catalog.index(index_id)
        self.assertEqual(refreshed["radar_enabled"], 0)
        self.assertIn("没有严格同指数", refreshed["exclusion_reason"])
        with self.assertRaisesRegex(ValueError, "只允许中证代码加.CSI"):
            self.catalog.add_candidate(
                industry_group="错误后缀", platform_code="930002.CNS", platform_name="错误指数",
                official_code="930002", official_name="错误指数",
                official_source_url="https://www.csindex.com.cn/", security_id="2.930002",
                mapping_confidence=0.98,
            )

    def test_strict_c_adds_one_dynamic_scan_item_and_job_snapshot(self):
        index_id = self.add_complete_index()
        check = self.catalog.check(index_id)
        self.assertTrue(check.admitted, check.reasons)
        item = next(row for row in self.catalog.radar_universe() if row["index_code"] == "C930001")
        self.assertEqual(item["csi_index_code"], "930001.CSI")
        self.assertEqual(item["index_name"], "测试行业正式指数")
        self.assertEqual(item["has_buyable_fund"], 1)
        self.assertEqual(item["fund_code"], "000001")
        task_id = ResearchOrchestrator(self.db).create_job("无API模拟")
        job = self.db.row("SELECT total_items FROM analysis_jobs WHERE task_id=?", (task_id,))
        self.assertEqual(job["total_items"], self.baseline + 1)

    def test_detailed_prompts_are_self_contained_and_target_specific(self):
        index_id = self.add_complete_index()
        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO investable_funds(
                   investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                   public_buyable_status,verified_at,active,passive_verified,
                   platform_checked_at,radar_eligible,relation_kind)
               VALUES(?,?,?,?,?,'公开渠道可申购',?,1,0,?,0,'同一指数')""",
            (index_id, "000099", "不合格增强A", "指数增强", "A", now, now),
        )
        service = DetailedResearchPromptService(self.db)
        deepseek = service.build(index_id, "deepseek")
        chatgpt = service.build(index_id, "chatgpt")
        for prompt in (deepseek, chatgpt):
            self.assertIn("930001.CSI", prompt)
            self.assertIn("测试指数ETF联接C", prompt)
            self.assertNotIn("不合格增强A", prompt)
            self.assertIn("本地严格C类基金白名单", prompt)
            self.assertIn("指数增强、主动基金", prompt)
            self.assertIn("不得把平台实时状态说成已经核验", prompt)
            self.assertIn("未来1—4周", prompt)
        self.assertIn("DeepSeek 网页版", deepseek)
        self.assertIn("ChatGPT 网页版", chatgpt)

    def test_no_strict_c_is_excluded_from_scan_prompt_and_current_catalog(self):
        self.add_complete_index("930010")
        index_id = self.catalog.add_candidate(
            industry_group="增强测试", platform_code="930002.CSI", platform_name="增强测试主题",
            official_code="930002", official_name="增强测试正式指数",
            official_source_url="https://www.csindex.com.cn/#/indices/family/detail?indexCode=930002",
            evidence_kind="中证官方事实表", security_id="2.930002",
            proxy_code="930002", proxy_name="增强测试正式指数", mapping_confidence=0.95,
        )
        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO investable_funds(
                   investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                   public_buyable_status,verified_at,active,passive_verified,platform_checked_at,radar_eligible
               ) VALUES(?,?,?,?,?,'公开渠道可申购',?,1,0,?,0)""",
            (index_id, "000002", "测试指数增强C", "指数增强", "C", now, now),
        )
        self.catalog.refresh_admission(index_id)
        self.assertTrue(self.catalog.check(index_id).admitted)
        self.assertNotIn("C930002", {row["index_code"] for row in self.catalog.radar_universe()})
        self.assertEqual(self.catalog.funds(index_id)[0]["radar_eligible"], 0)
        with self.assertRaisesRegex(ValueError, "历史审计|严格条件"):
            DetailedResearchPromptService(self.db).build(index_id, "chatgpt")

        task_id = ResearchOrchestrator(self.db).create_job("无API模拟")
        frozen_ids = self.db.row(
            "SELECT universe_ids_json FROM analysis_jobs WHERE task_id=?", (task_id,),
        )["universe_ids_json"]
        self.assertNotIn("C930002", frozen_ids)
        task_dir = Path(self.folder.name) / "tasks"
        task_dir.mkdir()
        with patch("app.protocol.TASK_DIR", task_dir):
            prompt, _path = build_task_package(self.db, "night")
        self.assertNotIn("930002.CSI", prompt)
        self.assertNotIn("增强测试正式指数", prompt)

        self.db.execute(
            """INSERT INTO csi_fund_sync_status(
                   investable_index_id,state,exact_c_count,attempted_at,last_success_at,last_error)
               VALUES(?,'已确认无C',0,?,?,?)
               ON CONFLICT(investable_index_id) DO UPDATE SET state='已确认无C',
                   exact_c_count=0,attempted_at=excluded.attempted_at,
                   last_success_at=excluded.last_success_at,last_error=excluded.last_error""",
            (index_id, now, now, "完整公开核验无严格C"),
        )
        self.assertEqual(self.catalog.retire_confirmed_no_c(index_id), 1)
        self.assertNotIn(index_id, {row["id"] for row in self.catalog.list_indices()})
        history = next(
            row for row in self.catalog.list_indices(include_inactive=True)
            if int(row["id"]) == index_id
        )
        self.assertEqual(history["active"], 0)
        self.assertEqual(history["collection_state"], "已确认无C")

    def test_v24_never_archives_a_v3_no_c_false_negative(self):
        index_id = self.catalog.add_candidate(
            industry_group="芯片设计",
            platform_code="930003.CSI",
            platform_name="芯片设计",
            official_code="930003",
            official_name="科创芯片设计",
            official_source_url="https://www.csindex.com.cn/#/indices/family/detail?indexCode=930003",
            evidence_kind="中证官方事实表",
            security_id="2.930003",
            proxy_code="930003",
            proxy_name="科创芯片设计",
            mapping_confidence=0.98,
        )
        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO csi_fund_sync_status(
                   investable_index_id,state,exact_c_count,attempted_at,last_success_at,last_error)
               VALUES(?,'已确认无C',0,?,?,?)""",
            (index_id, now, now, "旧算法短名误判"),
        )
        self.db.save_settings({
            "schema_version": "23",
            "csi_fund_sync_algorithm_version": "exact-passive-c-v3-root-expansion-ttl",
        })
        migrated = Database(self.db.path)
        row = migrated.row("SELECT active,radar_enabled FROM investable_indices WHERE id=?", (index_id,))
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["radar_enabled"], 0)

        # The same durable conclusion becomes archivable only after v4 has audited
        # it with CSI's official product-family evidence.
        migrated.save_settings({
            "schema_version": "23",
            "csi_fund_sync_algorithm_version": "exact-passive-c-v4-official-family",
        })
        migrated_again = Database(self.db.path)
        self.assertEqual(
            migrated_again.row("SELECT active FROM investable_indices WHERE id=?", (index_id,))["active"],
            0,
        )

    def test_parent_and_child_cannot_both_enter_scan(self):
        parent_id = self.add_complete_index("930020")
        child_id = self.add_complete_index("930021")
        self.db.execute(
            "UPDATE investable_indices SET parent_official_code='930020' WHERE id=?",
            (child_id,),
        )
        self.catalog.refresh_admission(child_id)
        live = {row["index_code"] for row in self.catalog.radar_universe()}
        self.assertIn("C930021", live)
        self.assertNotIn("C930020", live)
        self.assertIn("更细叶子指数", self.catalog.index(parent_id)["admission_status"])

        self.db.execute(
            "UPDATE investable_indices SET active=0,catalog_role='历史记录',radar_enabled=0 WHERE id=?",
            (child_id,),
        )
        self.catalog.refresh_admission(parent_id)
        self.assertIn(
            "C930020", {row["index_code"] for row in self.catalog.radar_universe()},
        )

    def test_disabled_quote_mapping_cannot_enter_scan(self):
        index_id = self.add_complete_index("930030")
        uid = self.catalog.index(index_id)["universe_id"]
        self.db.execute("UPDATE proxy_mappings SET active=0 WHERE universe_id=?", (uid,))
        self.catalog.refresh_admission(index_id)
        self.assertNotIn(uid, {row["index_code"] for row in self.catalog.radar_universe()})
        self.assertIn("行情映射已停用", self.catalog.index(index_id)["exclusion_reason"])

    def test_fund_whitelist_change_invalidates_run_action_and_draft(self):
        index_id = self.add_complete_index("930040")
        self.db.save_settings({
            "csi_fund_sync_algorithm_version": STRICT_FIELD_SYNC_ALGORITHM_VERSION,
        })
        orchestrator = ResearchOrchestrator(self.db)
        task_id = orchestrator.create_job("完整扫描")
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        total = int(job["total_items"])
        now = local_now().isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE analysis_jobs SET status='已完成',stage='完成',completed_items=? WHERE task_id=?",
            (total, task_id),
        )
        self.db.execute(
            """INSERT INTO radar_runs(
                   task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                   ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id,
                ChinaTradingCalendar.from_db(self.db).latest_completed_trading_day(local_now()).isoformat(),
                "测试", total, total, job["ranking_policy_version"], job["ranking_policy_json"],
                "测试完整研究", json.dumps({"task_id": task_id, "protocol_version":"3.0"}), now,
            ),
        )
        self.assertIsNotNone(latest_current_radar_run(self.db))
        action = {
            "candidates": [{
                "rank": 1, "universe_id": "C930040", "index_code": "930040.CSI",
                "sector_name": "测试行业正式指数", "fund_code": "000001",
                "fund_name": "测试指数ETF联接C", "status": "推荐买", "research_tilt": "推荐买",
                "timing_status": "可以买", "execution_status": "可执行", "score": 80,
                "allocation_pct": 10, "target_allocation_pct": 10, "suggested_amount": 100,
                "trend_score": 80, "opportunity_score": 75, "recommendation_score": 70,
                "entry_score": 70, "crowding_risk_score": 25, "data_confidence": 90,
                "correlation_bucket": "930040", "thesis": "测试",
            }],
            "holding_actions": [], "action_summary": "测试", "no_buy_reason": "",
        }
        save_action_advice(self.db, task_id, action)
        candidate_id = self.db.row(
            "SELECT id FROM candidates WHERE task_id=?", (task_id,),
        )["id"]

        self.db.execute(
            """UPDATE investable_funds SET active=0,radar_eligible=0
                 WHERE investable_index_id=? AND fund_code='000001'""",
            (index_id,),
        )
        self.catalog.refresh_admission(index_id)
        self.assertIsNone(latest_current_radar_run(self.db))
        with self.assertRaisesRegex(ValueError, "目录已变化|基金映射"):
            save_action_advice(self.db, task_id, action)
        with self.assertRaises(DecisionDraftError):
            DecisionDraftService(self.db).add_candidate(candidate_id)


if __name__ == "__main__":
    unittest.main()
