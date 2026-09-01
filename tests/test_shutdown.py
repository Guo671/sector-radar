from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.database import Database
from app.ui import PAGE_GUIDES, SectorRadarApp


@unittest.skipUnless(sys.platform.startswith("win"), "Windows desktop behavior")
class OneClickShutdownTests(unittest.TestCase):
    def test_lazy_page_construction_keeps_hidden_pages_on_demand(self):
        """Normal launches must not pay the construction cost of 13 hidden pages."""
        with tempfile.TemporaryDirectory(prefix="sector-radar-lazy-ui-") as raw:
            database = Database(Path(raw) / "test.sqlite3")
            app = SectorRadarApp(
                start_hidden=True, database=database, eager_pages=False,
            )
            try:
                self.assertEqual(set(app.page_classes), set(PAGE_GUIDES))
                self.assertEqual(set(app.pages), {"decision"})
                decision = app.pages["decision"]

                app.show("review")
                self.assertEqual(set(app.pages), {"decision", "review"})
                review = app.pages["review"]

                app.show("decision")
                self.assertIs(app.pages["decision"], decision)
                app.show("review")
                self.assertIs(app.pages["review"], review)
                self.assertEqual(set(app.pages), {"decision", "review"})
            finally:
                app.destroy()

    def test_direct_destroy_cancels_dashboard_and_navigation_callbacks(self):
        """A disposed root must not leave Tcl ``after`` scripts for the next root."""
        with tempfile.TemporaryDirectory(prefix="sector-radar-after-cleanup-") as raw:
            database = Database(Path(raw) / "test.sqlite3")
            app = SectorRadarApp(
                start_hidden=True, database=database, eager_pages=False,
            )
            decision = app.pages["decision"]
            decision.refresh()
            idle_token = decision._idle_name_after_id
            navigation_token = app.after_idle(lambda: None)
            pending = set(app.tk.splitlist(app.tk.call("after", "info")))
            self.assertIn(idle_token, pending)
            self.assertIn(navigation_token, pending)

            cancelled = []
            original_after_cancel = app.after_cancel
            original_page_after_cancel = decision.after_cancel

            def tracked_after_cancel(token):
                cancelled.append(token)
                return original_after_cancel(token)

            def tracked_page_after_cancel(token):
                cancelled.append(token)
                return original_page_after_cancel(token)

            app.after_cancel = tracked_after_cancel  # type: ignore[method-assign]
            decision.after_cancel = tracked_page_after_cancel  # type: ignore[method-assign]
            app.destroy()

            self.assertIn(idle_token, cancelled)
            self.assertIn(navigation_token, cancelled)

    def test_one_close_waits_for_worker_then_backs_up_and_exits(self):
        """One WM close must finish the whole safe-shutdown sequence."""
        with tempfile.TemporaryDirectory(prefix="sector-radar-close-test-") as raw:
            database = Database(Path(raw) / "test.sqlite3")
            backup_called = threading.Event()
            backup_options = {}
            cancelled = threading.Event()
            worker_finished = threading.Event()

            def fake_backup(*, prune_automatic=True):
                backup_options["prune_automatic"] = prune_automatic
                backup_called.set()
                return Path(raw) / "fake-backup.sqlite3"

            database.backup = fake_backup  # type: ignore[method-assign]
            app = SectorRadarApp(start_hidden=True, database=database)

            def slow_worker():
                cancelled.wait(2.0)
                time.sleep(0.35)
                worker_finished.set()

            app.start_background(
                slow_worker, label="close-state-test", cancel=cancelled.set,
            )
            outcome = {"timeout": False}
            started = time.monotonic()

            def timeout_guard():
                outcome["timeout"] = True
                try:
                    app.destroy()
                except Exception:
                    pass

            # This is intentionally the only close request in the test.
            app.after(80, app.on_close)
            app.after(6000, timeout_guard)
            app.mainloop()

            self.assertFalse(outcome["timeout"])
            self.assertTrue(cancelled.is_set())
            self.assertTrue(worker_finished.is_set())
            self.assertTrue(backup_called.is_set())
            self.assertIs(backup_options["prune_automatic"], False)
            self.assertGreaterEqual(time.monotonic() - started, 0.35)

    def test_one_close_cancels_startup_storage_maintenance(self):
        """Mechanical-disk maintenance must not hold the window open."""
        with tempfile.TemporaryDirectory(prefix="sector-radar-cleanup-close-") as raw:
            database = Database(Path(raw) / "test.sqlite3")
            maintenance_started = threading.Event()
            maintenance_stopped = threading.Event()
            backup_called = threading.Event()

            def cancellable_prune(stop_requested=None):
                maintenance_started.set()
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline:
                    if stop_requested is not None and stop_requested():
                        maintenance_stopped.set()
                        return {"stopped": True}
                    time.sleep(0.01)
                return {"stopped": False}

            def fake_backup(*, prune_automatic=True):
                backup_called.set()
                return Path(raw) / "fake-backup.sqlite3"

            database.prune_backups = cancellable_prune  # type: ignore[method-assign]
            database.backup = fake_backup  # type: ignore[method-assign]
            app = SectorRadarApp(start_hidden=True, database=database)
            app._auto_cleanup_runtime()
            outcome = {"timeout": False}

            def request_close_when_started():
                if maintenance_started.is_set():
                    app.on_close()
                else:
                    app.after(20, request_close_when_started)

            def timeout_guard():
                outcome["timeout"] = True
                try:
                    app.destroy()
                except Exception:
                    pass

            app.after(20, request_close_when_started)
            app.after(5000, timeout_guard)
            app.mainloop()

            self.assertFalse(outcome["timeout"])
            self.assertTrue(maintenance_started.is_set())
            self.assertTrue(maintenance_stopped.is_set())
            self.assertTrue(backup_called.is_set())


if __name__ == "__main__":
    unittest.main()
