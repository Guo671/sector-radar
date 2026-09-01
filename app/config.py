from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


APP_NAME = "板块雷达"
APP_VERSION = "4.4.0-alpha.41"
PROTOCOL_VERSION = "SECTOR_RADAR_RESULT_V3"
# Windows does not bundle the IANA time-zone database used by zoneinfo.
# A fixed UTC+8 offset is correct for both Beijing and Singapore time and keeps
# the app dependency-free on a normal Windows Python installation.
LOCAL_TZ = timezone(timedelta(hours=8), name="UTC+08:00")


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_today() -> date:
    return local_now().date()


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def data_root() -> Path:
    # The packaged app is deliberately portable: every database, backup,
    # export and task stays in a visible folder beside the EXE.  Never silently
    # fall back to C:\Users\... because the user may move the whole application
    # between drives and expects one self-contained folder.
    portable = app_root() / "data"
    portable.mkdir(parents=True, exist_ok=True)
    # A fixed probe name races when two app/self-test processes start together:
    # one process can delete the other process's still-open file on Windows.
    probe = portable / f".write_test_{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)
    return portable


DB_PATH = data_root() / "sector_radar.sqlite3"
EXPORT_DIR = data_root() / "exports"
BACKUP_DIR = data_root() / "backups"
TASK_DIR = data_root() / "tasks"
RUNTIME_DIR = data_root() / "runtime"

for directory in (EXPORT_DIR, BACKUP_DIR, TASK_DIR, RUNTIME_DIR):
    directory.mkdir(parents=True, exist_ok=True)


COLORS = {
    "bg": "#FFF9F4",
    "panel": "#FFFFFF",
    "nav": "#F7EDE7",
    "primary": "#C86478",
    "primary_dark": "#994457",
    "accent": "#6B9C8D",
    "text": "#3A3335",
    "muted": "#7E7476",
    "border": "#EADBD5",
    "profit": "#C84B55",
    "loss": "#4C9A78",
    "warning": "#D18B42",
    "danger": "#B9414A",
    "info": "#5B7EA6",
}
