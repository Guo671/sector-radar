from __future__ import annotations

import copy
import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.protocol import (
    END_MARKER,
    LEGACY_START_MARKER,
    START_MARKER,
    parse_result,
)
from app.universe import UNIVERSE_VERSION


class SchemaV29Tests(unittest.TestCase):
    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def test_fresh_database_contains_frozen_policy_and_structured_metric_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "fresh.sqlite3")
            with db.connect() as conn:
                for table in ("analysis_jobs", "radar_runs"):
                    columns = self._columns(conn, table)
                    self.assertIn("ranking_policy_version", columns)
                    self.assertIn("ranking_policy_json", columns)
                metric_columns = self._columns(conn, "sector_metrics")
                self.assertTrue({
                    "volatility_20d",
                    "consecutive_up_days",
                    "breadth_kind",
                    "breadth_pct",
                    "expected_data_cutoff",
                    "freshness_state",
                }.issubset(metric_columns))

    def test_v27_migration_adds_columns_to_existing_tables(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE analysis_jobs(id INTEGER PRIMARY KEY);
            CREATE TABLE radar_runs(task_id TEXT PRIMARY KEY);
            CREATE TABLE sector_metrics(id INTEGER PRIMARY KEY);
            CREATE TABLE candidates(id INTEGER PRIMARY KEY);
            CREATE TABLE radar_audit(
                id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL,
                universe_id TEXT NOT NULL
            );
            """
        )
        db = Database.__new__(Database)
        db._migrate_v27(conn)
        for table in ("analysis_jobs", "radar_runs"):
            columns = self._columns(conn, table)
            self.assertIn("ranking_policy_version", columns)
            self.assertIn("ranking_policy_json", columns)
        self.assertTrue({
            "volatility_20d",
            "consecutive_up_days",
            "breadth_kind",
            "breadth_pct",
            "expected_data_cutoff",
            "freshness_state",
        }.issubset(self._columns(conn, "sector_metrics")))

    def test_v28_advances_only_known_algorithm_version_and_preserves_user_tuning(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO settings VALUES('ranking_policy_version','four-dimension-v1');
            INSERT INTO settings VALUES('ranking_trend_weight','0.31');
            INSERT INTO settings VALUES('ranking_candidate_min_score','49');
            """
        )
        db = Database.__new__(Database)
        db._migrate_v28(conn)
        values = dict(conn.execute("SELECT key,value FROM settings"))
        self.assertEqual(values["ranking_policy_version"], "four-dimension-v2")
        self.assertEqual(values["ranking_trend_weight"], "0.31")
        self.assertEqual(values["ranking_candidate_min_score"], "49")

    def test_v29_adds_structured_flash_levels_and_preserves_user_tuning(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO settings VALUES('ranking_policy_version','four-dimension-v2');
            INSERT INTO settings VALUES('ranking_trend_weight','0.31');
            INSERT INTO settings VALUES('ranking_candidate_min_score','49');
            CREATE TABLE sector_evidence(
                id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, universe_id TEXT NOT NULL,
                crowding TEXT NOT NULL DEFAULT '', catalyst TEXT NOT NULL DEFAULT '',
                risk TEXT NOT NULL DEFAULT '', source_json TEXT NOT NULL DEFAULT '[]'
            );
            """
        )
        db = Database.__new__(Database)
        db._migrate_v29(conn)
        values = dict(conn.execute("SELECT key,value FROM settings"))
        self.assertEqual(values["ranking_policy_version"], "four-dimension-v3")
        self.assertEqual(values["ranking_trend_weight"], "0.31")
        self.assertEqual(values["ranking_candidate_min_score"], "49")
        self.assertTrue({"crowding_level", "catalyst_strength"}.issubset(
            self._columns(conn, "sector_evidence")
        ))

    def test_v35_centres_crowding_formula_without_overwriting_user_tuning(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO settings VALUES('ranking_policy_version','four-dimension-v3');
            INSERT INTO settings VALUES('ranking_crowding_penalty','0.31');
            INSERT INTO settings VALUES('ranking_candidate_min_score','49');
            """
        )
        db = Database.__new__(Database)
        db._migrate_v35(conn)
        values = dict(conn.execute("SELECT key,value FROM settings"))
        self.assertEqual(values["ranking_policy_version"], "four-dimension-v4")
        self.assertEqual(values["ranking_crowding_penalty"], "0.31")
        self.assertEqual(values["ranking_candidate_min_score"], "49")

    def test_v36_deduplicates_only_identical_exclusion_reason_clauses(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE investable_indices(
                id INTEGER PRIMARY KEY,
                exclusion_reason TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO investable_indices(id, exclusion_reason) VALUES(
                1,
                '用户自定义；已由CSI叶子行业目录替代。；已由CSI叶子行业目录替代；另一条'
            );
            INSERT INTO investable_indices(id, exclusion_reason) VALUES(
                2,
                '只有一条用户说明'
            );
            """
        )
        db = Database.__new__(Database)
        db._migrate_v36(conn)
        reasons = dict(conn.execute(
            "SELECT id, exclusion_reason FROM investable_indices ORDER BY id"
        ))
        values = dict(conn.execute("SELECT key,value FROM settings"))
        self.assertEqual(
            reasons[1],
            "用户自定义；已由CSI叶子行业目录替代。；另一条",
        )
        self.assertEqual(reasons[2], "只有一条用户说明")
        self.assertEqual(values["migration_v36_reason_rows_deduplicated"], "1")

    def test_v37_updates_only_the_complete_preview_price_triplet(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO settings VALUES('pro_input_price_cny_per_million','3.15');
            INSERT INTO settings VALUES('pro_cached_input_price_cny_per_million','0.03');
            INSERT INTO settings VALUES('pro_output_price_cny_per_million','6.30');
            INSERT INTO settings VALUES('ranking_trend_weight','0.31');
            """
        )
        db = Database.__new__(Database)
        db._migrate_v37(conn)
        values = dict(conn.execute("SELECT key,value FROM settings"))
        self.assertEqual(values["pro_input_price_cny_per_million"], "3.0")
        self.assertEqual(values["pro_cached_input_price_cny_per_million"], "0.025")
        self.assertEqual(values["pro_output_price_cny_per_million"], "6.0")
        self.assertEqual(values["migration_v37_official_pro_prices_updated"], "1")
        self.assertEqual(values["ranking_trend_weight"], "0.31")

        custom = sqlite3.connect(":memory:")
        self.addCleanup(custom.close)
        custom.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO settings VALUES('pro_input_price_cny_per_million','3.15');
            INSERT INTO settings VALUES('pro_cached_input_price_cny_per_million','0.031');
            INSERT INTO settings VALUES('pro_output_price_cny_per_million','6.30');
            """
        )
        db._migrate_v37(custom)
        custom_values = dict(custom.execute("SELECT key,value FROM settings"))
        self.assertEqual(custom_values["pro_input_price_cny_per_million"], "3.15")
        self.assertEqual(custom_values["pro_cached_input_price_cny_per_million"], "0.031")
        self.assertEqual(custom_values["pro_output_price_cny_per_million"], "6.30")
        self.assertEqual(custom_values["migration_v37_official_pro_prices_updated"], "0")


class ProtocolV3Tests(unittest.TestCase):
    def setUp(self):
        self.expected = [
            {
                "index_code": "LEAF-AI",
                "index_name": "中证人工智能产业指数",
                "category": "信息技术",
                "csi_index_code": "931071.CSI",
                "has_buyable_fund": 1,
            },
            {
                "index_code": "LEAF-CLOUD",
                "index_name": "中证云计算与大数据主题指数",
                "category": "信息技术",
                "csi_index_code": "930851.CSI",
                "has_buyable_fund": 1,
            },
        ]

    def payload(self) -> dict:
        sources = [
            {"id": "S1", "title": "来源1", "url": "https://a.example/1", "retrieved_at": "2026-08-11", "supports": "行情"},
            {"id": "S2", "title": "来源2", "url": "https://b.example/2", "retrieved_at": "2026-08-11", "supports": "成分"},
            {"id": "S3", "title": "来源3", "url": "https://c.example/3", "retrieved_at": "2026-08-11", "supports": "基金"},
            {"id": "S4", "title": "来源4", "url": "https://a.example/4", "retrieved_at": "2026-08-11", "supports": "催化"},
            {"id": "S5", "title": "来源5", "url": "https://b.example/5", "retrieved_at": "2026-08-11", "supports": "风险"},
        ]
        audit = []
        for row in self.expected:
            audit.append({
                "id": row["index_code"],
                "name": row["index_name"],
                "category": row["category"],
                "proxy_code": row["csi_index_code"],
                "proxy_name": row["index_name"],
                "returns_pct": {"d1": 1, "d3": 2, "d5": 3, "d10": 4, "d20": 5},
                "volume_signal": "温和放量",
                "breadth_signal": "真实成分股上涨比例58%",
                "position_20d": "62%分位",
                "crowding": "中",
                "catalyst": "产业订单改善",
                "research_tilt": "推荐买",
                "timing_status": "等待回调",
                "execution_status": "可执行",
                "trend_score": 76,
                "entry_score": 61,
                "crowding_risk_score": 52,
                "data_confidence": 90,
                "opportunity_score": 78,
                "recommendation_score": 74,
                "status": "ANALYZED",
                "source_ids": ["S1"],
                "note": "",
            })
        return {
            "protocol_version": "3.0",
            "task_kind": "night",
            "task_id": "2026-08-11-night-v3",
            "task_status": "COMPLETE",
            "universe_version": UNIVERSE_VERSION,
            "data_cutoff": "2026-08-11 15:00 +08:00",
            "market_regime": "轮动震荡",
            "universe_total": 2,
            "analyzed_total": 2,
            "coverage_by_category": [{"category": "信息技术", "expected": 2, "analyzed": 2}],
            "research_method": "统一使用本地五周期行情、真实扩散度与三方公开来源交叉核验。",
            "tool_calls_summary": [{"tool": "本地行情", "purpose": "结构化指标", "items": 2}],
            "audit": audit,
            "candidates": [{
                "rank": 1,
                "universe_id": "LEAF-AI",
                "index_code": "931071.CSI",
                "sector_name": "中证人工智能产业指数",
                "fund_code": "",
                "fund_name": "",
                "research_tilt": "推荐买",
                "timing_status": "等待回调",
                "execution_status": "基金待绑定",
                "trend_score": 76,
                "opportunity_score": 78,
                "recommendation_score": 74,
                "entry_score": 61,
                "crowding_risk_score": 52,
                "data_confidence": 90,
                "suggested_amount": 0,
                "allocation_pct": 0,
                "correlation_bucket": "AI产业链",
                "thesis": "盈利预期与订单同时改善",
                "catalyst": "产业订单改善",
                "crowding": "中",
                "risk": "短期位置偏高",
                "buy_conditions": "回调后量价企稳",
                "abandon_conditions": "跌破关键支撑",
            }],
            "avoid_list": [{
                "universe_id": "LEAF-CLOUD",
                "index_code": "930851.CSI",
                "sector_name": "中证云计算与大数据主题指数",
                "reason": "短期拥挤且缺少增量催化",
                "risk_level": "高",
            }],
            "fund_updates": [],
            "questions_for_intraday": ["当前涨跌幅"],
            "risk_flags": [],
            "sources": sources,
        }

    def wrap(self, payload: dict, marker: str = START_MARKER) -> str:
        return f"{marker}\n{json.dumps(payload, ensure_ascii=False)}\n{END_MARKER}"

    def parse(self, payload: dict, marker: str = START_MARKER):
        return parse_result(self.wrap(payload, marker), set(), 8000, self.expected)

    def test_valid_v3_result_is_writable(self):
        result = self.parse(self.payload())
        self.assertTrue(result.ok, result.errors)
        self.assertFalse(result.read_only)
        self.assertEqual(result.protocol_version, "3.0")

    def test_v3_accepts_official_h_series_csi_codes_for_candidates_and_avoid_list(self):
        expected = copy.deepcopy(self.expected)
        expected[0]["csi_index_code"] = "H30217.CSI"
        expected[1]["csi_index_code"] = "H30192.CSI"
        payload = self.payload()
        payload["candidates"][0]["index_code"] = "H30217.CSI"
        payload["avoid_list"][0]["index_code"] = "H30192.CSI"

        result = parse_result(self.wrap(payload), set(), 8000, expected)

        self.assertTrue(result.ok, result.errors)

    def test_v3_still_rejects_non_csi_letter_prefixes(self):
        expected = copy.deepcopy(self.expected)
        expected[0]["csi_index_code"] = "Z30217.CSI"
        payload = self.payload()
        payload["candidates"][0]["index_code"] = "Z30217.CSI"

        result = parse_result(self.wrap(payload), set(), 8000, expected)

        self.assertFalse(result.ok)
        self.assertTrue(any("缺少规范的中证.CSI代码" in error for error in result.errors), result.errors)

    def test_v3_candidate_fund_must_match_same_index_frozen_strict_c_whitelist(self):
        expected = copy.deepcopy(self.expected)
        expected[0]["strict_c_funds"] = [{
            "fund_code": "012345", "fund_name": "测试人工智能ETF联接C",
        }]
        payload = self.payload()
        payload["candidates"][0].update({
            "fund_code": "654321", "fund_name": "其他指数ETF联接C",
            "execution_status": "可执行",
        })
        result = parse_result(self.wrap(payload), set(), 8000, expected)
        self.assertFalse(result.ok)
        self.assertTrue(any("本轮冻结" in error and "白名单" in error for error in result.errors), result.errors)

    def test_v3_candidate_fund_name_must_be_the_frozen_canonical_name(self):
        expected = copy.deepcopy(self.expected)
        expected[0]["strict_c_funds"] = [{
            "fund_code": "012345", "fund_name": "测试人工智能ETF联接C",
        }]
        payload = self.payload()
        payload["candidates"][0].update({
            "fund_code": "012345", "fund_name": "相似但错误的简称",
            "execution_status": "可执行",
        })
        result = parse_result(self.wrap(payload), {"012345"}, 8000, expected)
        self.assertFalse(result.ok)
        self.assertTrue(any("完整名称应为" in error for error in result.errors), result.errors)

    def test_v3_accepts_exact_same_index_frozen_strict_c_fund(self):
        expected = copy.deepcopy(self.expected)
        expected[0]["strict_c_funds"] = [{
            "fund_code": "012345", "fund_name": "测试人工智能ETF联接C",
        }]
        payload = self.payload()
        payload["candidates"][0].update({
            "fund_code": "012345", "fund_name": "测试人工智能ETF联接C",
            "execution_status": "可执行",
        })
        result = parse_result(self.wrap(payload), {"012345"}, 8000, expected)
        self.assertTrue(result.ok, result.errors)

    def test_v3_rejects_invalid_enums_and_scores(self):
        cases = [
            ("research_tilt", "重点"),
            ("timing_status", "追涨"),
            ("execution_status", "大概可买"),
            ("trend_score", -1),
            ("opportunity_score", 101),
            ("recommendation_score", -1),
            ("entry_score", float("nan")),
            ("crowding_risk_score", True),
            ("data_confidence", "90"),
            ("score_adjustment", 1),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                payload = self.payload()
                payload["candidates"][0][field] = value
                result = self.parse(payload)
                self.assertFalse(result.ok)
                self.assertTrue(any(field in error for error in result.errors), result.errors)

    def test_v3_every_audit_row_requires_seven_level_timing_execution_and_all_scores(self):
        required_fields = (
            "research_tilt",
            "timing_status",
            "execution_status",
            "trend_score",
            "entry_score",
            "crowding_risk_score",
            "data_confidence",
            "opportunity_score",
            "recommendation_score",
        )
        for field in required_fields:
            with self.subTest(field=field):
                payload = self.payload()
                payload["audit"][0].pop(field)
                result = self.parse(payload)
                self.assertFalse(result.ok)
                self.assertTrue(any(field in error for error in result.errors), result.errors)

    def test_v3_rejects_score_tier_mismatch_for_audit_and_candidate(self):
        payload = self.payload()
        payload["audit"][0]["research_tilt"] = "千万别买"
        audit_result = self.parse(payload)
        self.assertFalse(audit_result.ok)
        self.assertTrue(any("应对应“推荐买”" in error for error in audit_result.errors), audit_result.errors)

        payload = self.payload()
        payload["candidates"][0]["research_tilt"] = "观察"
        candidate_result = self.parse(payload)
        self.assertFalse(candidate_result.ok)
        self.assertTrue(any("应对应“推荐买”" in error for error in candidate_result.errors), candidate_result.errors)

        enum_cases = (
            ("research_tilt", "模糊看好"),
            ("timing_status", "追涨"),
            ("execution_status", "大概可买"),
        )
        for field, value in enum_cases:
            with self.subTest(field=field, value=value):
                payload = self.payload()
                payload["audit"][0][field] = value
                result = self.parse(payload)
                self.assertFalse(result.ok)
                self.assertTrue(any(field in error for error in result.errors), result.errors)

    def test_v3_final_main_list_rejects_more_than_ten_candidates(self):
        payload = self.payload()
        payload["candidates"] = [copy.deepcopy(payload["candidates"][0]) for _ in range(11)]
        result = self.parse(payload)
        self.assertFalse(result.ok)
        self.assertTrue(any("最多10个候选" in error for error in result.errors), result.errors)

    def test_v3_avoid_list_requires_catalog_identity_reason_risk_and_uniqueness(self):
        mutations = []
        payload = self.payload(); payload["avoid_list"][0]["reason"] = ""; mutations.append(payload)
        payload = self.payload(); payload["avoid_list"][0]["risk_level"] = "极高"; mutations.append(payload)
        payload = self.payload(); payload["avoid_list"][0]["index_code"] = "930852.CSI"; mutations.append(payload)
        payload = self.payload(); payload["avoid_list"][0]["universe_id"] = "UNKNOWN"; mutations.append(payload)
        payload = self.payload(); payload["avoid_list"].append(copy.deepcopy(payload["avoid_list"][0])); mutations.append(payload)
        payload = self.payload(); payload["avoid_list"] = ["云计算"]; mutations.append(payload)
        for payload in mutations:
            with self.subTest(avoid_list=payload["avoid_list"]):
                self.assertFalse(self.parse(payload).ok)

    def test_v2_is_read_only_and_old_status_is_not_rewritten(self):
        payload = self.payload()
        payload["protocol_version"] = "2.0"
        candidate = payload["candidates"][0]
        for key in (
            "research_tilt", "timing_status", "execution_status",
            "trend_score", "opportunity_score", "recommendation_score", "entry_score",
            "crowding_risk_score", "data_confidence",
        ):
            candidate.pop(key)
        candidate["status"] = "旧版强势候选"
        payload["avoid_list"] = ["旧版回避方向"]
        result = self.parse(payload, LEGACY_START_MARKER)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.read_only)
        self.assertEqual(result.protocol_version, "2.0")
        self.assertEqual(result.payload["candidates"][0]["status"], "旧版强势候选")
        self.assertNotIn("research_tilt", result.payload["candidates"][0])
        self.assertTrue(any("只读" in warning for warning in result.warnings))

    def test_v2_marker_is_literal_and_survives_config_protocol_upgrade(self):
        import app.config as config_module
        import app.protocol as protocol_module

        original = config_module.PROTOCOL_VERSION
        try:
            config_module.PROTOCOL_VERSION = "SECTOR_RADAR_RESULT_V9"
            reloaded = importlib.reload(protocol_module)
            self.assertEqual(reloaded.LEGACY_START_MARKER, "<<<SECTOR_RADAR_RESULT_V2>>>")

            payload = self.payload()
            payload["protocol_version"] = "2.0"
            candidate = payload["candidates"][0]
            for key in (
                "research_tilt", "timing_status", "execution_status",
                "trend_score", "opportunity_score", "recommendation_score", "entry_score",
                "crowding_risk_score", "data_confidence",
            ):
                candidate.pop(key)
            candidate["status"] = "旧版只读结论"
            payload["avoid_list"] = ["旧版回避方向"]
            text = self.wrap(payload, reloaded.LEGACY_START_MARKER)
            result = reloaded.parse_result(text, set(), 8000, self.expected)
            self.assertTrue(result.ok, result.errors)
            self.assertTrue(result.read_only)
        finally:
            config_module.PROTOCOL_VERSION = original
            importlib.reload(protocol_module)

    def test_protocol_marker_and_payload_version_must_match(self):
        v3_result = self.parse(self.payload(), LEGACY_START_MARKER)
        self.assertFalse(v3_result.ok)
        self.assertTrue(any("V3结果必须使用V3结果标记" in error for error in v3_result.errors))

        payload = self.payload()
        payload["protocol_version"] = "2.0"
        candidate = payload["candidates"][0]
        for key in (
            "research_tilt", "timing_status", "execution_status",
            "trend_score", "opportunity_score", "recommendation_score", "entry_score",
            "crowding_risk_score", "data_confidence",
        ):
            candidate.pop(key)
        candidate["status"] = "旧版只读结论"
        payload["avoid_list"] = ["旧版回避方向"]
        v2_result = self.parse(payload, START_MARKER)
        self.assertFalse(v2_result.ok)
        self.assertTrue(any("历史V2结果必须使用固定V2标记" in error for error in v2_result.errors))


if __name__ == "__main__":
    unittest.main()
