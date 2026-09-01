from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import local_now
from app.database import Database
from app.fund_discovery import FundDiscoveryService
from app.platform_catalog import PlatformCatalogService


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def scalar(db: Database, sql: str) -> int:
    row = db.row(sql)
    return int(next(iter(row.values())) if row else 0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the zero-DeepSeek same-day fund execution refresh on a temporary DB copy.",
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    formal = args.database.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    before_hash = sha256(formal)
    started = time.perf_counter()
    report: dict[str, object] = {
        "status": "FAILED",
        "started_at": local_now().isoformat(timespec="seconds"),
        "formal_database": str(formal),
        "formal_hash_before": before_hash,
        "requested_limit": max(0, args.limit),
        "requested_workers": args.workers,
        "deepseek_api_calls": 0,
    }

    try:
        with tempfile.TemporaryDirectory(prefix="sector-radar-execution-") as folder:
            temporary_db = Path(folder) / "execution.sqlite3"
            shutil.copy2(formal, temporary_db)
            db = Database(temporary_db)
            db.save_settings({"fund_execution_refresh_workers": str(args.workers)})
            service = PlatformCatalogService(db)
            universe = service.radar_universe(refresh_admission=False)
            if args.limit > 0:
                universe = universe[: args.limit]
            selected_ids = [int(row["platform_index_id"]) for row in universe]
            service.radar_universe = lambda *, refresh_admission=True: list(universe)  # type: ignore[method-assign]
            before_calls = scalar(db, "SELECT COUNT(*) FROM model_calls")
            discovery = FundDiscoveryService(db)

            def progress(done: int, total: int, label: str) -> None:
                print(f"[{done:03d}/{total:03d}] {label}", flush=True)

            result = service.revalidate_radar_execution(
                checker=lambda fund: discovery.public_execution_snapshot(fund["fund_code"]),
                progress=progress,
            )
            after_calls = scalar(db, "SELECT COUNT(*) FROM model_calls")
            strict_rows = service.strict_catalog_funds_by_index(selected_ids)
            report.update({
                "catalog_total_before": len(universe),
                "strict_fund_rows": sum(len(rows) for rows in strict_rows.values()),
                "result": result,
                "deepseek_api_calls": after_calls - before_calls,
                "temporary_integrity_check": next(iter(db.row("PRAGMA integrity_check").values())),
            })
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"

    after_hash = sha256(formal)
    result = report.get("result") if isinstance(report.get("result"), dict) else {}
    report.update({
        "finished_at": local_now().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "formal_hash_after": after_hash,
        "formal_unchanged": after_hash == before_hash,
    })
    if (
        result
        and int(result.get("ready") or 0) == int(result.get("total") or -1)
        and not result.get("stopped")
        and int(report.get("deepseek_api_calls") or 0) == 0
        and report["formal_unchanged"]
        and report.get("temporary_integrity_check") == "ok"
    ):
        report["status"] = "PASSED"
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
