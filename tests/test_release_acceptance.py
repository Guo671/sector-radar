from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.release_acceptance import REPORT_SCHEMA, run_release_acceptance


class ReleaseAcceptanceTests(unittest.TestCase):
    def test_complete_flow_is_offline_disposable_and_does_not_touch_formal_db(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime"
            formal = root / "formal.sqlite3"
            connection = sqlite3.connect(formal)
            connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES('do-not-change')")
            connection.commit()
            connection.close()
            original = formal.read_bytes()
            report_path = root / "acceptance.json"

            # Any accidental urllib/socket network fallback must fail this test.
            with patch(
                "socket.create_connection",
                side_effect=AssertionError("release acceptance attempted network access"),
            ):
                report = run_release_acceptance(
                    report_path=report_path,
                    runtime_dir=runtime,
                    formal_db_path=formal,
                )

            self.assertEqual(report["schema"], REPORT_SCHEMA)
            self.assertEqual(report["status"], "PASSED")
            self.assertEqual(report["network_calls"], 0)
            self.assertTrue(report["formal_database_unchanged"])
            self.assertTrue(report["temporary_database_deleted"])
            self.assertEqual(formal.read_bytes(), original)
            self.assertEqual(list(runtime.iterdir()), [])
            self.assertEqual(report["isolated_result"]["holding_units"], 300)
            self.assertEqual(report["isolated_result"]["holding_cost"], 300)
            self.assertEqual(report["isolated_result"]["active_cash"], 7720)
            self.assertEqual(report["isolated_result"]["realized_pnl"], 20)
            self.assertEqual(report["isolated_result"]["open_review_segments"], 1)
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "PASSED")
            self.assertTrue(all(item["passed"] for item in saved["checks"]))


if __name__ == "__main__":
    unittest.main()
