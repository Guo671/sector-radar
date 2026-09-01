from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app.config import local_now
from app.catalog_policy import STRICT_FIELD_SYNC_ALGORITHM_VERSION
from app.database import Database
from app.deepseek import DeepSeekError, ModelResult
from app.orchestrator import ResearchOrchestrator
from app.platform_catalog import PlatformCatalogService
from app.ranking import Scorecard, build_scorecards, select_candidate_pool
from app.universe import UNIVERSE_VERSION


class ResearchIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "radar.sqlite3"

    @staticmethod
    def _ensure_scan_items(db, count: int) -> list[dict]:
        catalog = PlatformCatalogService(db)
        suffix = 0
        while len(catalog.radar_universe()) < count:
            if suffix > count + 10:
                raise AssertionError("测试补位指数未能进入严格C目录，停止以避免无限循环。")
            code = f"939{900 + suffix:03d}"
            index_id = catalog.add_candidate(
                industry_group="测试补位", platform_code=f"{code}.CSI",
                platform_name=f"测试补位{suffix}", official_code=code,
                official_name=f"测试补位正式指数{suffix}",
                official_source_url=f"https://www.csindex.com.cn/#/indices/family/detail?indexCode={code}",
                evidence_kind="中证官方事实表", security_id=f"2.{code}",
                proxy_code=code, proxy_name=f"测试补位正式指数{suffix}",
                mapping_confidence=0.99,
            )
            stamp = local_now().isoformat(timespec="seconds")
            db.execute(
                """INSERT INTO investable_funds(
                       investable_index_id,fund_code,fund_name,strategy_kind,share_class,
                       public_buyable_status,active,passive_verified,relation_kind,
                       verified_at,platform_checked_at,radar_eligible,verification_algorithm)
                    VALUES(?,?,?,'被动ETF联接','C','公开渠道可申购',1,1,'同一指数',?,?,0,?)""",
                (
                    index_id, f"89{suffix:04d}", f"测试补位ETF联接C{suffix}",
                    stamp, stamp, STRICT_FIELD_SYNC_ALGORITHM_VERSION,
                ),
            )
            db.save_settings({
                "csi_fund_sync_algorithm_version": STRICT_FIELD_SYNC_ALGORITHM_VERSION,
            })
            catalog.refresh_admission(index_id)
            suffix += 1
        return catalog.radar_universe()

    def test_migrations_run_once_and_do_not_reclassify_user_capital(self):
        class CountingDatabase(Database):
            v22_calls = 0

            def _migrate_v22(self, conn):
                type(self).v22_calls += 1
                return super()._migrate_v22(conn)

        db = CountingDatabase(self.path)
        self.assertEqual(CountingDatabase.v22_calls, 1)
        holding = db.row("SELECT id FROM holdings ORDER BY id LIMIT 1")
        db.execute(
            "UPDATE holdings SET counts_toward_active_capital=0 WHERE id=?",
            (holding["id"],),
        )
        reopened = CountingDatabase(self.path)
        self.assertEqual(CountingDatabase.v22_calls, 1)
        self.assertEqual(
            reopened.row("SELECT counts_toward_active_capital FROM holdings WHERE id=?", (holding["id"],))[
                "counts_toward_active_capital"
            ],
            0,
        )

    def test_job_freezes_ordered_catalog_and_rejects_legacy_or_resliced_resume(self):
        db = Database(self.path)
        orchestrator = ResearchOrchestrator(db)
        current = self._ensure_scan_items(db, 2)
        task_id = orchestrator.create_job("20项行情测试")
        job = db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        ids = json.loads(job["universe_ids_json"])
        self.assertEqual(len(ids), min(10, len(current)))
        self.assertEqual(job["universe_version"], UNIVERSE_VERSION)
        self.assertEqual(job["universe_hash"], orchestrator._universe_hash(ids))
        self.assertEqual(
            [row["index_code"] for row in orchestrator._load_job_universe(job)],
            ids,
        )

        shortened = ids + ["C_NOT_IN_FROZEN_CATALOG"]
        db.execute(
            "UPDATE analysis_jobs SET total_items=?,universe_ids_json=?,universe_hash=? WHERE task_id=?",
            (len(shortened), json.dumps(shortened), orchestrator._universe_hash(shortened), task_id),
        )
        with self.assertRaisesRegex(DeepSeekError, "旧66项切片|顺序或成员"):
            orchestrator._load_job_universe(
                db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
            )

        db.execute(
            "UPDATE analysis_jobs SET universe_version='',research_date='',universe_ids_json='[]',universe_hash='' WHERE task_id=?",
            (task_id,),
        )
        with self.assertRaisesRegex(DeepSeekError, "旧版|未固化"):
            orchestrator.validate_current_job(task_id)

    def test_cross_day_job_and_evidence_are_not_reused(self):
        db = Database(self.path)
        self._ensure_scan_items(db, 2)
        orchestrator = ResearchOrchestrator(db)
        task_id = orchestrator.create_job("20方向测试")
        job = db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        ids = json.loads(job["universe_ids_json"])
        yesterday = (local_now().date() - timedelta(days=1)).isoformat()
        today = local_now().date().isoformat()
        db.executemany(
            """INSERT INTO sector_evidence(
                   task_id,universe_id,crowding,catalyst,risk,source_json,web_search_calls,
                   evidence_date,universe_version) VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (task_id, ids[0], "低", "旧催化", "旧风险", "[]", 1, yesterday, UNIVERSE_VERSION),
                (task_id, ids[1], "低", "新催化", "新风险", "[]", 1, today, UNIVERSE_VERSION),
            ],
        )
        self.assertEqual([row["universe_id"] for row in orchestrator._current_evidence(task_id)], [ids[1]])
        db.execute("UPDATE analysis_jobs SET research_date=? WHERE task_id=?", (yesterday, task_id))
        with self.assertRaisesRegex(DeepSeekError, "跨日复用|研究日"):
            orchestrator.validate_current_job(task_id)

    def test_uncertain_flash_reservation_blocks_any_automatic_repeat(self):
        db = Database(self.path)
        self._ensure_scan_items(db, 3)
        orchestrator = ResearchOrchestrator(db)
        task_id = orchestrator.create_job("20方向测试")
        batch_ids = json.loads(
            db.row("SELECT universe_ids_json FROM analysis_jobs WHERE task_id=?", (task_id,))[
                "universe_ids_json"
            ]
        )[:3]
        call_id = orchestrator._reserve_model_call(
            task_id,
            "Flash联网检索",
            "deepseek-v4-flash",
            0.05,
            request_key=orchestrator._stable_request_key(
                "flash-search", {"batch_ids": batch_ids}
            ),
            context={"phase": "search", "batch_ids": batch_ids},
        )
        self.assertEqual(orchestrator._flash_batch_recovery(task_id, batch_ids)["phase"], "transport_uncertain")
        orchestrator._mark_reserved_model_call_uncertain(call_id, DeepSeekError("模拟网络中断"))
        self.assertEqual(orchestrator._flash_batch_recovery(task_id, batch_ids)["phase"], "transport_uncertain")

    def test_job_lease_blocks_second_process_and_can_be_released(self):
        db = Database(self.path)
        self._ensure_scan_items(db, 1)
        first = ResearchOrchestrator(db)
        second = ResearchOrchestrator(db)
        task_id = first.create_job("20项行情测试")
        owner = first._acquire_job_lease(task_id)
        with self.assertRaisesRegex(DeepSeekError, "重复扫描|正在另一个"):
            second._acquire_job_lease(task_id)
        first._release_job_lease(task_id, owner)
        next_owner = second._acquire_job_lease(task_id)
        self.assertTrue(next_owner)
        second._release_job_lease(task_id, next_owner)

    def test_model_configuration_is_frozen_when_job_is_created(self):
        db = Database(self.path)
        self._ensure_scan_items(db, 1)
        db.save_settings({
            "pro_input_price_cny_per_million": "3.15",
            "api_full_batch_size": "6",
        })
        orchestrator = ResearchOrchestrator(db)
        task_id = orchestrator.create_job("完整扫描")
        db.save_settings({
            "pro_input_price_cny_per_million": "99",
            "api_full_batch_size": "12",
        })
        frozen = orchestrator._model_config_for_job(task_id)
        self.assertEqual(frozen["pro_input_price_cny_per_million"], "3.15")
        self.assertEqual(frozen["api_full_batch_size"], "6")

    def test_full_scan_cost_estimate_reprices_real_usage_without_mutation(self):
        db = Database(self.path)
        db.save_settings({
            "api_full_batch_size": "6",
            "api_full_cost_limit_cny": "2.00",
            "flash_input_price_cny_per_million": "1",
            "flash_cached_input_price_cny_per_million": "0.02",
            "flash_output_price_cny_per_million": "2",
            "pro_input_price_cny_per_million": "3",
            "pro_cached_input_price_cny_per_million": "0.025",
            "pro_output_price_cny_per_million": "6",
        })
        now = local_now().isoformat(timespec="seconds")
        task_id = "cost-history-82"
        db.execute(
            """INSERT INTO analysis_jobs(
                   task_id,mode,stage,status,total_items,completed_items,cost_limit_cny,
                   estimated_cost_cny,created_at,updated_at)
               VALUES (?,'完整扫描','完成','已完成',82,82,2,0.5,?,?)""",
            (task_id, now, now),
        )
        db.executemany(
            """INSERT INTO model_calls(
                   task_id,stage,model,input_tokens,output_tokens,cached_tokens,
                   estimated_cost_cny,response_status,created_at)
               VALUES (?,?,?,?,?,?,0.01,'completed',?)""",
            [
                (task_id, "Flash联网检索", "deepseek-v4-flash", 1000, 500, 200, now)
                for _ in range(14)
            ] + [
                (task_id, "Pro终审", "deepseek-v4-pro", 2000, 1000, 0, now)
            ],
        )
        before_jobs = db.row("SELECT COUNT(*) AS n FROM analysis_jobs")["n"]
        before_calls = db.row("SELECT COUNT(*) AS n FROM model_calls")["n"]
        estimate = ResearchOrchestrator(db).estimate_full_scan_cost(82)
        self.assertEqual(estimate.flash_batch_count, 14)
        self.assertEqual(estimate.pro_call_count, 1)
        self.assertEqual(estimate.historical_task_id, task_id)
        expected = 14 * ((800 * 1 + 200 * .02 + 500 * 2) / 1_000_000) + (
            2000 * 3 + 1000 * 6
        ) / 1_000_000
        self.assertAlmostEqual(estimate.historical_repriced_cny, expected, places=4)
        self.assertLess(estimate.expected_low_cny, estimate.expected_high_cny)
        self.assertGreater(estimate.conservative_clean_run_cny, estimate.expected_high_cny)
        self.assertIn("预算阈值为¥2.00", estimate.confirmation_text())
        self.assertEqual(db.row("SELECT COUNT(*) AS n FROM analysis_jobs")["n"], before_jobs)
        self.assertEqual(db.row("SELECT COUNT(*) AS n FROM model_calls")["n"], before_calls)

    def test_full_scan_cost_estimate_has_conservative_first_run_fallback(self):
        db = Database(self.path)
        estimate = ResearchOrchestrator(db).estimate_full_scan_cost(82)
        self.assertIsNone(estimate.historical_repriced_cny)
        self.assertEqual(estimate.flash_batch_count, 14)
        self.assertGreater(estimate.expected_low_cny, 0)
        self.assertGreater(estimate.conservative_clean_run_cny, estimate.expected_high_cny)
        self.assertIn("尚无同规模成功轮次", estimate.confirmation_text())

    def test_request_key_is_unique_and_completed_pro_is_replayed_locally(self):
        class NeverCallClient:
            pro_calls = 0

            def pro_json(self, **_kwargs):
                self.pro_calls += 1
                raise AssertionError("本地重放不得再次调用Pro")

        db = Database(self.path)
        self._ensure_scan_items(db, 1)
        task_id = ResearchOrchestrator(db).create_job("完整扫描")
        client = NeverCallClient()
        orchestrator = ResearchOrchestrator(db, client=client)
        config = orchestrator._model_config_for_job(task_id)
        model = config["deepseek_pro_model"]
        system = orchestrator._pro_decision_system(False)
        prompt = '{"task_id":"local-replay"}'
        max_tokens = 4000
        request_key = orchestrator._stable_request_key(
            "pro-decision", {"logical_call": "once"}
        )
        fingerprint = orchestrator._request_fingerprint({
            "system": system,
            "prompt": prompt,
            "model": model,
            "max_tokens": max_tokens,
            "reasoning_effort": "max",
        })
        call_id = orchestrator._reserve_model_call(
            task_id,
            "Pro终审",
            model,
            0.02,
            request_key=request_key,
            context={
                "phase": "pro_decision_only",
                "request_fingerprint": fingerprint,
                "max_tokens": max_tokens,
            },
        )
        with self.assertRaisesRegex(DeepSeekError, "已经存在|重复发送"):
            orchestrator._reserve_model_call(
                task_id,
                "Pro终审",
                model,
                0.02,
                request_key=request_key,
                context={"phase": "pro_decision_only"},
            )
        raw = {
            "id": "response-local-replay",
            "model": model,
            "choices": [{
                "message": {"content": '{"market_regime":"轮动震荡"}'},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        orchestrator._finalize_reserved_model_call(
            call_id,
            ModelResult(
                text='{"market_regime":"轮动震荡"}',
                raw=raw,
                model=model,
                input_tokens=10,
                output_tokens=5,
                estimated_cost_cny=0.001,
                response_kind="chat",
                finish_reason="stop",
            ),
        )
        job = db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        replayed = orchestrator._obtain_pro_decision_result(
            task_id=task_id,
            job=job,
            universe_count=1,
            audit_count=1,
            prompt_text=prompt,
            system_text=system,
            model=model,
            desired_output_tokens=max_tokens,
            reasoning_effort="max",
            input_price=float(config["pro_input_price_cny_per_million"]),
            cached_price=float(config["pro_cached_input_price_cny_per_million"]),
            output_price=float(config["pro_output_price_cny_per_million"]),
            request_key=request_key,
            existing_call=db.row("SELECT * FROM model_calls WHERE id=?", (call_id,)),
        )
        self.assertEqual(replayed.text, '{"market_regime":"轮动震荡"}')
        self.assertEqual(client.pro_calls, 0)
        self.assertEqual(
            db.row("SELECT COUNT(*) n FROM model_calls WHERE task_id=?", (task_id,))["n"],
            1,
        )

    def test_transport_uncertain_pro_is_never_sent_again(self):
        class NeverCallClient:
            pro_calls = 0

            def pro_json(self, **_kwargs):
                self.pro_calls += 1
                raise AssertionError("不明状态不得重发")

        db = Database(self.path)
        self._ensure_scan_items(db, 1)
        task_id = ResearchOrchestrator(db).create_job("完整扫描")
        client = NeverCallClient()
        orchestrator = ResearchOrchestrator(db, client=client)
        config = orchestrator._model_config_for_job(task_id)
        model = config["deepseek_pro_model"]
        system = orchestrator._pro_decision_system(False)
        prompt = '{"task_id":"uncertain"}'
        max_tokens = 4000
        request_key = orchestrator._stable_request_key(
            "pro-decision", {"logical_call": "once"}
        )
        fingerprint = orchestrator._request_fingerprint({
            "system": system,
            "prompt": prompt,
            "model": model,
            "max_tokens": max_tokens,
            "reasoning_effort": "max",
        })
        call_id = orchestrator._reserve_model_call(
            task_id,
            "Pro终审",
            model,
            0.02,
            request_key=request_key,
            context={
                "phase": "pro_decision_only",
                "request_fingerprint": fingerprint,
                "max_tokens": max_tokens,
            },
        )
        orchestrator._mark_reserved_model_call_uncertain(call_id, DeepSeekError("模拟断线"))
        job = db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        with self.assertRaisesRegex(DeepSeekError, "禁止自动重发|可能已经计费"):
            orchestrator._obtain_pro_decision_result(
                task_id=task_id,
                job=job,
                universe_count=1,
                audit_count=1,
                prompt_text=prompt,
                system_text=system,
                model=model,
                desired_output_tokens=max_tokens,
                reasoning_effort="max",
                input_price=float(config["pro_input_price_cny_per_million"]),
                cached_price=float(config["pro_cached_input_price_cny_per_million"]),
                output_price=float(config["pro_output_price_cny_per_million"]),
                request_key=request_key,
                existing_call=db.row("SELECT * FROM model_calls WHERE id=?", (call_id,)),
            )
        self.assertEqual(client.pro_calls, 0)

    def test_startup_recovers_running_job_as_resumable_failure(self):
        db = Database(self.path)
        self._ensure_scan_items(db, 1)
        task_id = ResearchOrchestrator(db).create_job("20项行情测试")
        db.execute(
            "UPDATE analysis_jobs SET status='运行中',stage='行情数据',lease_owner='dead-process' WHERE task_id=?",
            (task_id,),
        )
        self.assertEqual(db.recover_interrupted_jobs(), 1)
        recovered = db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,))
        self.assertEqual(recovered["status"], "失败")
        self.assertEqual(recovered["stage"], "启动恢复")
        self.assertEqual(recovered["lease_owner"], "")
        self.assertIn("上次软件进程", recovered["error"])

    def test_automatic_backup_retention_and_atomic_restore(self):
        backup_root = Path(self.tmp.name) / "automatic-backups"
        db = Database(self.path)
        db.save_settings({"backup_keep_count": "3", "backup_keep_days": "3650"})
        original_name = db.row("SELECT fund_name FROM funds WHERE fund_code='020741'")["fund_name"]
        source = Path(self.tmp.name) / "manual-source.sqlite3"
        db.backup(source)
        db.execute("UPDATE funds SET fund_name='被修改的名称' WHERE fund_code='020741'")
        with patch("app.database.BACKUP_DIR", backup_root):
            for _index in range(6):
                db.backup()
            # A same-day burst is bounded immediately while the newest three
            # remain unconditional restore points.
            automatic = sorted(backup_root.glob("板块雷达备份_*.sqlite3"))
            self.assertEqual(len(automatic), 3)
            old = local_now().timestamp() - 4000 * 86400
            for path in automatic:
                os.utime(path, (old, old))
            stale_wal = Path(f"{automatic[-1]}-wal")
            stale_shm = Path(f"{automatic[-1]}-shm")
            stale_wal.write_bytes(b"")
            stale_shm.write_bytes(b"rebuildable-index")
            result = db.prune_backups()
            automatic = sorted(backup_root.glob("板块雷达备份_*.sqlite3"))
            self.assertEqual(len(automatic), 3)
            self.assertEqual(result["retained_latest"], 3)
            self.assertEqual(result["removed"], 0)
            self.assertEqual(result["sidecars_removed"], 2)
            self.assertGreater(result["sidecar_bytes_freed"], 0)
            self.assertFalse(stale_wal.exists() or stale_shm.exists())
            safety = db.restore(source)
        self.assertTrue(safety.exists())
        self.assertEqual(
            db.row("SELECT fund_name FROM funds WHERE fund_code='020741'")["fund_name"],
            original_name,
        )
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_automatic_backup_retention_can_stop_before_disk_scan(self):
        backup_root = Path(self.tmp.name) / "automatic-backups"
        db = Database(self.path)
        with patch("app.database.BACKUP_DIR", backup_root):
            result = db.prune_backups(stop_requested=lambda: True)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["sidecars_removed"], 0)

    def test_deployment_backups_require_explicit_cleanup(self):
        backup_root = Path(self.tmp.name) / "deployment-backups"
        db = Database(self.path)
        db.save_settings({
            "deployment_backup_keep_count": "2",
            "deployment_backup_keep_days": "30",
        })
        names = [
            "sector_radar-predeploy-v4.4.0-alpha.36-20260817-120000-001.sqlite3",
            "sector_radar-predeploy-v4.4.0-alpha.37-20260817-130000-002.sqlite3",
            "sector_radar-predeploy-v4.4.0-alpha.38-20260817-140000-003.sqlite3",
            "sector_radar-predeploy-v4.4.0-alpha.39-20260817-150000-004.sqlite3",
        ]
        with patch("app.database.BACKUP_DIR", backup_root):
            paths = [db.backup(backup_root / name) for name in names]
            wal = Path(f"{paths[0]}-wal"); wal.write_bytes(b"")
            shm = Path(f"{paths[0]}-shm"); shm.write_bytes(b"rebuildable-index")
            manual = db.backup(backup_root / "我的发布前手工备份.sqlite3")

            automatic_result = db.prune_backups()
            self.assertTrue(all(path.exists() for path in paths))
            self.assertTrue(wal.exists() and shm.exists())
            self.assertEqual(automatic_result["deployment_removed"], 0)
            self.assertFalse(automatic_result["deployment_cleanup_requested"])

            explicit_result = db.prune_backups(include_deployment=True)

        self.assertFalse(paths[0].exists() or paths[1].exists())
        self.assertTrue(paths[2].exists() and paths[3].exists())
        self.assertTrue(manual.exists())
        self.assertFalse(wal.exists() or shm.exists())
        self.assertEqual(explicit_result["deployment_removed"], 2)
        self.assertEqual(explicit_result["deployment_sidecars_removed"], 2)
        self.assertTrue(explicit_result["deployment_cleanup_requested"])

    def test_four_dimension_pool_is_reproducible_and_pro_can_reselect_without_scoring(self):
        db = Database(self.path)
        orchestrator = ResearchOrchestrator(db)
        metric = {
            "universe_id": "C000001",
            "d1": 1, "d3": 2, "d5": 3, "d10": 4, "d20": 5,
            "amount_ratio_5d": 1.2,
            "position_20d_pct": 60,
            "volatility_20d": 18,
            "consecutive_up_days": 2,
            "breadth_kind": "true_component_breadth",
            "breadth_pct": 64,
            "data_cutoff": "2026-08-10",
            "data_quality": "COMPLETE",
        }
        evidence = {
            "crowding": "中", "crowding_level": "中",
            "catalyst": "产业订单改善", "catalyst_strength": "中",
            "risk": "常规波动",
            "source_json": '[{"id":"S1"},{"id":"S2"}]',
        }
        first = build_scorecards(
            [metric], {"C000001": evidence}, expected_cutoff="2026-08-10"
        )["C000001"]
        second = build_scorecards(
            [dict(metric)], {"C000001": dict(evidence)}, expected_cutoff="2026-08-10"
        )["C000001"]
        self.assertEqual(first, second)
        self.assertEqual(
            set((first.as_components().keys())) & {
                "trend_opportunity", "entry_quality", "crowding_risk", "data_confidence"
            },
            {"trend_opportunity", "entry_quality", "crowding_risk", "data_confidence"},
        )
        low_confidence_metric = {**metric, "data_quality": "UNKNOWN"}
        low_confidence = build_scorecards(
            [low_confidence_metric], {"C000001": evidence}, expected_cutoff="2026-08-10"
        )["C000001"]
        self.assertLess(low_confidence.data_confidence, first.data_confidence)
        self.assertEqual(low_confidence.opportunity_score, first.opportunity_score)
        self.assertEqual(low_confidence.recommendation_score, first.recommendation_score)

        def card(uid: str, route: str, offset: int) -> Scorecard:
            if route == "trend":
                trend, entry, crowding, catalyst = 90, 50, 30, 30
            elif route == "rebound":
                trend, entry, crowding, catalyst = 45, 90, 30, 30
            else:
                trend, entry, crowding, catalyst = 30, 40, 20, 90
            return Scorecard(
                universe_id=uid,
                trend_opportunity=trend,
                entry_quality=entry,
                crowding_risk=crowding,
                data_confidence=90,
                catalyst_quality=catalyst,
                relative_strength=60,
                volume_breadth=60,
                opportunity_score=70,
                recommendation_score=70 - offset / 100,
                research_tilt="推荐买",
                timing_status="可以买",
                hard_avoid=False,
                position_20d_pct=55,
                weighted_return=2,
                true_breadth_pct=60,
                freshness_ok=True,
            )

        cards: dict[str, Scorecard] = {}
        clusters: dict[str, str] = {}
        for route in ("trend", "rebound", "catalyst"):
            for index in range(9):
                uid = f"{route}-{index}"
                cards[uid] = card(uid, route, index)
                clusters[uid] = uid
        pool = select_candidate_pool(
            cards, clusters, route_size=9, minimum_size=20,
            maximum_size=30, max_per_cluster=4, minimum_score=45,
        )
        self.assertEqual(len(pool), 27)
        self.assertEqual(
            Counter(row["pool_route"] for row in pool),
            Counter({
                "趋势确认·不过热": 9,
                "回调结束·重新转强": 9,
                "早期催化·低拥挤": 9,
            }),
        )

        scores = {uid: cards[uid].recommendation_score for uid in cards}
        allowed = {row["universe_id"] for row in pool}
        chosen_ids = {"trend-4", "rebound-7", "catalyst-2"}
        candidates = [
            {"universe_id": universe_id, "score_adjustment": 5}
            for universe_id in chosen_ids
        ]
        normalized = orchestrator._normalize_ranked_scores(
            candidates, scores, allowed_ids=allowed
        )
        self.assertEqual({row["universe_id"] for row in normalized}, chosen_ids)
        self.assertTrue(all(row["score_adjustment"] == 0 for row in normalized))
        self.assertTrue(all(row["score"] == scores[row["universe_id"]] for row in normalized))
        with self.assertRaisesRegex(DeepSeekError, "不在本轮20—30项候选池内"):
            orchestrator._normalize_ranked_scores(
                candidates + [{"universe_id": "OUTSIDE", "score_adjustment": 0}],
                {**scores, "OUTSIDE": 99},
                allowed_ids=allowed,
            )
        eleven = [{"universe_id": row["universe_id"]} for row in pool[:11]]
        with self.assertRaisesRegex(DeepSeekError, "最多选择10项"):
            orchestrator._normalize_ranked_scores(eleven, scores, allowed_ids=allowed)


if __name__ == "__main__":
    unittest.main()
