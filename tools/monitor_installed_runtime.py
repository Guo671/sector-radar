from __future__ import annotations

import argparse
import ctypes
import json
import os
import sqlite3
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def _database_state(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as connection:
        return {
            "integrity": str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            ),
            "foreign_keys": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
            "running_jobs": int(
                connection.execute(
                    "SELECT COUNT(*) FROM analysis_jobs WHERE status='运行中'"
                ).fetchone()[0]
            ),
            "counts": {
                table: int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                )
                for table in (
                    "holdings",
                    "trades",
                    "cost_lots",
                    "ledger_postings",
                    "active_cash_ledger",
                    "analysis_jobs",
                    "decision_drafts",
                    "recurring_occurrences",
                )
            },
        }


def _process_state(pid: int) -> dict[str, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return {"pid": pid, "running": False, "error": "process_not_found"}
    try:
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        handle_count = wintypes.DWORD()
        kernel32.GetProcessHandleCount(handle, ctypes.byref(handle_count))
        path_buffer = ctypes.create_unicode_buffer(32768)
        path_size = wintypes.DWORD(len(path_buffer))
        kernel32.QueryFullProcessImageNameW(
            handle, 0, path_buffer, ctypes.byref(path_size)
        )
        windows: list[dict[str, Any]] = []
        enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @enum_callback
        def visit(hwnd: int, _lparam: int) -> bool:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if int(owner.value) == pid and user32.IsWindowVisible(hwnd):
                length = int(user32.GetWindowTextLengthW(hwnd))
                title_buffer = ctypes.create_unicode_buffer(max(1, length + 1))
                user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
                windows.append(
                    {
                        "title": title_buffer.value,
                        "hung": bool(user32.IsHungAppWindow(hwnd)),
                    }
                )
            return True

        user32.EnumWindows(visit, 0)
        return {
            "pid": pid,
            "running": int(exit_code.value) == STILL_ACTIVE,
            "exit_code": int(exit_code.value),
            "path": path_buffer.value,
            "handle_count": int(handle_count.value),
            "visible_windows": windows,
            "responding": bool(windows) and not any(item["hung"] for item in windows),
        }
    finally:
        kernel32.CloseHandle(handle)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def monitor(
    installed_root: Path,
    pid: int,
    duration_minutes: int,
    interval_seconds: int,
    expected_version: str,
) -> dict[str, Any]:
    root = installed_root.resolve()
    database = root / "data" / "sector_radar.sqlite3"
    executable = root / "板块雷达.exe"
    report_path = root / "validation" / (
        f"runtime-soak-{expected_version}-{datetime.now().astimezone():%Y%m%d-%H%M%S}.json"
    )
    report: dict[str, Any] = {
        "status": "RUNNING",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "installed_root": str(root),
        "expected_executable": str(executable),
        "expected_version": expected_version,
        "pid": pid,
        "duration_minutes": duration_minutes,
        "interval_seconds": interval_seconds,
        "network_calls": 0,
        "samples": [],
        "error": "",
        "report_path": str(report_path),
    }
    deadline = time.monotonic() + duration_minutes * 60
    try:
        while True:
            process = _process_state(pid)
            database_state = _database_state(database)
            sample = {
                "sampled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "process": process,
                "database": database_state,
            }
            report["samples"].append(sample)
            path_ok = Path(str(process.get("path", ""))).resolve() == executable.resolve()
            healthy = (
                bool(process.get("running"))
                and bool(process.get("responding"))
                and path_ok
                and str(database_state.get("integrity", "")).lower() == "ok"
                and int(database_state.get("foreign_keys", -1)) == 0
            )
            sample["healthy"] = healthy
            sample["path_matches"] = path_ok
            if not healthy:
                report["status"] = "FAILED"
                report["error"] = "进程响应、路径或数据库完整性检查未通过。"
                break
            if time.monotonic() >= deadline:
                report["status"] = "PASSED"
                break
            _write_report(report_path, report)
            time.sleep(min(interval_seconds, max(0.0, deadline - time.monotonic())))
    except BaseException as exc:
        report["status"] = "FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    report["sample_count"] = len(report["samples"])
    _write_report(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="只读监控E盘安装版连续运行稳定性")
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--duration-minutes", type=int, default=60)
    parser.add_argument("--interval-seconds", type=int, default=600)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    report = monitor(
        args.installed_root,
        args.pid,
        max(1, args.duration_minutes),
        max(10, args.interval_seconds),
        args.expected_version,
    )
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
