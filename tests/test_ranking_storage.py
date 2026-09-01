from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from app.catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from app.database import Database, SCHEMA_VERSION
from app.deepseek import DeepSeekError
from app.orchestrator import ResearchOrchestrator
from app.services import (
    _current_ranking_policy_snapshot,
    _replace_candidates,
    import_radar_payload,
    latest_current_radar_run,
)
from app.universe import UNIVERSE_VERSION


class _FixedCalendar:
    def latest_completed_trading_day(self, _now):
        return date(2026, 8, 10)

    def resolve_order(self, submitted_at, *, fund_kind="普通场外"):
        class _Schedule:
            pricing_date = submitted_at.date()
        return _Schedule()

    def advance_trading_days(self, value, count):
        current = value
        for _ in range(count):
            current += timedelta(days=1)
            while current.weekday() >= 5:
                current += timedelta(days=1)
        return current


class RankingStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "ranking.sqlite3")

    def _policy(self) -> tuple[str, dict, str]:
        version, raw = _current_ranking_policy_snapshot(self.db)
        return version, json.loads(raw), raw

    def _payload(self, task_id: str = "v3-storage") -> dict:
        version, policy, _raw = self._policy()
        audit = []
        for number, universe_id in enumerate(("U-AI", "U-CLOUD"), start=1):
            audit.append({
                "id": universe_id,
                "name": f"行业{number}",
                "category": "信息技术",
                "proxy_code": f"93000{number}.CSI",
                "proxy_name": f"代理{number}",
                "returns_pct": {"d1": 1, "d3": 2, "d5": 3, "d10": 4, "d20": 5},
                "volume_signal": "温和放量",
                "breadth_signal": "真实成分股上涨比例58%",
                "position_20d": "价格区间位置62%",
                "crowding": "中",
                "catalyst": "强",
                "source_ids": ["S1"],
                "local_rank": number,
                "pool_route": "趋势确认但未过热",
                "research_tilt": "推荐买" if number == 1 else "观察",
                "timing_status": "等待回调" if number == 1 else "可以买",
                "execution_status": "基金可执行",
                "opportunity_score": 78 - number,
                "trend_score": 80 - number,
                "entry_score": 62 - number,
                "crowding_risk_score": 28 + number,
                "data_confidence": 96,
                "recommendation_score": 71 if number == 1 else 59,
                "score_components": {"policy_version": version, "freshness_ok": True},
                "local_reason": "本地四维可复算评分",
            })
        return {
            "protocol_version": "3.0",
            "task_id": task_id,
            "task_status": "COMPLETE",
            "universe_version": UNIVERSE_VERSION,
            "data_cutoff": "2026-08-10 15:00 +08:00",
            "market_regime": "轮动",
            "universe_total": 2,
            "analyzed_total": 2,
            "ranking_policy_version": version,
            "ranking_policy": policy,
            "market_summary": "V3落库测试",
            "audit": audit,
            "candidates": [{
                "rank": 1,
                "universe_id": "U-AI",
                "index_code": "930001.CSI",
                "sector_name": "行业1",
                "fund_code": "000001",
                "fund_name": "被动联接C",
                "research_tilt": "推荐买",
                "timing_status": "等待回调",
                "execution_status": "基金可执行",
                "recommendation_score": 71,
                "opportunity_score": 77,
                "entry_score": 61,
                "crowding_risk_score": 29,
                "data_confidence": 96,
                "allocation_pct": 0,
                "target_allocation_pct": 12.5,
                "suggested_amount": 0,
                "pool_route": "趋势确认但未过热",
                "suppression_reason": "",
                "execution_reason": "研究看好，但等待回调",
                "correlation_bucket": "AI算力",
            }],
            "avoid_list": [],
            "holding_actions": [],
            "fund_updates": [],
        }

    def _current_patches(self):
        return (
            patch("app.services._catalog_snapshot_ids", return_value=["U-AI", "U-CLOUD"]),
            patch("app.services._catalog_snapshot_hash", return_value="HASH-V3"),
            patch("app.services.ChinaTradingCalendar.from_db", return_value=_FixedCalendar()),
        )

    def test_fresh_database_has_v29_policy_defaults_and_all_storage_columns(self):
        self.assertEqual(int(self.db.setting("schema_version")), SCHEMA_VERSION)
        self.assertEqual(self.db.setting("ranking_policy_version"), "four-dimension-v4")
        expected = {
            "research_tilt", "timing_status", "execution_status",
            "opportunity_score", "entry_score", "crowding_risk_score",
            "data_confidence", "pool_route", "suppression_reason",
            "target_allocation_pct", "execution_reason",
        }
        candidate_columns = {row["name"] for row in self.db.rows("PRAGMA table_info(candidates)")}
        self.assertTrue(expected.issubset(candidate_columns))
        audit_columns = {row["name"] for row in self.db.rows("PRAGMA table_info(radar_audit)")}
        self.assertTrue({
            "trend_score", "recommendation_score", "score_components_json", "local_reason",
        }.issubset(audit_columns))
        tables = {row["name"] for row in self.db.rows(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertTrue({
            "recommendation_observations", "recommendation_outcomes",
        }.issubset(tables))
        evidence_columns = {row["name"] for row in self.db.rows("PRAGMA table_info(sector_evidence)")}
        self.assertTrue({"crowding_level", "catalyst_strength"}.issubset(evidence_columns))

    def test_v27_columns_keep_user_weights_before_v28_algorithm_bump(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO settings(key,value) VALUES('ranking_trend_weight','0.33');
            CREATE TABLE analysis_jobs(id INTEGER PRIMARY KEY);
            CREATE TABLE radar_runs(task_id TEXT PRIMARY KEY);
            CREATE TABLE sector_metrics(id INTEGER PRIMARY KEY);
            CREATE TABLE candidates(id INTEGER PRIMARY KEY);
            CREATE TABLE radar_audit(
                id INTEGER PRIMARY KEY,task_id TEXT NOT NULL,universe_id TEXT NOT NULL
            );
            """
        )
        bare = Database.__new__(Database)
        bare._migrate_v27(conn)
        conn.commit()
        columns = lambda table: {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        self.assertTrue({
            "target_allocation_pct", "execution_reason", "research_tilt",
            "timing_status", "execution_status", "opportunity_score",
            "entry_score", "crowding_risk_score", "data_confidence",
        }.issubset(columns("candidates")))
        self.assertTrue({
            "trend_score", "recommendation_score", "score_components_json", "local_reason",
        }.issubset(columns("radar_audit")))
        self.assertEqual(
            conn.execute(
                "SELECT value FROM settings WHERE key='ranking_trend_weight'"
            ).fetchone()[0],
            "0.33",
        )
        self.assertEqual(
            conn.execute(
                "SELECT value FROM settings WHERE key='ranking_policy_version'"
            ).fetchone()[0],
            "four-dimension-v2",
        )

    def test_v33_repairs_negated_hard_veto_and_keeps_pro_avoid_list(self):
        task_id = "negated-veto-repair"
        components = {
            "entry_quality": 93.8,
            "crowding_risk": 40.0,
            "recommendation_score": 46.147,
            "research_tilt": "千万别买",
            "timing_status": "不宜介入",
            "hard_avoid": True,
            "policy_version": "four-dimension-v3",
        }
        payload = {
            "audit": [{
                "id": "C399996", "research_tilt": "千万别买",
                "timing_status": "不宜介入", "score_components": dict(components),
            }],
            "candidates": [],
            "same_bucket_alternatives": [],
            "avoid_list": [{
                "universe_id": "C399996", "reason": "Pro独立认为政策传导较慢",
            }],
        }
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO radar_runs(
                       task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                       ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, "2026-08-11", "轮动震荡", 1, 1,
                    "four-dimension-v3", "{}", "测试",
                    json.dumps(payload, ensure_ascii=False), "now",
                ),
            )
            conn.execute(
                """INSERT INTO radar_audit(
                       task_id,universe_id,sector_name,category,proxy_code,proxy_name,
                       d1,d3,d5,d10,d20,volume_signal,breadth_signal,position_20d,
                       crowding,catalyst,recommendation_score,entry_score,
                       crowding_risk_score,research_tilt,timing_status,
                       score_components_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, "C399996", "智能家居", "消费", "399996", "智能家居",
                    1, 2, 3, 4, 5, "正常", "代理", "59%",
                    "尚未形成极端拥挤", "政策催化", 46.147, 93.8, 40,
                    "千万别买", "不宜介入",
                    json.dumps(components, ensure_ascii=False),
                ),
            )
            conn.execute(
                """INSERT INTO sector_metrics(
                       task_id,universe_id,proxy_code,proxy_name,d1,d3,d5,d10,d20,
                       volume_signal,breadth_signal,position_20d,data_source,
                       base_score,score_components_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, "C399996", "399996", "智能家居", 1, 2, 3, 4, 5,
                    "正常", "代理", "59%", "test", 46.147,
                    json.dumps(components, ensure_ascii=False),
                ),
            )
            conn.execute(
                """INSERT INTO sector_evidence(
                       task_id,universe_id,crowding_level,crowding,catalyst,risk
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    task_id, "C399996", 40,
                    "关注度提升但尚未形成极端拥挤。", "政策催化", "传导需要时间",
                ),
            )
            self.db._migrate_v33(conn)
        audit = self.db.row(
            "SELECT * FROM radar_audit WHERE task_id=? AND universe_id='C399996'",
            (task_id,),
        )
        repaired = json.loads(audit["score_components_json"])
        self.assertEqual(audit["research_tilt"], "观察")
        self.assertEqual(audit["timing_status"], "等待回调")
        self.assertFalse(repaired["hard_avoid"])
        self.assertEqual(repaired["hard_avoid_reason"], "")
        stored_payload = json.loads(
            self.db.row("SELECT raw_json FROM radar_runs WHERE task_id=?", (task_id,))["raw_json"]
        )
        self.assertEqual(stored_payload["audit"][0]["research_tilt"], "观察")
        self.assertEqual(stored_payload["avoid_list"], payload["avoid_list"])

    def test_v33_keeps_affirmative_hard_veto_but_repairs_visible_tier(self):
        task_id = "affirmative-veto-repair"
        components = {
            "entry_quality": 90, "crowding_risk": 30,
            "recommendation_score": 72, "research_tilt": "千万别买",
            "timing_status": "不宜介入", "hard_avoid": True,
            "policy_version": "four-dimension-v3",
        }
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO radar_runs(
                       task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                       ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "2026-08-11", "轮动", 1, 1, "four-dimension-v3", "{}", "", "{}", "now"),
            )
            conn.execute(
                """INSERT INTO radar_audit(
                       task_id,universe_id,sector_name,category,proxy_code,proxy_name,
                       d1,d3,d5,d10,d20,volume_signal,breadth_signal,position_20d,
                       crowding,catalyst,recommendation_score,entry_score,
                       crowding_risk_score,research_tilt,timing_status,score_components_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, "C-BLOCK", "测试", "测试", "1", "测试", 1, 2, 3, 4, 5,
                    "正常", "代理", "50%", "普通", "无", 72, 90, 30,
                    "千万别买", "不宜介入", json.dumps(components, ensure_ascii=False),
                ),
            )
            conn.execute(
                """INSERT INTO sector_evidence(
                       task_id,universe_id,crowding_level,crowding,catalyst,risk
                   ) VALUES(?,?,?,?,?,?)""",
                (task_id, "C-BLOCK", 30, "普通", "无", "基金已暂停申购"),
            )
            self.db._migrate_v33(conn)
        audit = self.db.row("SELECT * FROM radar_audit WHERE task_id=?", (task_id,))
        repaired = json.loads(audit["score_components_json"])
        self.assertEqual(audit["research_tilt"], "推荐买")
        self.assertEqual(audit["timing_status"], "不宜介入")
        self.assertTrue(repaired["hard_avoid"])
        self.assertIn("暂停申购", repaired["hard_avoid_reason"])

    def test_policy_rejects_old_formula_non_integer_or_out_of_range_pool(self):
        _version, valid, _raw = self._policy()
        for field, value in (
            ("version", "four-dimension-v2"),
            ("pool_min", 20.9),
            ("pool_min", 19),
            ("pool_max", 31),
        ):
            policy = dict(valid)
            policy[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(DeepSeekError):
                    ResearchOrchestrator._validated_ranking_policy(policy)

    def test_replace_candidates_persists_target_and_does_not_turn_zero_amount_into_observe(self):
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO radar_runs(
                       task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                       summary,raw_json,imported_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                ("direct", "2026-08-10", "轮动", 1, 1, "", "{}", "now"),
            )
            _replace_candidates(conn, "direct", self._payload()["candidates"], "now")
        row = self.db.row("SELECT * FROM candidates WHERE task_id='direct'")
        self.assertEqual(row["research_tilt"], "推荐买")
        self.assertEqual(row["status"], "推荐买")
        self.assertEqual(row["timing_status"], "等待回调")
        self.assertEqual(row["suggested_amount"], 0)
        self.assertEqual(row["target_allocation_pct"], 12.5)
        self.assertEqual(row["execution_reason"], "研究看好，但等待回调")

    def test_v3_import_exercises_candidate_and_audit_sql_and_freezes_policy(self):
        payload = self._payload()
        # A writable current run may carry an executable fund only when the
        # exact same-index passive-C whitelist exists.  The performance ledger
        # intentionally enforces this independently from the import parser.
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO universe(
                       index_code,index_name,category,has_buyable_fund,fund_code,status,notes
                   ) VALUES('U-AI','行业1','信息技术',1,'000001','已准入','测试')"""
            )
            index_id = conn.execute(
                """INSERT INTO investable_indices(
                       universe_id,official_index_code,official_index_name,
                       active,radar_enabled
                   ) VALUES('U-AI','930001','行业1',1,1)"""
            ).lastrowid
            conn.execute(
                """INSERT INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,quality_rank,source_url,verified_at,active,
                       passive_verified,relation_kind,verification_algorithm
                   ) VALUES(?, '000001','被动联接C','被动ETF联接','C',
                            '公开渠道可申购',1,'https://example.test/000001','2026-08-10',
                            1,1,'同一指数',?)""",
                (index_id, STRICT_FIELD_SYNC_ALGORITHM_VERSION),
            )
        patches = self._current_patches()
        with patches[0], patches[1], patches[2]:
            result = import_radar_payload(
                self.db, payload, json.dumps(payload, ensure_ascii=False), [],
            )
            current = latest_current_radar_run(self.db)
        self.assertFalse(result["read_only"])
        self.assertIsNotNone(current)
        self.assertEqual(current["task_id"], payload["task_id"])
        _version, _policy, frozen = self._policy()
        run = self.db.row("SELECT * FROM radar_runs WHERE task_id=?", (payload["task_id"],))
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (payload["task_id"],))
        self.assertEqual(run["ranking_policy_json"], frozen)
        self.assertEqual(job["ranking_policy_json"], frozen)
        self.assertEqual(job["universe_hash"], "HASH-V3")
        candidate = self.db.row("SELECT * FROM candidates WHERE task_id=?", (payload["task_id"],))
        self.assertEqual(candidate["research_tilt"], "推荐买")
        self.assertEqual(candidate["suggested_amount"], 0)
        self.assertEqual(candidate["target_allocation_pct"], 12.5)
        audit = self.db.rows(
            "SELECT * FROM radar_audit WHERE task_id=? ORDER BY local_rank", (payload["task_id"],),
        )
        self.assertEqual(len(audit), 2)
        self.assertEqual(audit[0]["research_tilt"], "推荐买")
        self.assertEqual(audit[1]["research_tilt"], "观察")
        self.assertEqual(json.loads(audit[0]["score_components_json"])["freshness_ok"], True)
        # Every seven-tier audit row is frozen for forward validation, while
        # only the actual execution candidate carries a fund code.
        observations = self.db.rows(
            "SELECT * FROM recommendation_observations WHERE task_id=? ORDER BY rank_no",
            (payload["task_id"],),
        )
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0]["fund_code"], "000001")
        self.assertEqual(observations[1]["fund_code"], "")
        self.assertEqual(
            self.db.row(
                "SELECT COUNT(*) AS n FROM recommendation_outcomes WHERE observation_id IN "
                "(SELECT id FROM recommendation_observations WHERE task_id=?)",
                (payload["task_id"],),
            )["n"],
            10,
        )

    def test_v2_is_raw_history_only_and_cannot_overwrite_same_id_v3(self):
        v2 = {
            "protocol_version": "2.0", "task_id": "legacy-v2",
            "data_cutoff": "2026-08-07", "market_regime": "旧行情",
            "universe_total": 186, "analyzed_total": 186,
            "market_summary": "旧研究", "candidates": [{"sector_name": "不应落库"}],
            "audit": [{"id": "OLD"}], "holding_actions": [{"fund_code": "000001"}],
        }
        result = import_radar_payload(self.db, v2, json.dumps(v2, ensure_ascii=False), [])
        self.assertTrue(result["read_only"])
        self.assertIsNotNone(self.db.row("SELECT * FROM radar_runs WHERE task_id='legacy-v2'"))
        for table in ("analysis_jobs", "candidates", "radar_audit", "holding_recommendations"):
            self.assertEqual(
                self.db.row(f"SELECT COUNT(*) AS n FROM {table} WHERE task_id='legacy-v2'")["n"], 0,
            )

        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO radar_runs(
                       task_id,data_cutoff,market_regime,universe_total,analyzed_total,
                       ranking_policy_version,ranking_policy_json,summary,raw_json,imported_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("same-id", "2026-08-10", "V3", 1, 1, "policy", "{}", "V3保留",
                 '{"protocol_version":"3.0"}', "now"),
            )
            conn.execute(
                "INSERT INTO candidates(task_id,rank_no,index_code,sector_name,updated_at) VALUES(?,?,?,?,?)",
                ("same-id", 1, "930001.CSI", "V3候选", "now"),
            )
        v2["task_id"] = "same-id"
        v2["market_summary"] = "绝不能覆盖V3"
        conflict = import_radar_payload(self.db, v2, json.dumps(v2, ensure_ascii=False), [])
        self.assertFalse(conflict["stored_run"])
        self.assertEqual(
            self.db.row("SELECT summary FROM radar_runs WHERE task_id='same-id'")["summary"], "V3保留",
        )
        self.assertEqual(
            self.db.row("SELECT COUNT(*) AS n FROM candidates WHERE task_id='same-id'")["n"], 1,
        )

        # An in-progress V3 job may not have a radar_runs row yet.  A pasted
        # legacy artifact with the same id must not delete that frozen job.
        self.db.execute(
            """INSERT INTO analysis_jobs(
                   task_id,mode,stage,status,total_items,completed_items,cost_limit_cny,
                   estimated_cost_cny,universe_version,research_date,universe_ids_json,
                   universe_hash,ranking_policy_version,ranking_policy_json,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "running-v3", "完整扫描", "行情数据", "运行中", 2, 0, 2, 0,
                UNIVERSE_VERSION, "2026-08-10", '["U-AI","U-CLOUD"]', "HASH",
                "four-dimension-v1", '{}', '', "now", "now",
            ),
        )
        v2["task_id"] = "running-v3"
        running_conflict = import_radar_payload(
            self.db, v2, json.dumps(v2, ensure_ascii=False), [],
        )
        self.assertFalse(running_conflict["stored_run"])
        self.assertIsNotNone(
            self.db.row("SELECT * FROM analysis_jobs WHERE task_id='running-v3'")
        )
        self.assertIsNone(
            self.db.row("SELECT * FROM radar_runs WHERE task_id='running-v3'")
        )

    def test_policy_snapshot_uses_one_bulk_settings_read(self):
        class BulkOnly:
            calls = 0

            def settings(self):
                self.calls += 1
                return {"ranking_policy_version": "bulk-v1"}

            def setting(self, *_args):  # pragma: no cover - must never run
                raise AssertionError("逐项setting读取会重新引入UI数据库往返")

        fake = BulkOnly()
        version, raw = _current_ranking_policy_snapshot(fake)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(version, "bulk-v1")
        self.assertEqual(json.loads(raw)["trend_weight"], 0.25)


if __name__ == "__main__":
    unittest.main()
