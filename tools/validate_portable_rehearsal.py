from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PREFIX = "portable-rehearsal-"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _database_snapshot(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size": 0,
        "sha256": "",
        "integrity": "missing",
    }
    if not path.exists():
        return result
    result["size"] = path.stat().st_size
    result["sha256"] = _sha256(path)
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        result["integrity"] = str(
            connection.execute("PRAGMA integrity_check").fetchone()[0]
        )
    return result


def _empty_sensitive_settings(path: Path) -> bool:
    if not path.exists():
        return True
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone()
        if table is None:
            return True
        rows = connection.execute(
            """
            SELECT key, value FROM settings
            WHERE lower(key) LIKE '%api%key%'
               OR lower(key) LIKE '%secret%'
               OR lower(key) LIKE '%credential%'
            """
        ).fetchall()
    return not any(str(value or "").strip() for _, value in rows)


def _run(executable: Path, argument: str, timeout: int) -> dict[str, Any]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    started = datetime.now().astimezone()
    completed = subprocess.run(
        [str(executable), argument],
        cwd=str(executable.parent),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=flags,
        check=False,
    )
    return {
        "argument": argument,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


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
        shutil.rmtree(resolved)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def validate(installed_root: Path, expected_version: str, timeout: int) -> dict[str, Any]:
    root = installed_root.resolve()
    executable = root / "板块雷达.exe"
    internal = root / "_internal"
    formal_database = root / "data" / "sector_radar.sqlite3"
    runtime_root = root / "data" / "runtime"
    validation_root = root / "validation"
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    rehearsal = runtime_root / f"{PREFIX}{expected_version}-{timestamp}-{os.getpid()}"
    report_path = validation_root / (
        f"portable-rehearsal-{expected_version}-{timestamp}.json"
    )
    report: dict[str, Any] = {
        "status": "FAILED",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "expected_version": expected_version,
        "installed_root": str(root),
        "rehearsal_root": str(rehearsal),
        "report_path": str(report_path),
        "network_calls": 0,
        "copied_user_data": False,
        "copied_api_key": False,
        "e_drive_sector_radar_absent_before": not (
            Path("E:/SectorRadar").exists() or Path("E:/板块雷达").exists()
        ),
        "checks": [],
        "stages": [],
        "error": "",
    }
    formal_before = _database_snapshot(formal_database)
    report["formal_database_before"] = formal_before
    cleaned = False
    try:
        if root.drive.upper() != "G:":
            raise RuntimeError(f"当前G盘阶段只允许从G盘验证，实际为：{root}")
        if not executable.is_file() or not internal.is_dir():
            raise RuntimeError("安装目录缺少板块雷达.exe或_internal。")
        if rehearsal.exists():
            raise RuntimeError(f"隔离目录已存在：{rehearsal}")
        runtime_root.mkdir(parents=True, exist_ok=True)
        rehearsal.mkdir()
        shutil.copy2(executable, rehearsal / executable.name)
        shutil.copytree(internal, rehearsal / internal.name)
        data_was_absent_after_copy = not (rehearsal / "data").exists()
        report["checks"].append({
            "name": "application_only_copy",
            "passed": data_was_absent_after_copy,
            "detail": "只复制EXE与_internal；没有复制正式data目录",
        })

        copied_executable = rehearsal / executable.name
        report["stages"].append(_run(copied_executable, "--self-test", timeout))
        report["stages"].append(_run(copied_executable, "--acceptance-test", timeout))
        report_files = sorted(
            (rehearsal / "validation").glob("release-acceptance-*.json"),
            key=lambda item: item.stat().st_mtime,
        )
        if not report_files:
            raise RuntimeError("隔离EXE没有生成发布验收报告。")
        acceptance = json.loads(report_files[-1].read_text(encoding="utf-8"))
        report["isolated_acceptance"] = acceptance
        isolated_database = rehearsal / "data" / "sector_radar.sqlite3"
        report["copied_api_key"] = not _empty_sensitive_settings(isolated_database)
        report["checks"].extend([
            {
                "name": "self_test_passed",
                "passed": report["stages"][0]["returncode"] == 0,
                "detail": "复制后的真实EXE隐藏创建全部页面并正常退出",
            },
            {
                "name": "acceptance_process_passed",
                "passed": report["stages"][1]["returncode"] == 0,
                "detail": "复制后的真实EXE完成离线全链验收",
            },
            {
                "name": "acceptance_report_passed",
                "passed": acceptance.get("status") == "PASSED",
                "detail": "隔离发布验收JSON状态为PASSED",
            },
            {
                "name": "expected_version",
                "passed": acceptance.get("app_version") == expected_version,
                "detail": f"验收版本应为{expected_version}",
            },
            {
                "name": "offline_only",
                "passed": acceptance.get("network_calls") == 0,
                "detail": "验收不联网，也不调用DeepSeek",
            },
            {
                "name": "no_api_key_copy",
                "passed": not report["copied_api_key"],
                "detail": "隔离副本没有复制或生成非空API密钥字段",
            },
        ])
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        formal_after = _database_snapshot(formal_database)
        report["formal_database_after"] = formal_after
        formal_unchanged = (
            formal_before.get("sha256") == formal_after.get("sha256")
            and formal_before.get("integrity") == "ok"
            and formal_after.get("integrity") == "ok"
        )
        report["checks"].append({
            "name": "formal_database_unchanged",
            "passed": formal_unchanged,
            "detail": "正式数据库验收前后完整且SHA-256完全一致",
        })
        try:
            _safe_remove(rehearsal, runtime_root)
            cleaned = not rehearsal.exists()
        except BaseException as cleanup_error:
            if report["error"]:
                report["error"] += f"；清理失败：{cleanup_error}"
            else:
                report["error"] = f"清理失败：{cleanup_error}"
        report["rehearsal_directory_deleted"] = cleaned
        report["checks"].append({
            "name": "rehearsal_directory_deleted",
            "passed": cleaned,
            "detail": "受控的G盘隔离副本已清理",
        })
        e_absent_after = not (
            Path("E:/SectorRadar").exists() or Path("E:/板块雷达").exists()
        )
        report["e_drive_sector_radar_absent_after"] = e_absent_after
        report["checks"].append({
            "name": "e_drive_untouched",
            "passed": bool(report["e_drive_sector_radar_absent_before"] and e_absent_after),
            "detail": "演练前后E盘均未建立板块雷达目录",
        })
        report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        if not report["error"] and all(item["passed"] for item in report["checks"]):
            report["status"] = "PASSED"
        _write_report(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="板块雷达G盘便携运行演练")
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    arguments = parser.parse_args()
    report = validate(
        arguments.installed_root,
        arguments.expected_version,
        max(30, arguments.timeout),
    )
    print(json.dumps({
        "status": report["status"],
        "report_path": report["report_path"],
        "checks": len(report["checks"]),
        "error": report["error"],
    }, ensure_ascii=False))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
