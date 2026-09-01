from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.database import Database
from app.platform_catalog import PlatformCatalogService
from app.services import latest_current_radar_run, research_run_status
from app.ui import ReviewPage, SectorRadarApp


class PerformanceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name) / "performance.sqlite3")

    def tearDown(self):
        self.folder.cleanup()

    def test_short_connection_does_not_renegotiate_wal(self):
        """WAL negotiation is one-time startup work, never a UI-read cost."""
        fake_connection = Mock()
        target = object.__new__(Database)
        target.path = Path(self.folder.name) / "fake.sqlite3"
        with patch("app.database.sqlite3.connect", return_value=fake_connection):
            with target.connect() as opened:
                self.assertIs(opened, fake_connection)
        sql = [str(call.args[0]).lower() for call in fake_connection.execute.call_args_list]
        self.assertFalse(any("journal_mode" in statement for statement in sql))
        fake_connection.close.assert_called_once_with()

    def test_read_only_radar_snapshot_is_bulk_and_does_not_refresh_admission(self):
        catalog = PlatformCatalogService(self.db)
        with (
            patch.object(catalog, "refresh_admission") as refresh,
            patch.object(self.db, "rows", wraps=self.db.rows) as rows,
        ):
            catalog.radar_universe(refresh_admission=False)
        refresh.assert_not_called()
        # One query for indices and at most one for all fund rows.
        self.assertLessEqual(rows.call_count, 2)

    def test_current_research_check_has_bounded_database_round_trips(self):
        with (
            patch.object(self.db, "rows", wraps=self.db.rows) as rows,
            patch.object(self.db, "row", wraps=self.db.row) as row,
            patch.object(self.db, "setting", wraps=self.db.setting) as setting,
        ):
            latest_current_radar_run(self.db)
        self.assertLessEqual(rows.call_count + row.call_count + setting.call_count, 8)

    def test_research_status_reuses_one_compatibility_snapshot(self):
        with (
            patch.object(self.db, "rows", wraps=self.db.rows) as rows,
            patch.object(self.db, "row", wraps=self.db.row) as row,
            patch.object(self.db, "setting", wraps=self.db.setting) as setting,
        ):
            research_run_status(self.db)
        # The dashboard used to rebuild the catalog/fund hash twice by calling
        # current and readable lookups independently. Keep one shared snapshot.
        self.assertLessEqual(rows.call_count + row.call_count + setting.call_count, 10)

    def test_refresh_all_only_refreshes_visible_page(self):
        visible = Mock()
        hidden = Mock()
        app = SimpleNamespace(
            pages={"visible": visible, "hidden": hidden},
            _current_page_key="visible",
        )
        SectorRadarApp.refresh_all(app)
        visible.refresh.assert_called_once_with()
        hidden.refresh.assert_not_called()

    def test_recommendation_local_maintenance_starts_service_only_in_worker(self):
        page = object.__new__(ReviewPage)
        page._recommendation_sync_active = False
        page._recommendation_local_active = False
        page.recommendation_status = Mock()
        page._set_recommendation_task_mode = Mock()
        page.after = lambda _delay, callback: callback()
        page._finish_snapshot_recovery = Mock()
        page._finish_cached_recommendation_refresh = Mock()
        page.db = self.db
        page.app = SimpleNamespace(start_background=Mock())

        with (
            patch("app.ui.messagebox.askyesno", return_value=True),
            patch("app.ui.RecommendationPerformanceService") as service_class,
        ):
            service_class.return_value.recover_verified_v3_snapshots.return_value = {"tasks_checked": 0}
            page.recover_recommendation_snapshots()
            service_class.assert_not_called()
            worker = page.app.start_background.call_args.args[0]
            worker()
            service_class.return_value.recover_verified_v3_snapshots.assert_called_once_with()
            page._finish_snapshot_recovery.assert_called_once_with({"tasks_checked": 0}, None)

        page._recommendation_local_active = False
        page.app.start_background.reset_mock()
        with patch("app.ui.RecommendationPerformanceService") as service_class:
            service_class.return_value.refresh_cached.return_value = {"total": 0}
            page.refresh_recommendation_cached()
            service_class.assert_not_called()
            worker = page.app.start_background.call_args.args[0]
            worker()
            service_class.return_value.refresh_cached.assert_called_once_with()
            page._finish_cached_recommendation_refresh.assert_called_once_with({"total": 0}, None)


if __name__ == "__main__":
    unittest.main()
