from __future__ import annotations

import tempfile
import unittest
import json
from datetime import date, datetime
from pathlib import Path

from app.config import LOCAL_TZ
from app.catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from app.database import Database
from app.recommendation_performance import (
    capture_recommendation_observations,
    recover_historical_v3_recommendation_observations,
    recommendation_performance_summary,
    refresh_recommendation_outcomes,
)
from app.trading_calendar import ChinaTradingCalendar
from app.universe import UNIVERSE_VERSION


class RecommendationPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "performance.sqlite3")
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO funds(
                       fund_code,fund_name,index_code,index_name,share_class,
                       correlation_bucket,purchase_fee_pct,status,notes,updated_at,
                       name_status,profile_source_url,profile_checked_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "000001", "测试被动联接C", "930001.CSI", "测试科技", "C",
                    "测试科技", 0.0, "可买", "", "2026-08-03T10:00:00+08:00",
                    "已自动识别", "https://example.test/fund/000001", "2026-08-03",
                ),
            )
            conn.execute(
                """INSERT INTO radar_runs(
                       task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                       ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    "run-forward", "2026-07-31", "轮动", 1, 1,
                    "four-dimension-v3", '{"version":"four-dimension-v3"}', "测试", "{}",
                    "2026-08-03T10:00:00+08:00",
                ),
            )
            conn.execute(
                """INSERT INTO radar_audit(
                       task_id,universe_id,sector_name,category,proxy_code,proxy_name,
                       d1,d3,d5,d10,d20,volume_signal,breadth_signal,position_20d,crowding,
                       local_rank,research_tilt,timing_status,execution_status,
                       recommendation_score,score_components_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "run-forward", "U-TECH", "测试科技", "信息技术", "930001", "测试科技",
                    1, 2, 3, 4, 5, "温和放量", "成分股上涨6/10", "价格区间位置60%", "中",
                    1, "推荐买", "可以买", "基金可执行", 72.0,
                    '{"recommendation_score":72}',
                ),
            )
            conn.execute(
                """INSERT INTO sector_metrics(
                       task_id,universe_id,proxy_code,proxy_name,d1,d3,d5,d10,d20,
                       volume_signal,breadth_signal,position_20d,data_source,
                       expected_data_cutoff,freshness_state
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "run-forward", "U-TECH", "930001", "测试科技", 1, 2, 3, 4, 5,
                    "温和放量", "成分股上涨6/10", "价格区间位置60%", "csi_official",
                    "2026-07-31", "fresh",
                ),
            )
            conn.execute(
                """INSERT INTO candidates(
                       task_id,rank_no,universe_id,index_code,sector_name,fund_code,fund_name,
                       status,score,research_tilt,timing_status,execution_status,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "run-forward", 1, "U-TECH", "930001.CSI", "测试科技", "000001",
                    "测试被动联接C", "推荐买", 72.0, "推荐买", "可以买", "基金可执行",
                    "2026-08-03T10:00:00+08:00",
                ),
            )
            conn.execute(
                """INSERT INTO universe(
                       index_code,index_name,category,has_buyable_fund,fund_code,status,notes
                   ) VALUES(?,?,?,1,?,'已准入','测试')""",
                ("U-TECH", "测试科技", "信息技术", "000001"),
            )
            index_id = conn.execute(
                """INSERT INTO investable_indices(
                       universe_id,official_index_code,official_index_name,active,radar_enabled
                   ) VALUES(?,?,?,1,1)""",
                ("U-TECH", "930001", "测试科技"),
            ).lastrowid
            conn.execute(
                """INSERT INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,quality_rank,source_url,verified_at,active,
                       passive_verified,relation_kind,verification_algorithm
                   ) VALUES(?,?,?,?,?,?,?,?,?,1,1,'同一指数',?)""",
                (
                    index_id, "000001", "测试被动联接C", "被动ETF联接", "C",
                    "公开渠道可申购", 2, "https://example.test/catalog/000001",
                    "2026-08-03", STRICT_FIELD_SYNC_ALGORITHM_VERSION,
                ),
            )

    def _capture(self):
        with self.db.connect() as conn:
            return capture_recommendation_observations(
                conn,
                "run-forward",
                datetime(2026, 8, 3, 10, 0, tzinfo=LOCAL_TZ),
                ChinaTradingCalendar(),
            )

    def _save_price_evidence(self):
        with self.db.connect() as conn:
            for trade_date, close in (("2026-08-03", 100.0), ("2026-08-04", 102.0)):
                conn.execute(
                    """INSERT INTO market_bars(
                           provider,security_id,trade_date,open,close,high,low,volume,amount,fetched_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "csi_official", "CSI.930001", trade_date, close, close, close, close,
                        1, 1, "2026-08-04T18:00:00+08:00",
                    ),
                )
            for nav_date, nav in (("2026-08-03", 1.0), ("2026-08-04", 1.02)):
                conn.execute(
                    """INSERT INTO fund_nav_history(
                           fund_code,nav_date,nav,source,source_url,fetched_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        "000001", nav_date, nav, "test-provider",
                        f"https://example.test/nav/{nav_date}", "2026-08-04T18:00:00+08:00",
                    ),
                )

    def test_capture_is_forward_only_idempotent_and_immutable(self):
        self.assertEqual(self._capture(), 1)
        self.assertEqual(self._capture(), 0)
        observation = self.db.row("SELECT * FROM recommendation_observations")
        self.assertEqual(observation["pricing_date"], "2026-08-03")
        self.assertEqual(observation["data_cutoff"], "2026-07-31")
        self.assertEqual(observation["index_security_id"], "CSI.930001")
        self.assertEqual(
            self.db.row("SELECT COUNT(*) AS n FROM recommendation_outcomes")["n"], 5,
        )
        self.db.execute(
            "UPDATE candidates SET fund_code='999999',score=1,research_tilt='千万别买'"
        )
        frozen = self.db.row("SELECT * FROM recommendation_observations")
        self.assertEqual((frozen["fund_code"], frozen["recommendation_score"], frozen["research_tilt"]),
                         ("000001", 72.0, "推荐买"))

    def test_unknown_fee_is_not_zero_then_verified_fee_matures(self):
        self._capture()
        self._save_price_evidence()
        counts = refresh_recommendation_outcomes(self.db, as_of=date(2026, 8, 4))
        self.assertEqual(counts["index_mature"], 1)
        self.assertNotIn("fund_mature", counts)
        outcome = self.db.row(
            "SELECT * FROM recommendation_outcomes WHERE horizon_sessions=1"
        )
        self.assertAlmostEqual(outcome["index_return_pct"], 2.0)
        self.assertAlmostEqual(outcome["fund_gross_return_pct"], 2.0)
        self.assertIsNone(outcome["fund_net_return_pct"])
        self.assertIn("赎回费阶梯", outcome["reason"])
        before = recommendation_performance_summary(self.db, horizon_sessions=1)
        self.assertEqual(before["fund_mature"], 0)
        self.assertIsNone(before["average_fund_net_return_pct"])

        self.db.execute(
            """INSERT INTO fee_tiers(
                   fund_code,min_days,max_days,redeem_fee_pct,source_url,checked_at
               ) VALUES(?,?,?,?,?,?)""",
            ("000001", 0, None, 1.5, "https://example.test/fee", "2026-08-03"),
        )
        counts = refresh_recommendation_outcomes(self.db, as_of=date(2026, 8, 4))
        self.assertEqual(counts["fund_mature"], 1)
        outcome = self.db.row(
            "SELECT * FROM recommendation_outcomes WHERE horizon_sessions=1"
        )
        self.assertAlmostEqual(outcome["fund_net_return_pct"], 0.47, places=6)
        self.assertEqual(outcome["fund_direction_hit"], 1)
        summary = recommendation_performance_summary(self.db, horizon_sessions=1)
        self.assertEqual(summary["fund_mature"], 1)
        self.assertAlmostEqual(summary["average_fund_net_return_pct"], 0.47, places=6)
        self.assertEqual(summary["fund_positive_rate_pct"], 100.0)
        self.assertEqual(summary["direction_hit_rate_pct"], 100.0)
        self.assertEqual(summary["by_score_band"][2]["group"], "70–<80分")
        self.assertEqual(summary["by_score_band"][2]["fund_mature"], 1)

    def test_index_only_result_is_complete_not_fund_pending(self):
        self._capture()
        self.db.execute(
            """UPDATE recommendation_observations
                  SET fund_code='',fund_name='',fund_selection_kind=''
                WHERE task_id='run-forward'"""
        )
        self._save_price_evidence()

        counts = refresh_recommendation_outcomes(self.db, as_of=date(2026, 8, 4))
        self.assertEqual(counts["index_mature"], 1)
        self.assertEqual(counts["index_only_mature"], 1)
        self.assertNotIn("fund_mature", counts)

        outcome = self.db.row(
            "SELECT * FROM recommendation_outcomes WHERE horizon_sessions=1"
        )
        self.assertEqual(outcome["status"], "指数结果已核算·无执行基金")
        self.assertIn("没有执行基金推荐", outcome["reason"])
        self.assertIsNone(outcome["fund_net_return_pct"])

        summary = recommendation_performance_summary(self.db, horizon_sessions=1)
        self.assertEqual(summary["index_mature"], 1)
        self.assertEqual(summary["index_only_mature"], 1)
        self.assertEqual(summary["fund_mature"], 0)
        self.assertIn("不冒充基金待核算", summary["definition"])

    def test_after_cutoff_rolls_to_next_trading_day(self):
        with self.db.connect() as conn:
            inserted = capture_recommendation_observations(
                conn,
                "run-forward",
                datetime(2026, 8, 3, 15, 1, tzinfo=LOCAL_TZ),
                ChinaTradingCalendar(),
            )
        # Same task/universe remains immutable, so verify the rule on a fresh task.
        self.assertEqual(inserted, 1)
        self.assertEqual(
            self.db.row("SELECT pricing_date FROM recommendation_observations")["pricing_date"],
            "2026-08-04",
        )

    def test_non_candidate_row_freezes_strict_c_benchmark_without_execution_side_effects(self):
        with self.db.connect() as conn:
            index_id = conn.execute(
                "SELECT id FROM investable_indices WHERE universe_id='U-TECH'"
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,quality_rank,source_url,verified_at,active,
                       passive_verified,relation_kind,verification_algorithm
                   ) VALUES(?,?,?,?,?,?,?,?,?,1,1,'同一指数',?)""",
                (
                    index_id, "000002", "测试科技基准C", "被动ETF联接", "C",
                    "公开渠道可申购", 1, "https://example.test/catalog", "2026-08-03",
                    STRICT_FIELD_SYNC_ALGORITHM_VERSION,
                ),
            )
            conn.execute(
                """INSERT INTO funds(
                       fund_code,fund_name,index_code,index_name,share_class,
                       correlation_bucket,purchase_fee_pct,status,notes,updated_at,
                       name_status,profile_source_url,profile_checked_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "000002", "测试科技基准C", "930001.CSI", "测试科技", "C",
                    "测试科技", 0, "可买", "", "2026-08-03T10:00:00+08:00",
                    "已自动识别", "https://example.test/fund/000002", "2026-08-03",
                ),
            )
            conn.execute("DELETE FROM candidates WHERE task_id='run-forward'")
        execution_tables = ("decision_drafts", "trades", "holdings", "active_cash_ledger")
        before = {
            table: self.db.row(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in execution_tables
        }
        self.assertEqual(self._capture(), 1)
        observation = self.db.row("SELECT * FROM recommendation_observations")
        self.assertEqual(observation["fund_code"], "000002")
        self.assertEqual(observation["fund_selection_kind"], "同指数严格C基准")
        after = {
            table: self.db.row(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in execution_tables
        }
        self.assertEqual(after, before)

    def test_candidate_snapshot_requires_exact_same_index_strict_c_evidence(self):
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE candidates SET fund_code='999999',fund_name='错误跨指数基金C' "
                "WHERE task_id='run-forward'"
            )
        with self.assertRaisesRegex(ValueError, "不在同指数严格C类白名单"):
            self._capture()
        self.assertEqual(
            self.db.row("SELECT COUNT(*) AS n FROM recommendation_observations")["n"], 0,
        )

    def test_candidate_snapshot_freezes_evidence_for_the_selected_not_top_ranked_fund(self):
        with self.db.connect() as conn:
            index_id = conn.execute(
                "SELECT id FROM investable_indices WHERE universe_id='U-TECH'"
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,quality_rank,source_url,verified_at,active,
                       passive_verified,relation_kind,verification_algorithm
                   ) VALUES(?,?,?,?,?,?,?,?,?,1,1,'同一指数',?)""",
                (
                    index_id, "000003", "测试第二候选C", "被动ETF联接", "C",
                    "公开渠道可申购", 9, "https://example.test/catalog/000003",
                    "2026-08-03", STRICT_FIELD_SYNC_ALGORITHM_VERSION,
                ),
            )
            conn.execute(
                "UPDATE candidates SET fund_code='000003',fund_name='测试第二候选C' "
                "WHERE task_id='run-forward'"
            )
        self.assertEqual(self._capture(), 1)
        frozen = json.loads(self.db.row(
            "SELECT source_snapshot_json FROM recommendation_observations"
        )["source_snapshot_json"])
        self.assertEqual(frozen["observed_fund_code"], "000003")
        self.assertEqual(
            frozen["fund_catalog_source_url"], "https://example.test/catalog/000003",
        )


class HistoricalV3RecommendationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "historical.sqlite3")

    @staticmethod
    def _audit_item(universe_id: str, name: str, code: str, rank: int, score: float) -> dict:
        if score >= 80:
            tilt = "非常推荐买"
        elif score >= 70:
            tilt = "推荐买"
        elif score >= 60:
            tilt = "有点推荐买"
        elif score >= 45:
            tilt = "观察"
        elif score >= 35:
            tilt = "不太推荐买"
        elif score >= 20:
            tilt = "不推荐买"
        else:
            tilt = "千万别买"
        return {
            "id": universe_id,
            "name": name,
            "category": "信息技术",
            "proxy_code": code,
            "proxy_name": name,
            "returns_pct": {"d1": 1, "d3": 2, "d5": 3, "d10": 4, "d20": 5},
            "volume_signal": "量能正常",
            "breadth_signal": "真实成分股上涨6/10",
            "position_20d": "价格区间位置55%",
            "crowding": "中",
            "catalyst": "订单改善",
            "local_rank": rank,
            "research_tilt": tilt,
            "timing_status": "可以买" if score >= 60 else "等待回调",
            "execution_status": "可执行",
            "trend_score": score,
            "entry_score": score,
            "crowding_risk_score": 40,
            "data_confidence": 90,
            "opportunity_score": score,
            "recommendation_score": score,
            "score_components": {
                "recommendation_score": score,
                "formula_version": "four-dimension-v3",
            },
            "status": "ANALYZED",
            "source_ids": [f"{universe_id}-S1"],
            "note": "",
        }

    def _seed_complete_v3(
        self,
        *,
        task_id: str = "historical-v3",
        protocol_version: str = "3.0",
        completed_items: int = 2,
        candidate_fund_checked_at: str = "2026-08-03T10:00:00+08:00",
    ) -> None:
        policy = {"version": "four-dimension-v3", "trend_weight": 0.25}
        audit = [
            self._audit_item("C930001", "测试人工智能", "930001", 1, 74.0),
            self._audit_item("C930002", "测试云计算", "930002", 2, 52.0),
        ]
        candidate = {
            "rank": 1,
            "universe_id": "C930001",
            "index_code": "930001.CSI",
            "sector_name": "测试人工智能",
            "fund_code": "000001",
            "fund_name": "测试人工智能ETF联接C",
            "research_tilt": "推荐买",
            "timing_status": "可以买",
            "execution_status": "可执行",
            "recommendation_score": 74.0,
            "suggested_amount": 500,
        }
        payload = {
            "protocol_version": protocol_version,
            "task_kind": "night",
            "task_id": task_id,
            "task_status": "COMPLETE",
            "universe_version": UNIVERSE_VERSION,
            "data_cutoff": "2026-08-03",
            "market_regime": "轮动震荡",
            "universe_total": 2,
            "analyzed_total": 2,
            "ranking_policy_version": "four-dimension-v3",
            "ranking_policy": policy,
            "audit": audit,
            "candidates": [candidate],
        }
        canonical_policy = json.dumps(
            policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        frozen_ids = ["C930001", "C930002"]
        with self.db.connect() as conn:
            for universe_id, name in (("C930001", "测试人工智能"), ("C930002", "测试云计算")):
                conn.execute(
                    """INSERT INTO universe(
                           index_code,index_name,category,has_buyable_fund,fund_code,status,notes
                       ) VALUES(?,?,?,1,'000001','已准入','测试')""",
                    (universe_id, name, "信息技术"),
                )
            index_id = conn.execute(
                """INSERT INTO investable_indices(
                       universe_id,official_index_code,official_index_name,active,
                       official_source_url,verified_at,platform_checked_at
                   ) VALUES(?,?,?,1,?,?,?)""",
                (
                    "C930001", "930001", "测试人工智能",
                    "https://example.test/index/930001", "2026-08-02", "2026-08-02",
                ),
            ).lastrowid
            conn.execute(
                """INSERT INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,quality_rank,source_url,verified_at,active,
                       passive_verified,relation_kind,verification_algorithm,platform_checked_at
                   ) VALUES(?,?,?,?,?,'公开渠道可申购',1,?,?,1,1,'同一指数',?,?)""",
                (
                    index_id, "000001", "测试人工智能ETF联接C", "被动ETF联接", "C",
                    "https://example.test/fund/000001", candidate_fund_checked_at,
                    STRICT_FIELD_SYNC_ALGORITHM_VERSION, candidate_fund_checked_at,
                ),
            )
            conn.execute(
                """INSERT INTO radar_runs(
                       task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                       ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, "2026-08-03", "轮动震荡", 2, 2,
                    "four-dimension-v3", canonical_policy, "测试",
                    json.dumps(payload, ensure_ascii=False), "2026-08-03T15:01:00+08:00",
                ),
            )
            conn.execute(
                """INSERT INTO analysis_jobs(
                       task_id,mode,stage,status,total_items,completed_items,
                       universe_version,research_date,universe_ids_json,universe_hash,
                       ranking_policy_version,ranking_policy_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, "完整扫描", "完成", "已完成", 2, completed_items,
                    UNIVERSE_VERSION, "2026-08-03", json.dumps(frozen_ids), "a" * 64,
                    "four-dimension-v3", canonical_policy,
                    "2026-08-03T14:00:00+08:00", "2026-08-03T15:01:00+08:00",
                ),
            )
            for universe_id, name, code in (
                ("C930001", "测试人工智能", "930001"),
                ("C930002", "测试云计算", "930002"),
            ):
                conn.execute(
                    """INSERT INTO sector_metrics(
                           task_id,universe_id,proxy_code,proxy_name,d1,d3,d5,d10,d20,
                           volume_signal,breadth_signal,position_20d,data_source,is_simulated,
                           data_cutoff,expected_data_cutoff,freshness_state
                       ) VALUES(?,?,?,?,1,2,3,4,5,'量能正常','真实成分股上涨6/10',
                                '价格区间位置55%','csi_official',0,'2026-08-03','2026-08-03','fresh')""",
                    (task_id, universe_id, code, name),
                )

    def test_recovers_raw_v3_idempotently_without_execution_side_effects(self):
        self._seed_complete_v3()
        execution_tables = (
            "decision_drafts", "trades", "holdings", "cost_lots",
            "active_cash_ledger", "ledger_postings",
        )
        before = {
            table: self.db.row(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in execution_tables
        }
        result = recover_historical_v3_recommendation_observations(self.db)
        self.assertEqual((result["tasks_eligible"], result["tasks_restored"]), (1, 1))
        self.assertEqual((result["observations_inserted"], result["outcomes_inserted"]), (2, 10))
        self.assertEqual(result["candidate_funds_kept"], 1)
        observations = self.db.rows(
            "SELECT * FROM recommendation_observations ORDER BY universe_id"
        )
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0]["fund_code"], "000001")
        self.assertEqual(observations[0]["pricing_date"], "2026-08-04")
        self.assertEqual(observations[1]["fund_code"], "")
        snapshot = json.loads(observations[0]["source_snapshot_json"])
        self.assertEqual(snapshot["capture_origin"], "historical_v3_raw_snapshot_recovery")
        self.assertEqual(snapshot["raw_candidate"]["fund_code"], "000001")
        after = {
            table: self.db.row(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in execution_tables
        }
        self.assertEqual(after, before)
        second = recover_historical_v3_recommendation_observations(self.db)
        self.assertEqual((second["tasks_restored"], second["observations_inserted"]), (0, 0))
        self.assertEqual(
            self.db.row("SELECT COUNT(*) AS n FROM recommendation_outcomes")["n"], 10,
        )

    def test_rejects_v2_and_incomplete_jobs(self):
        self._seed_complete_v3(task_id="legacy-v2", protocol_version="2.0")
        result = recover_historical_v3_recommendation_observations(
            self.db, task_ids=["legacy-v2"],
        )
        self.assertEqual((result["tasks_eligible"], result["observations_inserted"]), (0, 0))
        self.assertIn("protocol_version", result["details"][0]["reason"])

        with self.db.connect() as conn:
            conn.execute("UPDATE radar_runs SET task_id='incomplete' WHERE task_id='legacy-v2'")
            conn.execute("UPDATE analysis_jobs SET task_id='incomplete',completed_items=1 WHERE task_id='legacy-v2'")
            payload = json.loads(conn.execute(
                "SELECT raw_json FROM radar_runs WHERE task_id='incomplete'"
            ).fetchone()[0])
            payload["task_id"] = "incomplete"
            payload["protocol_version"] = "3.0"
            conn.execute(
                "UPDATE radar_runs SET raw_json=? WHERE task_id='incomplete'",
                (json.dumps(payload, ensure_ascii=False),),
            )
        result = recover_historical_v3_recommendation_observations(
            self.db, task_ids=["incomplete"],
        )
        self.assertEqual(result["observations_inserted"], 0)
        self.assertIn("完整数量不一致", result["details"][0]["reason"])

    def test_late_fund_evidence_keeps_index_observation_but_drops_fund(self):
        self._seed_complete_v3(candidate_fund_checked_at="2026-08-04T10:00:00+08:00")
        result = recover_historical_v3_recommendation_observations(self.db)
        self.assertEqual(result["observations_inserted"], 2)
        self.assertEqual(result["candidate_funds_kept"], 0)
        self.assertEqual(result["candidate_funds_dropped"], 1)
        row = self.db.row(
            "SELECT * FROM recommendation_observations WHERE universe_id='C930001'"
        )
        self.assertEqual((row["fund_code"], row["fund_selection_kind"]), ("", ""))
        snapshot = json.loads(row["source_snapshot_json"])
        self.assertIn("研究完成后", snapshot["fund_drop_reason"])

    def test_invalid_raw_score_is_rejected_even_if_derived_tables_are_empty(self):
        self._seed_complete_v3()
        with self.db.connect() as conn:
            row = conn.execute("SELECT raw_json FROM radar_runs WHERE task_id='historical-v3'").fetchone()
            payload = json.loads(row[0])
            payload["audit"][0]["recommendation_score"] = 200
            conn.execute(
                "UPDATE radar_runs SET raw_json=? WHERE task_id='historical-v3'",
                (json.dumps(payload, ensure_ascii=False),),
            )
        result = recover_historical_v3_recommendation_observations(self.db)
        self.assertEqual(result["observations_inserted"], 0)
        self.assertIn("0至100", result["details"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
