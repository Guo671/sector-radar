from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path

from app.runtime_housekeeping import (
    automatic_backup_sidecar_cleanup_plan,
    automatic_backup_retention_plan,
    cleanup_managed_storage,
    cleanup_runtime_cache,
    deployment_backup_retention_plan,
    deployment_backup_sidecar_cleanup_plan,
    managed_storage_policy,
    prune_automatic_backup_sidecars,
    prune_deployment_backup_sidecars,
    runtime_cleanup_due,
    storage_inventory,
    valid_automatic_backup_file,
    valid_deployment_backup_file,
)


class RuntimeHousekeepingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data" / "runtime"
        self.root.mkdir(parents=True)

    @staticmethod
    def _old(path: Path) -> None:
        os.utime(path, (100.0, 100.0))

    @staticmethod
    def _at(value: str) -> float:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()

    def test_runtime_cleanup_runs_at_most_once_per_day(self):
        zone = timezone(timedelta(hours=8))
        now = datetime(2026, 8, 16, 23, 0, tzinfo=zone)
        self.assertTrue(runtime_cleanup_due("", now=now))
        self.assertTrue(runtime_cleanup_due("not-a-time", now=now))
        self.assertTrue(runtime_cleanup_due("2026-08-15T22:59:59+08:00", now=now))
        self.assertFalse(runtime_cleanup_due("2026-08-16T22:00:00+08:00", now=now))
        self.assertFalse(runtime_cleanup_due("2026-08-17T01:00:00+08:00", now=now))

    def _rollback(self, name: str, *, database: bool = False) -> Path:
        return self._rollback_in(self.root, name, database=database)

    def _rollback_in(
        self, root: Path, name: str, *, database: bool = False,
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        (path / "_internal").mkdir(parents=True)
        (path / "板块雷达.exe").write_bytes(b"exe")
        if database:
            (path / "_internal" / "user.sqlite3").write_bytes(b"protect")
        self._old(path / "板块雷达.exe")
        if database:
            self._old(path / "_internal" / "user.sqlite3")
        self._old(path / "_internal")
        self._old(path)
        return path

    def _migration(self, name: str, *, corrupt: bool = False) -> Path:
        path = self.root / name
        path.mkdir()
        database = path / "migration-copy.sqlite3"
        if corrupt:
            database.write_bytes(b"not sqlite")
        else:
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE proof(value TEXT)")
            connection.commit()
            connection.close()
        self._old(database)
        self._old(path)
        return path

    @staticmethod
    def _diagnostic(path: Path, *, valid: bool = True) -> Path:
        payload = {
            "note": (
                "脱敏结构诊断：不含API Key，也不含模型正文与搜索正文。"
                if valid else "用户自己的普通JSON"
            )
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.utime(path, (100.0, 100.0))
        return path

    def test_only_stale_direct_mei_folders_are_removed(self):
        stale = self.root / "_MEI12345"
        stale.mkdir(); (stale / "python.dll").write_bytes(b"x" * 20)
        self._old(stale / "python.dll"); self._old(stale)
        not_ours = self.root / "_MEI-wrong"
        not_ours.mkdir(); self._old(not_ours)
        ordinary = self.root / "research-cache"
        ordinary.mkdir(); self._old(ordinary)

        result = cleanup_runtime_cache(self.root, min_age_seconds=1, now=1000)

        self.assertFalse(stale.exists())
        self.assertTrue(not_ours.exists())
        self.assertTrue(ordinary.exists())
        self.assertEqual(result["removed_directories"], 1)
        self.assertEqual(result["bytes_freed"], 20)

    def test_active_and_recent_directories_are_never_removed(self):
        active = self.root / "_MEI111"
        active.mkdir(); self._old(active)
        recent = self.root / "_MEI222"
        recent.mkdir(); os.utime(recent, (950.0, 950.0))

        result = cleanup_runtime_cache(
            self.root,
            min_age_seconds=100,
            now=1000,
            current_runtime=active,
        )

        self.assertTrue(active.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(result["skipped_active"], 1)
        self.assertEqual(result["skipped_recent"], 1)

    def test_old_deepseek_tmp_is_removed_but_other_files_remain(self):
        deepseek = self.root / "deepseek"
        deepseek.mkdir()
        old_tmp = deepseek / "deepseek-stream-old.sse.tmp"
        old_tmp.write_bytes(b"old"); self._old(old_tmp)
        recent_tmp = deepseek / "deepseek-request-new.json.tmp"
        recent_tmp.write_bytes(b"new"); os.utime(recent_tmp, (950.0, 950.0))
        evidence = deepseek / "response.json"
        evidence.write_text("keep", encoding="utf-8"); self._old(evidence)

        result = cleanup_runtime_cache(
            self.root,
            min_age_seconds=1,
            temporary_file_age_seconds=100,
            now=1000,
        )

        self.assertFalse(old_tmp.exists())
        self.assertTrue(recent_tmp.exists())
        self.assertTrue(evidence.exists())
        self.assertEqual(result["removed_temporary_files"], 1)

    def test_candidate_with_nested_reparse_point_is_never_removed(self):
        candidate = self.root / "_MEI333"
        candidate.mkdir(); (candidate / "payload.dll").write_bytes(b"keep")
        self._old(candidate / "payload.dll"); self._old(candidate)

        with mock.patch(
            "app.runtime_housekeeping._contains_reparse_point",
            return_value=True,
        ):
            result = cleanup_runtime_cache(
                self.root,
                min_age_seconds=1,
                now=1000,
            )

        self.assertTrue(candidate.exists())
        self.assertEqual(result["removed_directories"], 0)
        self.assertEqual(result["skipped_unsafe"], 1)

    def test_policy_is_bounded_even_when_settings_are_invalid(self):
        policy = managed_storage_policy({
            "deployment_backup_keep_count": "-50",
            "deployment_backup_keep_days": "bad",
            "rollback_keep_count": "-50",
            "rollback_keep_days": "bad",
            "diagnostic_keep_count": "9999",
            "diagnostic_keep_days": "1",
        })
        self.assertEqual(policy["deployment_backup_keep_count"], 2)
        self.assertEqual(policy["deployment_backup_keep_days"], 30)
        self.assertEqual(policy["rollback_keep_count"], 2)
        self.assertEqual(policy["rollback_keep_days"], 30)
        self.assertEqual(policy["diagnostic_keep_count"], 200)
        self.assertEqual(policy["diagnostic_keep_days"], 30)

    def test_expired_rollbacks_keep_the_newest_count(self):
        paths = [
            self._rollback(f"predeploy-v4.4.0-alpha.{number}-2026080{number}-010101")
            for number in range(1, 5)
        ]
        for index, path in enumerate(paths):
            stamp = 100.0 + index
            os.utime(path, (stamp, stamp))
        exports = Path(self.temp.name) / "data" / "exports"
        result = cleanup_managed_storage(
            self.root, exports,
            settings={"rollback_keep_count": 2, "rollback_keep_days": 7},
            now=self._at("2026-08-20 12:00:00"),
        )
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertTrue(paths[2].exists())
        self.assertTrue(paths[3].exists())
        self.assertEqual(result["rollback_directories"]["removed"], 2)

    def test_recent_distinct_daily_rollbacks_are_kept_beyond_latest_count(self):
        paths = [
            self._rollback(f"predeploy-v4.4.0-alpha.{number}-2026080{number}-010101")
            for number in range(1, 5)
        ]
        for index, path in enumerate(paths):
            stamp = 1000.0 + index
            os.utime(path, (stamp, stamp))
        exports = Path(self.temp.name) / "data" / "exports"
        result = cleanup_managed_storage(
            self.root, exports,
            settings={"rollback_keep_count": 2, "rollback_keep_days": 7},
            now=self._at("2026-08-08 00:00:00"),
        )
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual(result["rollback_directories"]["removed"], 0)
        self.assertEqual(result["rollback_directories"]["retained_latest"], 2)
        self.assertEqual(result["rollback_directories"]["retained_daily"], 2)

    def test_recent_same_day_duplicates_are_bounded_and_millisecond_names_work(self):
        names = [
            "predeploy-v4.4.0-alpha.1-20260807-080000",
            "predeploy-v4.4.0-alpha.2-20260807-090000",
            "predeploy-v4.4.0-alpha.3-20260808-080000",
            "predeploy-v4.4.0-alpha.4-20260808-090000-123",
            "predeploy-v4.4.0-alpha.5-20260808-100000-456789",
        ]
        paths = [self._rollback(name) for name in names]
        exports = Path(self.temp.name) / "data" / "exports"
        backups = Path(self.temp.name) / "data" / "backups"

        inventory = storage_inventory(
            self.root, backups, exports,
            settings={"rollback_keep_count": 2, "rollback_keep_days": 7},
            now=self._at("2026-08-09 12:00:00"),
        )

        self.assertEqual(inventory["rollback_directories"]["count"], 5)
        self.assertEqual(inventory["rollback_directories"]["cleanup_candidates"], 2)
        self.assertEqual(
            inventory["rollback_directories"]["cleanup_names"],
            [names[2], names[0]],
        )
        self.assertTrue(all(path.exists() for path in paths))

        result = cleanup_managed_storage(
            self.root, exports,
            settings={"rollback_keep_count": 2, "rollback_keep_days": 7},
            now=self._at("2026-08-09 12:00:00"),
        )
        self.assertTrue(paths[1].exists())  # newest daily checkpoint for Aug 7
        self.assertTrue(paths[3].exists() and paths[4].exists())  # newest two
        self.assertFalse(paths[0].exists() or paths[2].exists())
        self.assertEqual(result["rollback_directories"]["removed"], 2)
        self.assertEqual(result["rollback_directories"]["retained_latest"], 2)
        self.assertEqual(result["rollback_directories"]["retained_daily"], 1)
        self.assertEqual(
            set(result["rollback_directories"]["removed_reasons"].values()),
            {"同日已有更新回退版本"},
        )

    def test_rollback_with_database_and_unknown_directory_are_protected(self):
        protected = self._rollback(
            "predeploy-v4.4.0-alpha.1-20260801-010101", database=True,
        )
        unknown = self.root / "old-exe-folder"
        unknown.mkdir(); (unknown / "板块雷达.exe").write_bytes(b"keep")
        self._old(unknown / "板块雷达.exe"); self._old(unknown)
        exports = Path(self.temp.name) / "data" / "exports"
        cleanup_managed_storage(
            self.root, exports,
            settings={"rollback_keep_count": 2, "rollback_keep_days": 7},
            now=20 * 86400,
        )
        self.assertTrue(protected.exists())
        self.assertTrue(unknown.exists())

    def test_program_root_rollbacks_are_included_without_double_counting(self):
        data = Path(self.temp.name) / "data"
        backups = data / "backups"
        exports = data / "exports"
        program = Path(self.temp.name) / "SectorRadar"
        program_rollback = self._rollback_in(
            program, "predeploy-v4.4.0-alpha.8-20260808-010101",
        )
        runtime_rollback = self._rollback(
            "predeploy-v4.4.0-alpha.9-20260809-010101",
        )

        inventory = storage_inventory(
            self.root, backups, exports, program_root=program,
        )
        same_root_inventory = storage_inventory(
            self.root, backups, exports, program_root=self.root,
        )

        self.assertEqual(inventory["rollback_directories"]["count"], 2)
        self.assertEqual(same_root_inventory["rollback_directories"]["count"], 1)
        self.assertTrue(program_rollback.exists() and runtime_rollback.exists())

    def test_program_root_cleanup_uses_one_global_policy_and_protects_data(self):
        program = Path(self.temp.name) / "SectorRadar"
        program.mkdir()
        data = program / "data"
        data.mkdir()
        live_database = data / "sector_radar.sqlite3"
        live_database.write_bytes(b"live user data")
        current_internal = program / "_internal"
        current_internal.mkdir()
        (current_internal / "python.dll").write_bytes(b"current")
        current_exe = program / "板块雷达.exe"
        current_exe.write_bytes(b"current exe")
        exports = data / "exports"
        rollbacks = [
            self._rollback_in(
                program,
                f"predeploy-v4.4.0-alpha.{number}-2026080{number}-010101",
            )
            for number in range(1, 4)
        ]
        runtime_rollback = self._rollback(
            "predeploy-v4.4.0-alpha.4-20260804-010101",
        )
        for index, path in enumerate((*rollbacks, runtime_rollback)):
            stamp = 100.0 + index
            os.utime(path, (stamp, stamp))

        result = cleanup_managed_storage(
            self.root, exports, program_root=program,
            settings={"rollback_keep_count": 2, "rollback_keep_days": 7},
            now=self._at("2026-08-20 12:00:00"),
        )

        self.assertFalse(rollbacks[0].exists())
        self.assertFalse(rollbacks[1].exists())
        self.assertTrue(rollbacks[2].exists())
        self.assertTrue(runtime_rollback.exists())
        self.assertEqual(result["rollback_directories"]["removed"], 2)
        self.assertEqual(live_database.read_bytes(), b"live user data")
        self.assertTrue(current_exe.exists() and current_internal.exists())

    def test_program_root_rollback_with_database_is_never_removed(self):
        program = Path(self.temp.name) / "SectorRadar"
        protected = self._rollback_in(
            program,
            "predeploy-v4.4.0-alpha.1-20260801-010101",
            database=True,
        )
        exports = Path(self.temp.name) / "data" / "exports"

        result = cleanup_managed_storage(
            self.root, exports, program_root=program,
            settings={"rollback_keep_count": 2, "rollback_keep_days": 7},
            now=20 * 86400,
        )

        self.assertTrue(protected.exists())
        self.assertEqual(result["rollback_directories"]["eligible"], 0)
        self.assertEqual(result["rollback_directories"]["protected"], 1)

    def test_only_old_healthy_migration_copy_is_pruned(self):
        oldest = self._migration("sector-radar-v34-migration-20260801-010101")
        newest = self._migration("sector-radar-v34-migration-20260802-010101")
        corrupt = self._migration(
            "sector-radar-v34-migration-20260731-010101", corrupt=True,
        )
        os.utime(oldest / "migration-copy.sqlite3", (100.0, 100.0))
        os.utime(newest / "migration-copy.sqlite3", (200.0, 200.0))
        os.utime(oldest, (100.0, 100.0)); os.utime(newest, (200.0, 200.0))
        exports = Path(self.temp.name) / "data" / "exports"
        result = cleanup_managed_storage(self.root, exports, now=20 * 86400)
        self.assertFalse(oldest.exists())
        self.assertTrue(newest.exists())
        self.assertTrue(corrupt.exists())
        self.assertEqual(result["migration_directories"]["removed"], 1)

    def test_migration_inventory_is_non_mutating_and_uses_database_age(self):
        migration = self._migration(
            "sector-radar-v34-migration-20260801-010101"
        )
        database = migration / "migration-copy.sqlite3"
        # The directory can look recent after an unrelated metadata event;
        # cleanup must use the immutable snapshot's own age.
        os.utime(database, (100.0, 100.0))
        os.utime(migration, (999999.0, 999999.0))
        backups = Path(self.temp.name) / "data" / "backups"
        exports = Path(self.temp.name) / "data" / "exports"
        before_names = sorted(path.name for path in migration.iterdir())
        before_database_mtime = database.stat().st_mtime

        inventory = storage_inventory(self.root, backups, exports)

        self.assertEqual(inventory["migration_directories"]["count"], 1)
        self.assertEqual(sorted(path.name for path in migration.iterdir()), before_names)
        self.assertEqual(database.stat().st_mtime, before_database_mtime)
        self.assertFalse((migration / "migration-copy.sqlite3-wal").exists())
        self.assertFalse((migration / "migration-copy.sqlite3-shm").exists())

        newer = self._migration(
            "sector-radar-v34-migration-20260802-010101"
        )
        os.utime(newer / "migration-copy.sqlite3", (200.0, 200.0))
        os.utime(newer, (200.0, 200.0))
        result = cleanup_managed_storage(
            self.root, exports, now=20 * 86400,
        )
        self.assertFalse(migration.exists())
        self.assertTrue(newer.exists())
        self.assertEqual(result["migration_directories"]["removed"], 1)

    def test_only_valid_old_diagnostics_are_pruned(self):
        exports = Path(self.temp.name) / "data" / "exports"
        exports.mkdir(parents=True)
        diagnostics = [
            self._diagnostic(exports / f"API任务诊断_2026080{number}-010101-task-{number}.json")
            for number in range(1, 7)
        ]
        for index, path in enumerate(diagnostics):
            stamp = 100.0 + index
            os.utime(path, (stamp, stamp))
        ordinary = self._diagnostic(
            exports / "API任务诊断_20260701-010101-user.json", valid=False,
        )
        picture = exports / "研究截图.png"; picture.write_bytes(b"keep"); self._old(picture)
        result = cleanup_managed_storage(
            self.root, exports,
            settings={"diagnostic_keep_count": 5, "diagnostic_keep_days": 30},
            now=200 * 86400,
        )
        self.assertFalse(diagnostics[0].exists())
        self.assertTrue(all(path.exists() for path in diagnostics[1:]))
        self.assertTrue(ordinary.exists())
        self.assertTrue(picture.exists())
        self.assertEqual(result["diagnostic_exports"]["removed"], 1)

    def test_inventory_distinguishes_managed_from_protected_files(self):
        backups = Path(self.temp.name) / "data" / "backups"
        exports = Path(self.temp.name) / "data" / "exports"
        backups.mkdir(parents=True); exports.mkdir(parents=True)
        automatic = backups / "板块雷达备份_20260813_010101_123456.sqlite3"
        connection = sqlite3.connect(automatic)
        for table in ("settings", "funds", "holdings", "candidates"):
            connection.execute(f"CREATE TABLE {table}(value TEXT)")
        connection.commit(); connection.close()
        manual = backups / "我的完整备份.sqlite3"; manual.write_bytes(b"manual")
        diagnostic = self._diagnostic(
            exports / "API任务诊断_20260813-010101-task.json",
        )
        csv_file = exports / "我的交易.csv"; csv_file.write_bytes(b"csv")
        rollback = self._rollback("predeploy-v4.4.0-alpha.1-20260801-010101")
        migration = self._migration("sector-radar-v34-migration-20260801-010101")
        inventory = storage_inventory(self.root, backups, exports)
        self.assertEqual(inventory["automatic_backups"]["count"], 1)
        self.assertEqual(inventory["protected_backups"]["count"], 1)
        self.assertEqual(inventory["rollback_directories"]["count"], 1)
        self.assertEqual(inventory["migration_directories"]["count"], 1)
        self.assertEqual(inventory["diagnostic_exports"]["count"], 1)
        self.assertEqual(inventory["protected_exports"]["count"], 1)
        self.assertTrue(diagnostic.exists() and csv_file.exists())
        self.assertTrue(rollback.exists() and migration.exists())

    def test_inventory_validates_each_rollback_only_once(self):
        backups = Path(self.temp.name) / "data" / "backups"
        exports = Path(self.temp.name) / "data" / "exports"
        rollback = self._rollback(
            "predeploy-v4.4.0-alpha.1-20260801-010101"
        )

        with mock.patch(
            "app.runtime_housekeeping._valid_rollback_directory",
            wraps=__import__(
                "app.runtime_housekeeping", fromlist=["_valid_rollback_directory"]
            )._valid_rollback_directory,
        ) as validator:
            inventory = storage_inventory(
                self.root,
                backups,
                exports,
                settings={"rollback_keep_count": 3, "rollback_keep_days": 30},
                now=self._at("2026-08-02 12:00:00"),
            )

        self.assertEqual(inventory["rollback_directories"]["count"], 1)
        self.assertEqual(validator.call_count, 1)
        self.assertTrue(rollback.exists())

    def test_automatic_backup_requires_exact_name_and_healthy_app_schema(self):
        backups = Path(self.temp.name) / "data" / "backups"
        backups.mkdir(parents=True)
        healthy = backups / "板块雷达备份_20260813_010101_123456.sqlite3"
        connection = sqlite3.connect(healthy)
        for table in ("settings", "funds", "holdings", "candidates"):
            connection.execute(f"CREATE TABLE {table}(value TEXT)")
        connection.commit(); connection.close()
        old_format = backups / "板块雷达备份_20260813_010101.sqlite3"
        old_format.write_bytes(healthy.read_bytes())
        fake = backups / "板块雷达备份_20260813_010102_123456.sqlite3"
        fake.write_bytes(b"not a database")

        self.assertTrue(valid_automatic_backup_file(healthy, backups))
        self.assertFalse(valid_automatic_backup_file(old_format, backups))
        self.assertFalse(valid_automatic_backup_file(fake, backups))

    def test_automatic_backup_validation_and_planning_are_cancellable(self):
        backups = Path(self.temp.name) / "data" / "backups"
        backups.mkdir(parents=True)
        healthy = backups / "板块雷达备份_20260813_010101_123456.sqlite3"
        connection = sqlite3.connect(healthy)
        for table in ("settings", "funds", "holdings", "candidates"):
            connection.execute(f"CREATE TABLE {table}(value TEXT)")
        connection.commit(); connection.close()

        self.assertFalse(
            valid_automatic_backup_file(healthy, backups, lambda: True),
        )
        plan = automatic_backup_retention_plan(
            backups,
            keep_count=3,
            keep_days=30,
            stop_requested=lambda: True,
        )
        self.assertTrue(plan["stopped"])
        self.assertEqual(plan["removable"], [])
        result = prune_automatic_backup_sidecars(backups, lambda: True)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["removed"], 0)

    def test_automatic_backup_with_nonempty_wal_is_protected(self):
        backups = Path(self.temp.name) / "data" / "backups"
        backups.mkdir(parents=True)
        healthy = backups / "板块雷达备份_20260813_010101_123456.sqlite3"
        connection = sqlite3.connect(healthy)
        for table in ("settings", "funds", "holdings", "candidates"):
            connection.execute(f"CREATE TABLE {table}(value TEXT)")
        connection.commit(); connection.close()
        wal = Path(f"{healthy}-wal")
        shm = Path(f"{healthy}-shm")
        wal.write_bytes(b"")
        shm.write_bytes(b"rebuildable-index")
        self.assertTrue(valid_automatic_backup_file(healthy, backups))
        wal.write_bytes(b"uncheckpointed-pages")
        self.assertFalse(valid_automatic_backup_file(healthy, backups))
        plan = automatic_backup_sidecar_cleanup_plan(backups)
        self.assertEqual(plan["removable"], [])
        self.assertEqual(plan["protected"], 2)

    def test_zero_automatic_sidecars_are_cleaned_but_manual_ones_are_protected(self):
        backups = Path(self.temp.name) / "data" / "backups"
        backups.mkdir(parents=True)
        healthy = backups / "板块雷达备份_20260813_010101_123456.sqlite3"
        connection = sqlite3.connect(healthy)
        for table in ("settings", "funds", "holdings", "candidates"):
            connection.execute(f"CREATE TABLE {table}(value TEXT)")
        connection.commit(); connection.close()
        automatic_sidecars = [Path(f"{healthy}-wal"), Path(f"{healthy}-shm")]
        automatic_sidecars[0].write_bytes(b"")
        automatic_sidecars[1].write_bytes(b"rebuildable-index")
        orphan_base = backups / "板块雷达备份_20260812_010101_654321.sqlite3"
        orphan_sidecars = [Path(f"{orphan_base}-wal"), Path(f"{orphan_base}-shm")]
        orphan_sidecars[0].write_bytes(b"")
        orphan_sidecars[1].write_bytes(b"old-index")
        manual = backups / "我的备份.sqlite3"
        manual.write_bytes(b"manual")
        manual_wal = Path(f"{manual}-wal"); manual_wal.write_bytes(b"")

        inventory = storage_inventory(self.root, backups, backups / "exports")
        self.assertEqual(inventory["automatic_backups"]["count"], 1)
        self.assertEqual(inventory["automatic_backups"]["sidecar_cleanup_candidates"], 4)
        self.assertEqual(inventory["protected_backups"]["count"], 2)
        result = prune_automatic_backup_sidecars(backups)
        self.assertEqual(result["removed"], 4)
        self.assertGreater(result["bytes_freed"], 0)
        self.assertTrue(healthy.exists() and manual.exists() and manual_wal.exists())
        self.assertTrue(all(not path.exists() for path in (*automatic_sidecars, *orphan_sidecars)))

    def test_automatic_backup_burst_keeps_latest_and_one_per_other_day(self):
        backups = Path(self.temp.name) / "data" / "backups"
        backups.mkdir(parents=True)
        names = [
            "板块雷达备份_20260807_080000_000001.sqlite3",
            "板块雷达备份_20260807_090000_000002.sqlite3",
            "板块雷达备份_20260808_080000_000003.sqlite3",
            "板块雷达备份_20260808_090000_000004.sqlite3",
            "板块雷达备份_20260808_100000_000005.sqlite3",
        ]
        paths = []
        for name in names:
            path = backups / name
            connection = sqlite3.connect(path)
            for table in ("settings", "funds", "holdings", "candidates"):
                connection.execute(f"CREATE TABLE {table}(value TEXT)")
            connection.commit(); connection.close()
            paths.append(path)

        plan = automatic_backup_retention_plan(
            backups,
            keep_count=2,
            keep_days=7,
            now=self._at("2026-08-09 12:00:00"),
        )

        self.assertEqual(len(plan["valid"]), 5)
        self.assertEqual([path.name for path in plan["retained_latest"]], names[4:2:-1])
        self.assertEqual([path.name for path in plan["retained_daily"]], [names[1]])
        self.assertEqual(
            [path.name for path, _reason in plan["removable"]],
            [names[2], names[0]],
        )
        self.assertTrue(all(path.exists() for path in paths))

    def test_deployment_backup_requires_exact_known_name_and_healthy_schema(self):
        backups = Path(self.temp.name) / "data" / "backups"
        backups.mkdir(parents=True)
        names = [
            "predeploy-v4.4.0-alpha.14-20260814-122743.sqlite3",
            "sector_radar_predeploy_4.4.0-alpha.35_20260817_014137_810.sqlite3",
            "sector_radar-predeploy-v4.4.0-alpha.38-20260817-184746-758.sqlite3",
        ]
        paths = []
        for name in names:
            path = backups / name
            connection = sqlite3.connect(path)
            for table in ("settings", "funds", "holdings", "candidates"):
                connection.execute(f"CREATE TABLE {table}(value TEXT)")
            connection.commit(); connection.close()
            paths.append(path)
        near_match = backups / "my-sector_radar-predeploy-v4.4.0-alpha.38-20260817-184746-758.sqlite3"
        near_match.write_bytes(paths[-1].read_bytes())
        corrupt = backups / "sector_radar-predeploy-v4.4.0-alpha.39-20260817-190000-123.sqlite3"
        corrupt.write_bytes(b"not sqlite")

        self.assertTrue(all(valid_deployment_backup_file(path, backups) for path in paths))
        self.assertFalse(valid_deployment_backup_file(near_match, backups))
        self.assertFalse(valid_deployment_backup_file(corrupt, backups))

    def test_deployment_retention_inventory_and_sidecars_are_separate(self):
        backups = Path(self.temp.name) / "data" / "backups"
        exports = Path(self.temp.name) / "data" / "exports"
        backups.mkdir(parents=True); exports.mkdir(parents=True)
        names = [
            "sector_radar_predeploy_4.4.0-alpha.31_20260816_205805_573.sqlite3",
            "sector_radar_predeploy_4.4.0-alpha.32_20260816_221448_650.sqlite3",
            "sector_radar-predeploy-v4.4.0-alpha.37-20260817-182808-609.sqlite3",
            "sector_radar-predeploy-v4.4.0-alpha.38-20260817-184746-758.sqlite3",
        ]
        paths = []
        for name in names:
            path = backups / name
            connection = sqlite3.connect(path)
            for table in ("settings", "funds", "holdings", "candidates"):
                connection.execute(f"CREATE TABLE {table}(value TEXT)")
            connection.commit(); connection.close()
            paths.append(path)
        wal = Path(f"{paths[1]}-wal"); wal.write_bytes(b"")
        shm = Path(f"{paths[1]}-shm"); shm.write_bytes(b"rebuildable-index")
        manual = backups / "我手工命名的predeploy备份.sqlite3"
        manual.write_bytes(paths[-1].read_bytes())

        plan = deployment_backup_retention_plan(
            backups,
            keep_count=2,
            keep_days=30,
            now=self._at("2026-08-18 12:00:00"),
        )
        self.assertEqual([path.name for path in plan["retained_latest"]], names[3:1:-1])
        self.assertEqual([path.name for path in plan["retained_daily"]], [names[1]])
        self.assertEqual([path.name for path, _reason in plan["removable"]], [names[0]])
        inventory = storage_inventory(
            self.root,
            backups,
            exports,
            settings={
                "deployment_backup_keep_count": 2,
                "deployment_backup_keep_days": 30,
            },
            now=self._at("2026-08-18 12:00:00"),
        )
        self.assertEqual(inventory["deployment_backups"]["count"], 4)
        self.assertEqual(inventory["deployment_backups"]["cleanup_candidates"], 1)
        self.assertEqual(inventory["deployment_backups"]["sidecar_cleanup_candidates"], 2)
        self.assertEqual(inventory["protected_backups"]["count"], 1)
        self.assertTrue(all(path.exists() for path in paths))
        self.assertTrue(wal.exists() and shm.exists() and manual.exists())

    def test_deployment_nonempty_wal_and_near_match_sidecars_are_protected(self):
        backups = Path(self.temp.name) / "data" / "backups"
        backups.mkdir(parents=True)
        database = backups / "sector_radar-predeploy-v4.4.0-alpha.38-20260817-184746-758.sqlite3"
        connection = sqlite3.connect(database)
        for table in ("settings", "funds", "holdings", "candidates"):
            connection.execute(f"CREATE TABLE {table}(value TEXT)")
        connection.commit(); connection.close()
        wal = Path(f"{database}-wal"); wal.write_bytes(b"uncheckpointed-pages")
        shm = Path(f"{database}-shm"); shm.write_bytes(b"index")
        near_match = backups / "sector_radar-predeploy-v4.4.0-alpha.38-20260817-184746.sqlite3-wal"
        near_match.write_bytes(b"")

        self.assertFalse(valid_deployment_backup_file(database, backups))
        plan = deployment_backup_sidecar_cleanup_plan(backups)
        self.assertEqual(plan["removable"], [])
        self.assertEqual(plan["protected"], 2)
        result = prune_deployment_backup_sidecars(backups)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(wal.exists() and shm.exists() and near_match.exists())

    def test_managed_cleanup_never_touches_tasks_or_backups(self):
        data = Path(self.temp.name) / "data"
        tasks = data / "tasks"; backups = data / "backups"
        exports = data / "exports"
        tasks.mkdir(parents=True); backups.mkdir(parents=True)
        task = tasks / "网页版研究任务.md"; task.write_text("important", encoding="utf-8")
        backup = backups / "manual.sqlite3"; backup.write_bytes(b"important")
        cleanup_managed_storage(self.root, exports, now=999 * 86400)
        self.assertTrue(task.exists())
        self.assertTrue(backup.exists())

    def test_managed_cleanup_cancellation_preserves_remaining_targets(self):
        rollback = self._rollback(
            "predeploy-v4.4.0-alpha.1-20260801-010101",
        )
        exports = Path(self.temp.name) / "data" / "exports"
        result = cleanup_managed_storage(
            self.root,
            exports,
            now=999 * 86400,
            stop_requested=lambda: True,
        )
        self.assertTrue(result["stopped"])
        self.assertEqual(result["removed"], 0)
        self.assertTrue(rollback.exists())


if __name__ == "__main__":
    unittest.main()
