from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from app.database import Database  # noqa: E402
from app.orchestrator import FULL_MARKET_MODE, ResearchOrchestrator  # noqa: E402
from app.platform_catalog import PlatformCatalogService  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _database_snapshot(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size": 0,
        "sha256": "",
        "integrity": "missing",
    }
    if not path.is_file():
        return result
    result["size"] = path.stat().st_size
    result["sha256"] = _sha256(path)
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        result["integrity"] = str(
            connection.execute("PRAGMA integrity_check").fetchone()[0]
        )
    return result


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _e_drive_absent() -> bool:
    return not (Path("E:/SectorRadar").exists() or Path("E:/板块雷达").exists())


def validate(
    clone_database: Path,
    formal_database: Path,
    report_path: Path,
    expected_count: int,
) -> dict[str, Any]:
    clone = clone_database.resolve()
    formal = formal_database.resolve()
    report = report_path.resolve()
    runtime_root = (formal.parent / "runtime").resolve()
    started = datetime.now().astimezone()
    result: dict[str, Any] = {
        "status": "FAILED",
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": "",
        "clone_database": str(clone),
        "formal_database": str(formal),
        "report_path": str(report),
        "expected_count": expected_count,
        "task_id": "",
        "network_scope": "公开行情源；禁止DeepSeek调用",
        "formal_before": _database_snapshot(formal),
        "formal_after": {},
        "e_drive_absent_before": _e_drive_absent(),
        "e_drive_absent_after": False,
        "job": {},
        "metrics": {},
        "model_calls": 0,
        "checks": [],
        "progress": [],
        "error": "",
    }

    def check(name: str, passed: bool, detail: str) -> None:
        result["checks"].append(
            {"name": name, "passed": bool(passed), "detail": detail}
        )

    try:
        if clone == formal:
            raise ValueError("隔离数据库不能与正式数据库相同。")
        if clone.parent.parent != runtime_root or not clone.parent.name.startswith(
            "market-refresh-clone-"
        ):
            raise ValueError(
                f"隔离数据库必须位于受控运行目录：{runtime_root}\\market-refresh-clone-*"
            )
        if not clone.is_file():
            raise FileNotFoundError(f"找不到隔离数据库：{clone}")
        if result["formal_before"]["integrity"] != "ok":
            raise RuntimeError("正式数据库基线未通过完整性检查。")
        clone_before = _database_snapshot(clone)
        if clone_before["integrity"] != "ok":
            raise RuntimeError("隔离数据库未通过完整性检查。")

        db = Database(clone)
        catalog_count = len(PlatformCatalogService(db).radar_universe())
        check(
            "catalog_count_before_run",
            catalog_count == expected_count,
            f"当前可扫描目录{catalog_count}项，期望{expected_count}项",
        )
        if catalog_count != expected_count:
            raise RuntimeError(
                f"隔离副本目录数量为{catalog_count}，不是期望的{expected_count}。"
            )

        orchestrator = ResearchOrchestrator(db, client=None)
        task_id = orchestrator.create_job(FULL_MARKET_MODE)
        result["task_id"] = task_id

        def progress(stage: str, completed: int, total: int, message: str) -> None:
            event = {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "stage": stage,
                "completed": int(completed),
                "total": int(total),
                "message": message,
            }
            result["progress"].append(event)
            print(
                f"PROGRESS|{stage}|{completed}/{total}|{message}",
                flush=True,
            )

        outcome = orchestrator.run(task_id, progress=progress)
        job = db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (task_id,)) or {}
        metrics = db.rows(
            "SELECT * FROM sector_metrics WHERE task_id=? ORDER BY universe_id",
            (task_id,),
        )
        model_calls = int(
            (db.row("SELECT COUNT(*) AS n FROM model_calls WHERE task_id=?", (task_id,)) or {}).get(
                "n", 0
            )
        )
        result["job"] = {
            "mode": job.get("mode", ""),
            "stage": job.get("stage", ""),
            "status": job.get("status", ""),
            "total_items": int(job.get("total_items") or 0),
            "completed_items": int(job.get("completed_items") or 0),
            "estimated_cost_cny": float(job.get("estimated_cost_cny") or 0),
            "error": str(job.get("error") or ""),
            "result_message": outcome.message,
        }
        cutoffs = sorted(
            {str(row.get("data_cutoff") or "")[:10] for row in metrics}
        )
        expected_cutoffs = sorted(
            {str(row.get("expected_data_cutoff") or "")[:10] for row in metrics}
        )
        freshness_states = sorted(
            {str(row.get("freshness_state") or "") for row in metrics}
        )
        source_counts: dict[str, int] = {}
        for row in metrics:
            source = str(row.get("data_source") or "未知")
            source_counts[source] = source_counts.get(source, 0) + 1
        result["metrics"] = {
            "row_count": len(metrics),
            "distinct_universe_count": len(
                {str(row.get("universe_id") or "") for row in metrics}
            ),
            "data_cutoffs": cutoffs,
            "expected_data_cutoffs": expected_cutoffs,
            "freshness_states": freshness_states,
            "source_counts": source_counts,
            "cache_hit_count": sum(int(row.get("cache_hit") or 0) for row in metrics),
            "simulated_count": sum(int(row.get("is_simulated") or 0) for row in metrics),
        }
        result["model_calls"] = model_calls

        check(
            "job_completed",
            job.get("status") == "行情更新完成",
            f"状态={job.get('status') or ''}；阶段={job.get('stage') or ''}",
        )
        check(
            "all_catalog_items_completed",
            int(job.get("total_items") or 0) == expected_count
            and int(job.get("completed_items") or 0) == expected_count,
            f"{job.get('completed_items') or 0}/{job.get('total_items') or 0}",
        )
        check(
            "one_metric_per_catalog_item",
            len(metrics) == expected_count
            and result["metrics"]["distinct_universe_count"] == expected_count,
            f"rows={len(metrics)}；distinct={result['metrics']['distinct_universe_count']}",
        )
        check(
            "single_consistent_cutoff",
            len(cutoffs) == 1 and bool(cutoffs[0]),
            f"实际截止日={cutoffs}",
        )
        check(
            "single_expected_cutoff",
            len(expected_cutoffs) == 1 and bool(expected_cutoffs[0]),
            f"应截止日={expected_cutoffs}",
        )
        check(
            "cutoff_matches_expected",
            len(cutoffs) == 1
            and len(expected_cutoffs) == 1
            and cutoffs[0] == expected_cutoffs[0],
            f"实际截止日={cutoffs}；应截止日={expected_cutoffs}",
        )
        check(
            "no_simulated_market_data",
            result["metrics"]["simulated_count"] == 0,
            f"模拟行情={result['metrics']['simulated_count']}项",
        )
        check(
            "zero_deepseek_calls",
            model_calls == 0,
            f"model_calls={model_calls}",
        )
        check(
            "zero_model_cost",
            float(job.get("estimated_cost_cny") or 0) == 0.0,
            f"estimated_cost_cny={float(job.get('estimated_cost_cny') or 0):.6f}",
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["formal_after"] = _database_snapshot(formal)
        result["e_drive_absent_after"] = _e_drive_absent()
        formal_unchanged = (
            result["formal_before"].get("sha256")
            == result["formal_after"].get("sha256")
            and result["formal_after"].get("integrity") == "ok"
        )
        check(
            "formal_database_unchanged",
            formal_unchanged,
            (
                f"before={result['formal_before'].get('sha256', '')}；"
                f"after={result['formal_after'].get('sha256', '')}；"
                f"integrity={result['formal_after'].get('integrity', '')}"
            ),
        )
        check(
            "e_drive_untouched",
            bool(result["e_drive_absent_before"] and result["e_drive_absent_after"]),
            "E盘未建立SectorRadar或板块雷达目录",
        )
        result["finished_at"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        result["status"] = (
            "PASSED"
            if not result["error"]
            and result["checks"]
            and all(item["passed"] for item in result["checks"])
            else "FAILED"
        )
        _write_report(report, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在受控隔离数据库副本上验证全目录公开行情刷新。"
    )
    parser.add_argument("--clone-db", required=True, type=Path)
    parser.add_argument("--formal-db", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--expected-count", type=int, default=82)
    arguments = parser.parse_args()
    result = validate(
        arguments.clone_db,
        arguments.formal_db,
        arguments.report,
        arguments.expected_count,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
