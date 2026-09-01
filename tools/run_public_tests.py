"""Run the public regression suite without polluting production bootstrap."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import Database  # noqa: E402
from tests.public_fixtures import seed_legacy_regression_portfolio  # noqa: E402


def verify_clean_public_bootstrap() -> None:
    with tempfile.TemporaryDirectory() as folder:
        database = Database(Path(folder) / "fresh-public.sqlite3")
        for table in (
            "holdings", "trades", "cost_lots", "ledger_postings",
            "recurring_plans", "recurring_occurrences", "decision_drafts",
        ):
            count = int(database.row(f"SELECT COUNT(*) AS n FROM {table}")["n"])
            if count != 0:
                raise AssertionError(f"public bootstrap populated {table}: {count}")
        if database.setting("deepseek_api_key_dpapi", ""):
            raise AssertionError("public bootstrap populated an API key")
    print("PUBLIC_BOOTSTRAP_OK holdings=0 trades=0 recurring=0 api_key=empty")


def main() -> int:
    verify_clean_public_bootstrap()
    Database._seed = seed_legacy_regression_portfolio
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
