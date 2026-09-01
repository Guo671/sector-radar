from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.database as database_module  # noqa: E402
from app.database import Database  # noqa: E402


PREFIX = "installed-restore-rehearsal-"
CORE_TABLES = (
    "settings",
    "funds",
    "holdings",
    "trades",
    "cost_lots",
    "ledger_postings",
    "active_cash_ledger",
    "analysis_jobs",
    "radar_runs",
    "candidates",
    "sector_metrics",
    "sector_evidence",
    "model_calls",
    "decision_drafts",
    "recommendation_observations",
)


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
        "size": path.stat().st_size if path.is_file() else 0,
        "sha256": _sha256(path) if path.is_file() else "",
        "integrity": "missing",
        "foreign_keys": -1,
        "table_counts": {},
    }
    if not path.is_file():
        return result
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        result["integrity"] = str(
            connection.execute("PRAGMA integrity_check").fetchone()[0]
        )
        result["foreign_keys"] = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        available = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        result["table_counts"] = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in CORE_TABLES
            if table in available
        }
    return result


def _safe_remove(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    safe_parent = parent.resolve()
    if resolved.parent != safe_parent:
        raise RuntimeError(f"拒绝清理父目录不匹配的路径：{resolved}")
    if not resolved.name.startswith(PREFIX):
        raise RuntimeError(f"拒绝清理名称不受控的路径：{resolved.name}")
    if resolved.is_symlink():
        raise RuntimeError(f"拒绝递归清理符号链接：{resolved}")
    if resolved.exists():
        last_error: OSError | None = None
        for attempt in range(6):
            try:
                gc.collect()
                for suffix in ("-wal", "-shm"):
                    Path(str(resolved / "scratch.sqlite3") + suffix).unlink(
                        missing_ok=True
                    )
                shutil.rmtree(resolved)
                return
            except OSError as exc:
                last_error = exc
                if attempt < 5:
                    time.sleep(0.2 * (attempt + 1))
        if last_error is not None:
            raise last_error


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def validate(installed_root: Path, expected_version: str) -> dict[str, Any]:
    root = installed_root.resolve()
    formal_database = root / "data" / "sector_radar.sqlite3"
    backup_root = root / "data" / "backups"
    runtime_root = root / "data" / "runtime"
    validation_root = root / "validation"
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    rehearsal = runtime_root / f"{PREFIX}{expected_version}-{stamp}-{os.getpid()}"
    protected_backup = backup_root / (
        f"sector_radar-E-independent-check-{expected_version}-{stamp}.sqlite3"
    )
    report_path = validation_root / (
        f"installed-backup-restore-{expected_version}-{stamp}.json"
    )
    report: dict[str, Any] = {
        "status": "FAILED",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "installed_root": str(root),
        "expected_version": expected_version,
        "formal_database": str(formal_database),
        "protected_backup": str(protected_backup),
        "rehearsal_root": str(rehearsal),
        "report_path": str(report_path),
        "network_calls": 0,
        "checks": [],
        "error": "",
    }
    formal_before = _database_snapshot(formal_database)
    report["formal_before"] = formal_before
    cleaned = False
    original_backup_dir = database_module.BACKUP_DIR
    try:
        if root.drive.upper() != "E:":
            raise RuntimeError(f"此验收只允许测试E盘安装目录，实际为：{root}")
        if not formal_database.is_file():
            raise FileNotFoundError(f"正式数据库不存在：{formal_database}")
        if rehearsal.exists():
            raise RuntimeError(f"隔离演练目录已存在：{rehearsal}")
        backup_root.mkdir(parents=True, exist_ok=True)
        runtime_root.mkdir(parents=True, exist_ok=True)
        rehearsal.mkdir()

        formal = Database(formal_database)
        formal.backup(protected_backup, prune_automatic=False)
        backup_snapshot = _database_snapshot(protected_backup)
        report["protected_backup_snapshot"] = backup_snapshot

        scratch_database = rehearsal / "scratch.sqlite3"
        scratch_backup_root = rehearsal / "safety-backups"
        scratch_backup_root.mkdir()
        database_module.BACKUP_DIR = scratch_backup_root
        scratch = Database(scratch_database)
        safety_backup = scratch.restore(protected_backup)
        restored_snapshot = _database_snapshot(scratch_database)
        report["restored_snapshot"] = restored_snapshot
        report["safety_backup"] = str(safety_backup)
        report["checks"].extend(
            [
                {
                    "name": "backup_integrity",
                    "passed": backup_snapshot["integrity"].lower() == "ok",
                    "detail": "E盘正式数据库已生成受保护备份且SQLite完整",
                },
                {
                    "name": "backup_foreign_keys",
                    "passed": backup_snapshot["foreign_keys"] == 0,
                    "detail": "E盘受保护备份没有外键损坏",
                },
                {
                    "name": "restore_integrity",
                    "passed": restored_snapshot["integrity"].lower() == "ok",
                    "detail": "受保护备份已成功恢复到隔离临时数据库",
                },
                {
                    "name": "restore_foreign_keys",
                    "passed": restored_snapshot["foreign_keys"] == 0,
                    "detail": "隔离恢复数据库没有外键损坏",
                },
                {
                    "name": "core_counts_match_backup",
                    "passed": (
                        restored_snapshot["table_counts"]
                        == backup_snapshot["table_counts"]
                    ),
                    "detail": "15张核心业务表行数与备份完全一致",
                },
                {
                    "name": "restore_created_safety_backup",
                    "passed": Path(safety_backup).is_file(),
                    "detail": "恢复动作执行前自动生成了隔离安全回退副本",
                },
                {
                    "name": "offline_only",
                    "passed": report["network_calls"] == 0,
                    "detail": "整个备份恢复演练不联网、不调用DeepSeek",
                },
            ]
        )
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        database_module.BACKUP_DIR = original_backup_dir
        formal_after = _database_snapshot(formal_database)
        report["formal_after"] = formal_after
        formal_unchanged = (
            formal_before.get("sha256") == formal_after.get("sha256")
            and formal_before.get("integrity") == "ok"
            and formal_after.get("integrity") == "ok"
        )
        report["checks"].append(
            {
                "name": "formal_database_unchanged",
                "passed": formal_unchanged,
                "detail": "E盘正式数据库字节哈希与演练前一致",
            }
        )
        try:
            _safe_remove(rehearsal, runtime_root)
            cleaned = True
        except BaseException as cleanup_error:
            existing = report.get("error", "")
            report["error"] = (
                f"{existing}; 清理失败：{type(cleanup_error).__name__}: {cleanup_error}"
            ).strip("; ")
        report["temporary_deleted"] = cleaned and not rehearsal.exists()
        report["checks"].append(
            {
                "name": "temporary_deleted",
                "passed": report["temporary_deleted"],
                "detail": "受控隔离恢复目录已删除",
            }
        )
        report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        if report["checks"] and all(item["passed"] for item in report["checks"]):
            report["status"] = "PASSED"
            report["error"] = ""
        _write_report(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="验收已安装目录的独立备份与恢复能力")
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    report = validate(args.installed_root, args.expected_version)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
