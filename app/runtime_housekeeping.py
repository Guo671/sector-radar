from __future__ import annotations

import os
import json
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


_MEI_NAME = re.compile(r"_MEI\d+\Z")
_ROLLBACK_NAME = re.compile(
    r"predeploy-v\d+\.\d+\.\d+-(?:alpha|beta|rc)\.\d+"
    r"-(?P<date>\d{8})-(?P<clock>\d{6})(?:-(?P<fraction>\d{3,6}))?\Z",
    re.IGNORECASE,
)
_MIGRATION_NAME = re.compile(
    r"sector-radar-v\d+-migration-(?:\d{8}-\d{6}|[a-z0-9]{8})\Z",
    re.IGNORECASE,
)
_AUTOMATIC_BACKUP_NAME = re.compile(
    r"板块雷达备份_(?P<date>\d{8})_(?P<clock>\d{6})_"
    r"(?P<fraction>\d{6})\.sqlite3\Z",
)
_AUTOMATIC_BACKUP_SIDECAR_NAME = re.compile(
    r"(?P<base>板块雷达备份_(?P<date>\d{8})_(?P<clock>\d{6})_"
    r"(?P<fraction>\d{6})\.sqlite3)-(?P<kind>wal|shm)\Z",
)
_DEPLOYMENT_BACKUP_NAMES = (
    re.compile(
        r"sector_radar-predeploy-v\d+\.\d+\.\d+-(?:alpha|beta|rc)\.\d+"
        r"-(?P<date>\d{8})-(?P<clock>\d{6})-(?P<fraction>\d{3,6})\.sqlite3\Z",
        re.IGNORECASE,
    ),
    re.compile(
        r"sector_radar_predeploy_\d+\.\d+\.\d+-(?:alpha|beta|rc)\.\d+"
        r"_(?P<date>\d{8})_(?P<clock>\d{6})_(?P<fraction>\d{3,6})\.sqlite3\Z",
        re.IGNORECASE,
    ),
    re.compile(
        r"predeploy-v\d+\.\d+\.\d+-(?:alpha|beta|rc)\.\d+"
        r"-(?P<date>\d{8})-(?P<clock>\d{6})\.sqlite3\Z",
        re.IGNORECASE,
    ),
)
_DIAGNOSTIC_NAME = re.compile(r"API任务诊断_[A-Za-z0-9._-]+\.json\Z")
_REPARSE_POINT = 0x400


def _stop_requested(callback: Callable[[], bool] | None) -> bool:
    """Treat an unavailable/broken cancellation callback as a safe stop."""
    if callback is None:
        return False
    try:
        return bool(callback())
    except Exception:
        return True


def runtime_cleanup_due(
    last_value: Any,
    *,
    now: datetime,
    interval_hours: float = 24.0,
) -> bool:
    """Return whether slow storage maintenance is due for another pass."""
    raw = str(last_value or "").strip()
    if not raw:
        return True
    try:
        previous = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    current = now
    if previous.tzinfo is None and current.tzinfo is not None:
        previous = previous.replace(tzinfo=current.tzinfo)
    elif previous.tzinfo is not None and current.tzinfo is None:
        current = current.replace(tzinfo=previous.tzinfo)
    age_seconds = (current - previous).total_seconds()
    # A future timestamp usually means the system clock moved backwards.  It
    # is safer to skip one maintenance pass than to loop on every launch.
    return age_seconds >= max(1.0, float(interval_hours)) * 3600


def _is_reparse_point(path: Path) -> bool:
    """Do not follow a symlink/junction while removing a cache directory."""
    try:
        stat = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(
        getattr(stat, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _directory_size(
    path: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    total = 0
    for base, directories, files in os.walk(path, followlinks=False):
        if _stop_requested(stop_requested):
            break
        # Reparse points are not part of a legitimate PyInstaller extraction.
        directories[:] = [
            name for name in directories
            if not _is_reparse_point(Path(base) / name)
        ]
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                pass
    return total


def _contains_reparse_point(
    path: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    """Reject a candidate containing links/junctions before recursive delete."""
    try:
        for base, directories, files in os.walk(path, followlinks=False):
            if _stop_requested(stop_requested):
                return True
            parent = Path(base)
            for name in (*directories, *files):
                if _is_reparse_point(parent / name):
                    return True
    except OSError:
        return True
    return False


def _direct_child(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=True).parent == root.resolve(strict=True)
    except OSError:
        return False


def _valid_rollback_directory(
    path: Path,
    root: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    """Recognise only a deployment rollback folder that cannot hold user data."""
    if (
        _stop_requested(stop_requested)
        or not _ROLLBACK_NAME.fullmatch(path.name)
        or not path.is_dir()
        or _is_reparse_point(path)
        or not _direct_child(path, root)
        or _contains_reparse_point(path, stop_requested)
    ):
        return False
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    allowed = all(
        child.name == "_internal"
        or (child.is_file() and child.suffix.lower() in {".exe", ".txt"})
        for child in children
    )
    if not allowed or not any(child.name == "_internal" for child in children):
        return False
    if not any(child.is_file() and child.suffix.lower() == ".exe" for child in children):
        return False
    # Even a correctly named rollback is protected if somebody placed a
    # database inside it.  Runtime DLL names such as sqlite3.dll are harmless;
    # only actual database/container suffixes are rejected.
    try:
        for item in path.rglob("*"):
            if _stop_requested(stop_requested):
                return False
            if item.is_file() and item.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                return False
    except OSError:
        return False
    return True


def _valid_migration_directory(
    path: Path,
    root: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    """Accept only a disposable, healthy migration rehearsal database copy."""
    if (
        _stop_requested(stop_requested)
        or not _MIGRATION_NAME.fullmatch(path.name)
        or not path.is_dir()
        or _is_reparse_point(path)
        or not _direct_child(path, root)
        or _contains_reparse_point(path, stop_requested)
    ):
        return False
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    allowed_names = {
        "migration-copy.sqlite3",
        "migration-copy.sqlite3-wal",
        "migration-copy.sqlite3-shm",
    }
    if not children or any(not child.is_file() or child.name not in allowed_names for child in children):
        return False
    database = path / "migration-copy.sqlite3"
    if not database.is_file():
        return False
    try:
        # ``mode=ro`` alone may still create/update WAL shared-memory files on
        # Windows.  These rehearsal copies are closed, immutable snapshots;
        # immutable=1 makes an inventory genuinely non-mutating.
        uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            if stop_requested is not None:
                connection.set_progress_handler(
                    lambda: 1 if _stop_requested(stop_requested) else 0,
                    1000,
                )
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0]).lower() == "ok"
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError):
        return False


def valid_automatic_backup_file(
    path: Path,
    root: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    """Recognise only a healthy app-created automatic database backup.

    A filename prefix is not sufficient proof: a user may deliberately give a
    manual backup a similar name.  The current automatic format includes a
    six-digit microsecond suffix, must be a direct non-link child of the backup
    folder, and must contain the core SectorRadar tables with a clean SQLite
    integrity result.  Anything uncertain remains protected.
    """
    if _stop_requested(stop_requested):
        return False
    candidate = Path(path)
    backup_root = Path(root)
    if (
        not _AUTOMATIC_BACKUP_NAME.fullmatch(candidate.name)
        or not candidate.is_file()
        or _is_reparse_point(candidate)
        or not _direct_child(candidate, backup_root)
        or not _automatic_backup_sidecars_are_safe(candidate, backup_root)
    ):
        return False
    return _valid_sector_radar_backup_database(candidate, stop_requested)


def _deployment_backup_match(name: str):
    for pattern in _DEPLOYMENT_BACKUP_NAMES:
        match = pattern.fullmatch(name)
        if match:
            return match
    return None


def _valid_sector_radar_backup_database(
    candidate: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    """Read-only integrity/schema check shared by every managed DB snapshot."""
    try:
        uri = candidate.resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            if stop_requested is not None:
                # ``PRAGMA integrity_check`` can take tens of seconds across
                # many backups on a mechanical disk.  The progress handler
                # lets a single close request interrupt the current check.
                connection.set_progress_handler(
                    lambda: 1 if _stop_requested(stop_requested) else 0,
                    1000,
                )
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {"settings", "funds", "holdings", "candidates"}
            if not required.issubset(tables):
                return False
            check = connection.execute("PRAGMA integrity_check").fetchone()
            return bool(check) and str(check[0]).lower() == "ok"
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError):
        return False


def valid_deployment_backup_file(
    path: Path,
    root: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    """Recognise only a healthy database snapshot created before deployment.

    Three exact historical formats are supported because all three were
    emitted by SectorRadar's own deployment workflow.  Near matches, manual
    names, links, schema-incomplete databases and snapshots with a non-empty
    WAL are never managed automatically.
    """
    if _stop_requested(stop_requested):
        return False
    candidate = Path(path)
    backup_root = Path(root)
    if (
        _deployment_backup_match(candidate.name) is None
        or not candidate.is_file()
        or _is_reparse_point(candidate)
        or not _direct_child(candidate, backup_root)
        or not _deployment_backup_sidecars_are_safe(candidate, backup_root)
    ):
        return False
    return _valid_sector_radar_backup_database(candidate, stop_requested)


def _automatic_backup_sidecars_are_safe(database: Path, root: Path) -> bool:
    """Require closed, ordinary sidecars and an empty-or-absent WAL.

    A non-empty WAL may contain database pages that were never checkpointed
    into the main file.  Such a backup is deliberately excluded from every
    automatic retention/deletion plan.  A SHM file is only a rebuildable WAL
    index, but it must still be a direct, non-link file beside the backup.
    """
    backup_root = Path(root)
    for kind in ("wal", "shm"):
        sidecar = Path(f"{database}-{kind}")
        if not os.path.lexists(sidecar):
            continue
        match = _AUTOMATIC_BACKUP_SIDECAR_NAME.fullmatch(sidecar.name)
        if (
            not match
            or match.group("base") != database.name
            or not sidecar.is_file()
            or _is_reparse_point(sidecar)
            or not _direct_child(sidecar, backup_root)
        ):
            return False
        try:
            if kind == "wal" and sidecar.stat().st_size != 0:
                return False
        except OSError:
            return False
    return True


def _deployment_backup_sidecar_parts(name: str) -> tuple[str, str] | None:
    for kind in ("wal", "shm"):
        suffix = f"-{kind}"
        if name.endswith(suffix):
            base = name[:-len(suffix)]
            if _deployment_backup_match(base) is not None:
                return base, kind
    return None


def _deployment_backup_sidecars_are_safe(database: Path, root: Path) -> bool:
    """Protect a predeploy snapshot whenever its WAL may contain real pages."""
    backup_root = Path(root)
    for kind in ("wal", "shm"):
        sidecar = Path(f"{database}-{kind}")
        if not os.path.lexists(sidecar):
            continue
        parts = _deployment_backup_sidecar_parts(sidecar.name)
        if (
            parts is None
            or parts != (database.name, kind)
            or not sidecar.is_file()
            or _is_reparse_point(sidecar)
            or not _direct_child(sidecar, backup_root)
        ):
            return False
        try:
            if kind == "wal" and sidecar.stat().st_size != 0:
                return False
        except OSError:
            return False
    return True


def deployment_backup_sidecar_cleanup_plan(
    root: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Preview disposable WAL/SHM remnants for exact predeploy snapshots."""
    backup_root = Path(root).resolve()
    if _stop_requested(stop_requested):
        return {
            "removable": [], "protected": 0, "errors": [], "stopped": True,
        }
    groups: dict[str, list[Path]] = {}
    protected = 0
    errors: list[str] = []
    try:
        entries = list(backup_root.iterdir()) if backup_root.is_dir() else []
    except OSError as exc:
        return {
            "removable": [], "protected": 0, "errors": [str(exc)],
            "stopped": _stop_requested(stop_requested),
        }
    for path in entries:
        parts = _deployment_backup_sidecar_parts(path.name)
        if parts is not None:
            groups.setdefault(parts[0], []).append(path)
    removable: list[tuple[Path, str]] = []
    for base_name, sidecars in groups.items():
        if _stop_requested(stop_requested):
            return {
                "removable": [], "protected": protected,
                "errors": errors, "stopped": True,
            }
        database = backup_root / base_name
        safe = True
        for sidecar in sidecars:
            parts = _deployment_backup_sidecar_parts(sidecar.name)
            if (
                parts is None
                or parts[0] != base_name
                or not sidecar.is_file()
                or _is_reparse_point(sidecar)
                or not _direct_child(sidecar, backup_root)
            ):
                safe = False
                break
        wal = backup_root / f"{base_name}-wal"
        try:
            if os.path.lexists(wal) and (not wal.is_file() or wal.stat().st_size != 0):
                safe = False
        except OSError as exc:
            safe = False
            errors.append(f"{wal.name}: {exc}")
        if os.path.lexists(database):
            safe = safe and valid_deployment_backup_file(
                database, backup_root, stop_requested,
            )
            if _stop_requested(stop_requested):
                return {
                    "removable": [], "protected": protected,
                    "errors": errors, "stopped": True,
                }
            reason = "已关闭部署前备份的空WAL/SHM边车"
        else:
            reason = "主部署前备份已不存在的空WAL/SHM边车"
        if safe:
            removable.extend((path, reason) for path in sidecars)
        else:
            protected += len(sidecars)
    return {
        "removable": removable, "protected": protected, "errors": errors,
        "stopped": False,
    }


def prune_deployment_backup_sidecars(
    root: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Delete only predeploy sidecars that still pass a fresh strict plan."""
    backup_root = Path(root).resolve()
    initial = deployment_backup_sidecar_cleanup_plan(backup_root, stop_requested)
    removed = 0
    bytes_freed = 0
    removed_names: list[str] = []
    errors = list(initial["errors"])
    for path, _reason in initial["removable"]:
        if _stop_requested(stop_requested):
            break
        try:
            resolved = path.resolve(strict=True)
            fresh = deployment_backup_sidecar_cleanup_plan(
                backup_root, stop_requested,
            )
            if fresh.get("stopped") or _stop_requested(stop_requested):
                break
            allowed = {
                candidate.resolve(strict=True)
                for candidate, _fresh_reason in fresh["removable"]
            }
            if resolved.parent != backup_root or resolved not in allowed:
                continue
            size = resolved.stat().st_size
            resolved.unlink()
            removed += 1
            bytes_freed += size
            removed_names.append(path.name)
        except (OSError, RuntimeError) as exc:
            errors.append(f"{path.name}: {exc}")
    return {
        "removed": removed,
        "bytes_freed": bytes_freed,
        "removed_names": removed_names,
        "protected": int(initial["protected"]),
        "errors": errors,
        "stopped": bool(initial.get("stopped")) or _stop_requested(stop_requested),
    }


def automatic_backup_sidecar_cleanup_plan(
    root: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Find only disposable automatic-backup WAL/SHM remnants.

    Sidecars beside a healthy automatic backup are safe to discard when the
    WAL is empty.  An orphan with an exact app-generated name is also safe
    because an empty WAL and its SHM cache contain no recoverable transaction
    pages.  Manual-name sidecars, links, malformed files and any non-empty WAL
    remain protected.
    """
    backup_root = Path(root).resolve()
    if _stop_requested(stop_requested):
        return {
            "removable": [], "protected": 0, "errors": [], "stopped": True,
        }
    groups: dict[str, list[Path]] = {}
    protected = 0
    errors: list[str] = []
    try:
        entries = list(backup_root.iterdir()) if backup_root.is_dir() else []
    except OSError as exc:
        return {
            "removable": [], "protected": 0, "errors": [str(exc)],
            "stopped": _stop_requested(stop_requested),
        }
    for path in entries:
        match = _AUTOMATIC_BACKUP_SIDECAR_NAME.fullmatch(path.name)
        if match:
            groups.setdefault(match.group("base"), []).append(path)
    removable: list[tuple[Path, str]] = []
    for base_name, sidecars in groups.items():
        if _stop_requested(stop_requested):
            return {
                "removable": [], "protected": protected,
                "errors": errors, "stopped": True,
            }
        database = backup_root / base_name
        safe = True
        for sidecar in sidecars:
            match = _AUTOMATIC_BACKUP_SIDECAR_NAME.fullmatch(sidecar.name)
            if (
                not match
                or match.group("base") != base_name
                or not sidecar.is_file()
                or _is_reparse_point(sidecar)
                or not _direct_child(sidecar, backup_root)
            ):
                safe = False
                break
        wal = backup_root / f"{base_name}-wal"
        try:
            if os.path.lexists(wal) and (not wal.is_file() or wal.stat().st_size != 0):
                safe = False
        except OSError as exc:
            safe = False
            errors.append(f"{wal.name}: {exc}")
        if os.path.lexists(database):
            safe = safe and valid_automatic_backup_file(
                database, backup_root, stop_requested,
            )
            if _stop_requested(stop_requested):
                return {
                    "removable": [], "protected": protected,
                    "errors": errors, "stopped": True,
                }
            reason = "已关闭自动备份的空WAL/SHM边车"
        else:
            reason = "主备份已不存在的空WAL/SHM边车"
        if safe:
            removable.extend((path, reason) for path in sidecars)
        else:
            protected += len(sidecars)
    return {
        "removable": removable, "protected": protected, "errors": errors,
        "stopped": False,
    }


def prune_automatic_backup_sidecars(
    root: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Delete only sidecars that still pass the strict plan at unlink time."""
    backup_root = Path(root).resolve()
    initial = automatic_backup_sidecar_cleanup_plan(backup_root, stop_requested)
    removed = 0
    bytes_freed = 0
    removed_names: list[str] = []
    errors = list(initial["errors"])
    for path, _reason in initial["removable"]:
        if _stop_requested(stop_requested):
            break
        try:
            resolved = path.resolve(strict=True)
            fresh = automatic_backup_sidecar_cleanup_plan(
                backup_root, stop_requested,
            )
            if fresh.get("stopped") or _stop_requested(stop_requested):
                break
            allowed = {
                candidate.resolve(strict=True)
                for candidate, _fresh_reason in fresh["removable"]
            }
            if resolved.parent != backup_root or resolved not in allowed:
                continue
            size = resolved.stat().st_size
            resolved.unlink()
            removed += 1
            bytes_freed += size
            removed_names.append(path.name)
        except (OSError, RuntimeError) as exc:
            errors.append(f"{path.name}: {exc}")
    return {
        "removed": removed,
        "bytes_freed": bytes_freed,
        "removed_names": removed_names,
        "protected": int(initial["protected"]),
        "errors": errors,
        "stopped": bool(initial.get("stopped")) or _stop_requested(stop_requested),
    }


def _automatic_backup_timestamp(path: Path) -> float:
    match = _AUTOMATIC_BACKUP_NAME.fullmatch(path.name)
    if match:
        try:
            value = datetime.strptime(
                f"{match.group('date')}{match.group('clock')}",
                "%Y%m%d%H%M%S",
            ).replace(microsecond=int(match.group("fraction")))
            return value.timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    return path.stat().st_mtime


def automatic_backup_retention_plan(
    root: Path,
    *,
    keep_count: int,
    keep_days: int,
    now: float | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Build a non-mutating, strictly validated automatic-backup plan.

    The newest ``keep_count`` files are unconditional.  Inside the requested
    history window one newest file per local calendar day is additionally
    retained.  Same-day bursts are therefore bounded without losing daily
    restore points.  Manual or unrecognised files never enter this plan.
    """
    backup_root = Path(root).resolve()
    if _stop_requested(stop_requested):
        return {
            "valid": [], "protected_matching": 0,
            "retained_latest": [], "retained_daily": [], "removable": [],
            "errors": [], "stopped": True,
        }
    timestamp = time.time() if now is None else float(now)
    valid: list[Path] = []
    invalid_matching = 0
    errors: list[str] = []
    try:
        entries = list(backup_root.iterdir()) if backup_root.is_dir() else []
    except OSError as exc:
        return {
            "valid": [], "protected_matching": 0,
            "retained_latest": [], "retained_daily": [], "removable": [],
            "errors": [str(exc)], "stopped": _stop_requested(stop_requested),
        }
    for path in entries:
        if _stop_requested(stop_requested):
            return {
                "valid": valid, "protected_matching": invalid_matching,
                "retained_latest": [], "retained_daily": [], "removable": [],
                "errors": errors, "stopped": True,
            }
        if not _AUTOMATIC_BACKUP_NAME.fullmatch(path.name):
            continue
        try:
            if valid_automatic_backup_file(path, backup_root, stop_requested):
                valid.append(path)
            elif _stop_requested(stop_requested):
                return {
                    "valid": valid, "protected_matching": invalid_matching,
                    "retained_latest": [], "retained_daily": [], "removable": [],
                    "errors": errors, "stopped": True,
                }
            else:
                invalid_matching += 1
        except (OSError, sqlite3.Error) as exc:
            invalid_matching += 1
            errors.append(f"{path.name}: {exc}")
    try:
        valid.sort(key=_automatic_backup_timestamp, reverse=True)
    except OSError as exc:
        return {
            "valid": valid, "protected_matching": invalid_matching + len(valid),
            "retained_latest": [], "retained_daily": [], "removable": [],
            "errors": [*errors, f"自动备份排序失败：{exc}"], "stopped": False,
        }
    latest = valid[:max(0, keep_count)]
    retained_days = {
        datetime.fromtimestamp(_automatic_backup_timestamp(path)).strftime("%Y%m%d")
        for path in latest
    }
    daily: list[Path] = []
    removable: list[tuple[Path, str]] = []
    cutoff = timestamp - max(0, keep_days) * 86400
    for path in valid[len(latest):]:
        if _stop_requested(stop_requested):
            return {
                "valid": valid, "protected_matching": invalid_matching,
                "retained_latest": latest, "retained_daily": daily,
                "removable": [], "errors": errors, "stopped": True,
            }
        try:
            file_timestamp = _automatic_backup_timestamp(path)
            day = datetime.fromtimestamp(file_timestamp).strftime("%Y%m%d")
        except (OSError, ValueError, OverflowError) as exc:
            invalid_matching += 1
            errors.append(f"{path.name}: {exc}")
            continue
        if file_timestamp >= cutoff and day not in retained_days:
            retained_days.add(day)
            daily.append(path)
            continue
        reason = "超过每日保留期" if file_timestamp < cutoff else "同日已有更新自动备份"
        removable.append((path, reason))
    return {
        "valid": valid,
        "protected_matching": invalid_matching,
        "retained_latest": latest,
        "retained_daily": daily,
        "removable": removable,
        "errors": errors,
        "stopped": False,
    }


def _deployment_backup_timestamp(path: Path) -> float:
    match = _deployment_backup_match(path.name)
    if match:
        try:
            value = datetime.strptime(
                f"{match.group('date')}{match.group('clock')}",
                "%Y%m%d%H%M%S",
            )
            fraction = (match.groupdict().get("fraction") or "0").ljust(6, "0")[:6]
            return value.replace(microsecond=int(fraction)).timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    return path.stat().st_mtime


def deployment_backup_retention_plan(
    root: Path,
    *,
    keep_count: int,
    keep_days: int,
    now: float | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Preview retention for strictly recognised deployment DB snapshots."""
    backup_root = Path(root).resolve()
    if _stop_requested(stop_requested):
        return {
            "valid": [], "protected_matching": 0,
            "retained_latest": [], "retained_daily": [], "removable": [],
            "errors": [], "stopped": True,
        }
    timestamp = time.time() if now is None else float(now)
    valid: list[Path] = []
    invalid_matching = 0
    errors: list[str] = []
    try:
        entries = list(backup_root.iterdir()) if backup_root.is_dir() else []
    except OSError as exc:
        return {
            "valid": [], "protected_matching": 0,
            "retained_latest": [], "retained_daily": [], "removable": [],
            "errors": [str(exc)], "stopped": _stop_requested(stop_requested),
        }
    for path in entries:
        if _stop_requested(stop_requested):
            return {
                "valid": valid, "protected_matching": invalid_matching,
                "retained_latest": [], "retained_daily": [], "removable": [],
                "errors": errors, "stopped": True,
            }
        if _deployment_backup_match(path.name) is None:
            continue
        try:
            if valid_deployment_backup_file(path, backup_root, stop_requested):
                valid.append(path)
            elif _stop_requested(stop_requested):
                return {
                    "valid": valid, "protected_matching": invalid_matching,
                    "retained_latest": [], "retained_daily": [], "removable": [],
                    "errors": errors, "stopped": True,
                }
            else:
                invalid_matching += 1
        except (OSError, sqlite3.Error) as exc:
            invalid_matching += 1
            errors.append(f"{path.name}: {exc}")
    try:
        valid.sort(key=_deployment_backup_timestamp, reverse=True)
    except OSError as exc:
        return {
            "valid": valid, "protected_matching": invalid_matching + len(valid),
            "retained_latest": [], "retained_daily": [], "removable": [],
            "errors": [*errors, f"部署前备份排序失败：{exc}"], "stopped": False,
        }
    latest = valid[:max(0, keep_count)]
    retained_days = {
        datetime.fromtimestamp(_deployment_backup_timestamp(path)).strftime("%Y%m%d")
        for path in latest
    }
    daily: list[Path] = []
    removable: list[tuple[Path, str]] = []
    cutoff = timestamp - max(0, keep_days) * 86400
    for path in valid[len(latest):]:
        if _stop_requested(stop_requested):
            return {
                "valid": valid, "protected_matching": invalid_matching,
                "retained_latest": latest, "retained_daily": daily,
                "removable": [], "errors": errors, "stopped": True,
            }
        try:
            file_timestamp = _deployment_backup_timestamp(path)
            day = datetime.fromtimestamp(file_timestamp).strftime("%Y%m%d")
        except (OSError, ValueError, OverflowError) as exc:
            invalid_matching += 1
            errors.append(f"{path.name}: {exc}")
            continue
        if file_timestamp >= cutoff and day not in retained_days:
            retained_days.add(day)
            daily.append(path)
            continue
        reason = "超过部署备份每日保留期" if file_timestamp < cutoff else "同日已有更新部署备份"
        removable.append((path, reason))
    return {
        "valid": valid,
        "protected_matching": invalid_matching,
        "retained_latest": latest,
        "retained_daily": daily,
        "removable": removable,
        "errors": errors,
        "stopped": False,
    }


def _valid_diagnostic_file(
    path: Path,
    root: Path,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    """Recognise only this app's explicitly redacted API diagnostic export."""
    if (
        _stop_requested(stop_requested)
        or not _DIAGNOSTIC_NAME.fullmatch(path.name)
        or not path.is_file()
        or _is_reparse_point(path)
        or not _direct_child(path, root)
    ):
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    note = str(payload.get("note") or "")
    return "脱敏结构诊断" in note and "API Key" in note


def _bounded_integer(
    settings: dict[str, Any], key: str, default: int, minimum: int, maximum: int,
) -> int:
    try:
        value = int(float(settings.get(key, default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def managed_storage_policy(settings: dict[str, Any] | None = None) -> dict[str, int]:
    """Return a conservative policy; newest recovery evidence is never pruned."""
    values = dict(settings or {})
    return {
        "backup_keep_count": _bounded_integer(values, "backup_keep_count", 14, 3, 100),
        "backup_keep_days": _bounded_integer(values, "backup_keep_days", 30, 1, 3650),
        "deployment_backup_keep_count": _bounded_integer(
            values, "deployment_backup_keep_count", 3, 2, 20,
        ),
        "deployment_backup_keep_days": _bounded_integer(
            values, "deployment_backup_keep_days", 30, 7, 3650,
        ),
        "rollback_keep_count": _bounded_integer(values, "rollback_keep_count", 3, 2, 20),
        "rollback_keep_days": _bounded_integer(values, "rollback_keep_days", 30, 7, 3650),
        "diagnostic_keep_count": _bounded_integer(values, "diagnostic_keep_count", 20, 5, 200),
        "diagnostic_keep_days": _bounded_integer(values, "diagnostic_keep_days", 180, 30, 3650),
        # Migration copies contain no unique user state.  One recent healthy
        # rehearsal remains available, and nothing younger than seven days is
        # removed even when several builds happen close together.
        "migration_keep_count": 1,
        "migration_keep_days": 7,
    }


def _prune_paths(
    paths: list[Path],
    *,
    root: Path,
    keep_count: int,
    keep_days: int,
    validator,
    now: float,
    mtime_getter=None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    valid = []
    protected = 0
    errors: list[str] = []
    for path in paths:
        if _stop_requested(stop_requested):
            break
        try:
            if validator(path, root, stop_requested):
                valid.append(path)
            else:
                protected += 1
        except (OSError, sqlite3.Error) as exc:
            protected += 1
            errors.append(f"{path.name}: {exc}")
    modified_at = mtime_getter or (lambda path: path.stat().st_mtime)
    valid.sort(key=modified_at, reverse=True)
    removed = 0
    bytes_freed = 0
    cutoff = now - max(0, keep_days) * 86400
    for index, path in enumerate(valid):
        if _stop_requested(stop_requested):
            break
        try:
            # Both limits must say "old" before deleting.  Rapid development
            # may temporarily exceed the count, but never sacrifices a fresh
            # rollback solely because several builds happened on one day.
            if index < keep_count or modified_at(path) >= cutoff:
                continue
            if not validator(path, root, stop_requested):
                protected += 1
                continue
            size = (
                _directory_size(path, stop_requested)
                if path.is_dir() else path.stat().st_size
            )
            if _stop_requested(stop_requested):
                break
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
            bytes_freed += size
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    return {
        "eligible": len(valid), "protected": protected, "removed": removed,
        "bytes_freed": bytes_freed, "errors": errors,
        "stopped": _stop_requested(stop_requested),
    }


def _category(paths: list[Path], validator, root: Path) -> dict[str, int]:
    count = 0
    size = 0
    for path in paths:
        try:
            if not validator(path, root):
                continue
            count += 1
            size += _directory_size(path) if path.is_dir() else path.stat().st_size
        except OSError:
            continue
    return {"count": count, "bytes": size}


def _rollback_roots(runtime_root: Path, program_root: Path | None) -> list[Path]:
    """Return unique roots that may directly contain deployment rollbacks.

    Older builds put ``predeploy-*`` beside the EXE while newer maintenance
    code originally inspected only ``data/runtime``.  Both locations are
    legitimate, but an overlapping/same path must never be counted twice.
    """
    roots = [Path(runtime_root).resolve()]
    if program_root is None:
        return roots
    program = Path(program_root).resolve()
    if program not in roots:
        roots.append(program)
    return roots


def _rollback_entries(roots: list[Path]) -> list[tuple[Path, Path]]:
    entries: list[tuple[Path, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        # Only exact rollback-looking names enter this destructive category.
        # Ordinary program/data folders remain wholly outside its boundary.
        entries.extend(
            (path, root) for path in children
            if _ROLLBACK_NAME.fullmatch(path.name)
        )
    return entries


def _rollback_category(entries: list[tuple[Path, Path]]) -> dict[str, int]:
    count = 0
    size = 0
    for path, root in entries:
        try:
            if not _valid_rollback_directory(path, root):
                continue
            count += 1
            size += _directory_size(path)
        except OSError:
            continue
    return {"count": count, "bytes": size}


def _rollback_timestamp(path: Path) -> float:
    """Use the deployment timestamp embedded in an app-created rollback name.

    Newer deploy scripts append millisecond/microsecond digits to avoid name
    collisions.  Falling back to the directory mtime keeps older or unusual
    but otherwise valid packages sortable without widening the name boundary.
    """
    match = _ROLLBACK_NAME.fullmatch(path.name)
    if match:
        try:
            value = datetime.strptime(
                f"{match.group('date')}{match.group('clock')}",
                "%Y%m%d%H%M%S",
            )
            fraction = str(match.group("fraction") or "")
            if fraction:
                value = value.replace(microsecond=int(fraction.ljust(6, "0")))
            return value.timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    return path.stat().st_mtime


def _rollback_day(path: Path) -> str:
    match = _ROLLBACK_NAME.fullmatch(path.name)
    if match:
        return str(match.group("date"))
    return datetime.fromtimestamp(_rollback_timestamp(path)).strftime("%Y%m%d")


def _rollback_retention_plan(
    entries: list[tuple[Path, Path]],
    *,
    keep_count: int,
    keep_days: int,
    now: float,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Plan a bounded rollback set without changing the filesystem.

    The newest ``keep_count`` packages are unconditional recovery points.
    Within ``keep_days`` we additionally retain at most one newest package per
    local calendar day.  Older same-day duplicates and expired packages are
    removable.  This keeps rapid alpha builds bounded while preserving useful
    daily history; anything that fails the strict structural validator remains
    protected and never enters the plan.
    """
    valid: list[tuple[Path, Path]] = []
    protected = 0
    errors: list[str] = []
    for path, root in entries:
        if _stop_requested(stop_requested):
            return {
                "valid": valid, "protected": protected, "errors": errors,
                "retained_latest": [], "retained_daily": [],
                "removable": [], "stopped": True,
            }
        try:
            if _valid_rollback_directory(path, root, stop_requested):
                valid.append((path, root))
            elif _stop_requested(stop_requested):
                return {
                    "valid": valid, "protected": protected, "errors": errors,
                    "retained_latest": [], "retained_daily": [],
                    "removable": [], "stopped": True,
                }
            else:
                protected += 1
        except OSError as exc:
            protected += 1
            errors.append(f"{path.name}: {exc}")
    try:
        valid.sort(key=lambda item: _rollback_timestamp(item[0]), reverse=True)
    except OSError as exc:
        # A disappearing candidate is not a reason to weaken the deletion
        # boundary.  Treat the whole plan as non-destructive for this run.
        return {
            "valid": valid,
            "protected": protected + len(valid),
            "errors": [*errors, f"回退目录排序失败：{exc}"],
            "retained_latest": [],
            "retained_daily": [],
            "removable": [],
            "stopped": False,
        }

    latest = valid[:max(0, keep_count)]
    retained_days = {_rollback_day(path) for path, _root in latest}
    daily: list[tuple[Path, Path]] = []
    removable: list[tuple[Path, Path, str]] = []
    cutoff = now - max(0, keep_days) * 86400
    for path, root in valid[len(latest):]:
        if _stop_requested(stop_requested):
            return {
                "valid": valid, "protected": protected, "errors": errors,
                "retained_latest": latest, "retained_daily": daily,
                "removable": [], "stopped": True,
            }
        try:
            timestamp = _rollback_timestamp(path)
            day = _rollback_day(path)
        except OSError as exc:
            protected += 1
            errors.append(f"{path.name}: {exc}")
            continue
        if timestamp >= cutoff and day not in retained_days:
            retained_days.add(day)
            daily.append((path, root))
            continue
        reason = "超过每日保留期" if timestamp < cutoff else "同日已有更新回退版本"
        removable.append((path, root, reason))
    return {
        "valid": valid,
        "protected": protected,
        "errors": errors,
        "retained_latest": latest,
        "retained_daily": daily,
        "removable": removable,
        "stopped": False,
    }


def _prune_rollback_entries(
    entries: list[tuple[Path, Path]],
    *,
    keep_count: int,
    keep_days: int,
    now: float,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Apply one global bounded policy across runtime and EXE-side copies."""
    plan = _rollback_retention_plan(
        entries, keep_count=keep_count, keep_days=keep_days, now=now,
        stop_requested=stop_requested,
    )
    removed = 0
    bytes_freed = 0
    removed_names: list[str] = []
    removed_reasons: dict[str, str] = {}
    protected = int(plan["protected"])
    errors = list(plan["errors"])
    for path, root, reason in plan["removable"]:
        if _stop_requested(stop_requested):
            break
        try:
            # Revalidate immediately before the destructive operation.  A
            # package that changed after planning becomes protected.
            if not _valid_rollback_directory(path, root, stop_requested):
                protected += 1
                continue
            size = _directory_size(path, stop_requested)
            if _stop_requested(stop_requested):
                break
            shutil.rmtree(path)
            removed += 1
            bytes_freed += size
            removed_names.append(path.name)
            removed_reasons[path.name] = reason
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    return {
        "eligible": len(plan["valid"]), "protected": protected, "removed": removed,
        "bytes_freed": bytes_freed, "errors": errors,
        "retained_latest": len(plan["retained_latest"]),
        "retained_daily": len(plan["retained_daily"]),
        "removed_names": removed_names,
        "removed_reasons": removed_reasons,
        "stopped": bool(plan.get("stopped")) or _stop_requested(stop_requested),
    }


def storage_inventory(
    runtime_root: Path,
    backup_root: Path,
    export_root: Path,
    *,
    program_root: Path | None = None,
    settings: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Classify managed and protected storage without changing any file."""
    runtime = Path(runtime_root).resolve()
    backups = Path(backup_root).resolve()
    exports = Path(export_root).resolve()
    for root in (runtime, backups, exports):
        root.mkdir(parents=True, exist_ok=True)
    runtime_entries = list(runtime.iterdir())
    backup_files = [path for path in backups.iterdir() if path.is_file()]
    export_files = [path for path in exports.iterdir() if path.is_file()]
    rollback_entries = _rollback_entries(_rollback_roots(runtime, program_root))
    policy = managed_storage_policy(settings)
    rollback_plan = _rollback_retention_plan(
        rollback_entries,
        keep_count=policy["rollback_keep_count"],
        keep_days=policy["rollback_keep_days"],
        now=time.time() if now is None else float(now),
    )
    # The structural validator walks every file to reject links and databases.
    # Re-running it through ``_rollback_category`` made a storage inventory scan
    # the same large PyInstaller trees twice on a mechanical disk.  The
    # retention plan already owns the validated set, so size that exact set once
    # and reuse the byte counts below.
    rollback_sizes = {
        path: _directory_size(path)
        for path, _root in rollback_plan["valid"]
    }
    rollback = {
        "count": len(rollback_plan["valid"]),
        "bytes": sum(rollback_sizes.values()),
    }
    removable = list(rollback_plan["removable"])
    rollback.update({
        "cleanup_candidates": len(removable),
        "cleanup_bytes": sum(
            rollback_sizes.get(path, 0) for path, _root, _reason in removable
        ),
        "retained_latest": len(rollback_plan["retained_latest"]),
        "retained_daily": len(rollback_plan["retained_daily"]),
        "protected": int(rollback_plan["protected"]),
        "cleanup_names": [path.name for path, _root, _reason in removable],
    })
    migration = _category(runtime_entries, _valid_migration_directory, runtime)
    diagnostic = _category(export_files, _valid_diagnostic_file, exports)
    automatic_plan = automatic_backup_retention_plan(
        backups,
        keep_count=policy["backup_keep_count"],
        keep_days=policy["backup_keep_days"],
        now=time.time() if now is None else float(now),
    )
    automatic = list(automatic_plan["valid"])
    automatic_sidecar_plan = automatic_backup_sidecar_cleanup_plan(backups)
    automatic_sidecars = [
        path for path, _reason in automatic_sidecar_plan["removable"]
    ]
    deployment_plan = deployment_backup_retention_plan(
        backups,
        keep_count=policy["deployment_backup_keep_count"],
        keep_days=policy["deployment_backup_keep_days"],
        now=time.time() if now is None else float(now),
    )
    deployment = list(deployment_plan["valid"])
    deployment_sidecar_plan = deployment_backup_sidecar_cleanup_plan(backups)
    deployment_sidecars = [
        path for path, _reason in deployment_sidecar_plan["removable"]
    ]
    removable_sidecars = [*automatic_sidecars, *deployment_sidecars]
    protected_backups = [
        path for path in backup_files
        if (
            path not in automatic
            and path not in deployment
            and path not in removable_sidecars
        )
    ]
    protected_exports = [path for path in export_files if not _valid_diagnostic_file(path, exports)]
    return {
        "automatic_backups": {
            "count": len(automatic),
            "bytes": (
                sum(path.stat().st_size for path in automatic)
                + sum(path.stat().st_size for path in automatic_sidecars)
            ),
            "cleanup_candidates": len(automatic_plan["removable"]),
            "cleanup_bytes": sum(
                path.stat().st_size for path, _reason in automatic_plan["removable"]
            ),
            "total_cleanup_bytes": (
                sum(path.stat().st_size for path, _reason in automatic_plan["removable"])
                + sum(path.stat().st_size for path in automatic_sidecars)
            ),
            "sidecar_cleanup_candidates": len(automatic_sidecars),
            "sidecar_cleanup_bytes": sum(path.stat().st_size for path in automatic_sidecars),
            "protected_sidecars": int(automatic_sidecar_plan["protected"]),
            "retained_latest": len(automatic_plan["retained_latest"]),
            "retained_daily": len(automatic_plan["retained_daily"]),
            "cleanup_names": [
                path.name for path, _reason in automatic_plan["removable"]
            ],
        },
        "deployment_backups": {
            "count": len(deployment),
            "bytes": (
                sum(path.stat().st_size for path in deployment)
                + sum(path.stat().st_size for path in deployment_sidecars)
            ),
            "cleanup_candidates": len(deployment_plan["removable"]),
            "cleanup_bytes": sum(
                path.stat().st_size for path, _reason in deployment_plan["removable"]
            ),
            "total_cleanup_bytes": (
                sum(path.stat().st_size for path, _reason in deployment_plan["removable"])
                + sum(path.stat().st_size for path in deployment_sidecars)
            ),
            "sidecar_cleanup_candidates": len(deployment_sidecars),
            "sidecar_cleanup_bytes": sum(
                path.stat().st_size for path in deployment_sidecars
            ),
            "protected_sidecars": int(deployment_sidecar_plan["protected"]),
            "retained_latest": len(deployment_plan["retained_latest"]),
            "retained_daily": len(deployment_plan["retained_daily"]),
            "protected_matching": int(deployment_plan["protected_matching"]),
            "cleanup_names": [
                path.name for path, _reason in deployment_plan["removable"]
            ],
        },
        "protected_backups": {
            "count": len(protected_backups),
            "bytes": sum(path.stat().st_size for path in protected_backups),
        },
        "rollback_directories": rollback,
        "migration_directories": migration,
        "diagnostic_exports": diagnostic,
        "protected_exports": {
            "count": len(protected_exports),
            "bytes": sum(path.stat().st_size for path in protected_exports),
        },
    }


def cleanup_managed_storage(
    runtime_root: Path,
    export_root: Path,
    *,
    settings: dict[str, Any] | None = None,
    now: float | None = None,
    current_runtime: Path | None = None,
    program_root: Path | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Prune only validated, app-generated caches and redacted diagnostics.

    Manual backups, task packages, CSV/PNG exports, the live database and any
    unrecognised file are outside this function's destructive boundary.
    """
    timestamp = time.time() if now is None else float(now)
    runtime = Path(runtime_root).resolve()
    exports = Path(export_root).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    exports.mkdir(parents=True, exist_ok=True)
    policy = managed_storage_policy(settings)
    runtime_entries = list(runtime.iterdir())
    export_files = [path for path in exports.iterdir() if path.is_file()]
    cache = cleanup_runtime_cache(
        runtime,
        now=timestamp,
        current_runtime=current_runtime,
        stop_requested=stop_requested,
    )
    rollback = _prune_rollback_entries(
        (
            [] if _stop_requested(stop_requested)
            else _rollback_entries(_rollback_roots(runtime, program_root))
        ),
        keep_count=policy["rollback_keep_count"],
        keep_days=policy["rollback_keep_days"],
        now=timestamp,
        stop_requested=stop_requested,
    )
    migration = _prune_paths(
        runtime_entries, root=runtime,
        keep_count=policy["migration_keep_count"],
        keep_days=policy["migration_keep_days"],
        validator=_valid_migration_directory, now=timestamp,
        # Inventory once created empty WAL/SHM files and refreshed the folder
        # timestamp.  The snapshot file is the authoritative age evidence.
        mtime_getter=lambda path: (path / "migration-copy.sqlite3").stat().st_mtime,
        stop_requested=stop_requested,
    )
    diagnostics = _prune_paths(
        export_files, root=exports,
        keep_count=policy["diagnostic_keep_count"],
        keep_days=policy["diagnostic_keep_days"],
        validator=_valid_diagnostic_file, now=timestamp,
        stop_requested=stop_requested,
    )
    return {
        "policy": policy, "runtime_cache": cache,
        "rollback_directories": rollback,
        "migration_directories": migration,
        "diagnostic_exports": diagnostics,
        "removed": (
            int(cache.get("removed_directories") or 0)
            + int(cache.get("removed_temporary_files") or 0)
            + rollback["removed"] + migration["removed"] + diagnostics["removed"]
        ),
        "bytes_freed": (
            int(cache.get("bytes_freed") or 0)
            + rollback["bytes_freed"] + migration["bytes_freed"]
            + diagnostics["bytes_freed"]
        ),
        "stopped": (
            bool(cache.get("stopped"))
            or bool(rollback.get("stopped"))
            or bool(migration.get("stopped"))
            or bool(diagnostics.get("stopped"))
            or _stop_requested(stop_requested)
        ),
    }


def cleanup_runtime_cache(
    root: Path,
    *,
    min_age_seconds: float = 12 * 3600,
    temporary_file_age_seconds: float = 24 * 3600,
    now: float | None = None,
    current_runtime: Path | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Remove only SectorRadar's proven-stale PyInstaller/runtime leftovers.

    ``root`` must be the dedicated ``data/runtime`` directory.  Only direct
    children named exactly ``_MEI`` plus digits are eligible; junctions,
    symlinks, recent folders and the current PyInstaller directory are always
    skipped.  DeepSeek request/stream ``*.tmp`` files have a separate age gate.

    The application holds its single-instance mutex before scheduling this
    cleanup.  Keeping the path checks here as well makes the destructive scope
    independently testable and prevents an unexpected path from widening it.
    """
    timestamp = time.time() if now is None else float(now)
    requested_root = Path(root).expanduser()
    requested_root.mkdir(parents=True, exist_ok=True)
    resolved_root = requested_root.resolve()
    active = current_runtime
    if active is None and getattr(sys, "_MEIPASS", None):
        active = Path(sys._MEIPASS)
    try:
        active_resolved = Path(active).resolve() if active is not None else None
    except OSError:
        active_resolved = None

    result: dict[str, Any] = {
        "root": str(resolved_root),
        "removed_directories": 0,
        "removed_temporary_files": 0,
        "bytes_freed": 0,
        "skipped_active": 0,
        "skipped_recent": 0,
        "skipped_unsafe": 0,
        "errors": [],
        "stopped": False,
    }
    try:
        entries = list(resolved_root.iterdir())
    except OSError as exc:
        result["errors"].append(f"无法读取运行缓存目录：{exc}")
        return result

    for entry in entries:
        if _stop_requested(stop_requested):
            result["stopped"] = True
            return result
        if not _MEI_NAME.fullmatch(entry.name):
            continue
        if not entry.is_dir() or _is_reparse_point(entry):
            result["skipped_unsafe"] += 1
            continue
        try:
            resolved = entry.resolve(strict=True)
            # This is the destructive boundary: an eligible folder must remain
            # a direct child of the exact dedicated runtime directory.
            if resolved.parent != resolved_root:
                result["skipped_unsafe"] += 1
                continue
            if active_resolved is not None and resolved == active_resolved:
                result["skipped_active"] += 1
                continue
            if timestamp - entry.stat().st_mtime < max(0.0, min_age_seconds):
                result["skipped_recent"] += 1
                continue
            if _contains_reparse_point(resolved, stop_requested):
                if _stop_requested(stop_requested):
                    result["stopped"] = True
                    return result
                result["skipped_unsafe"] += 1
                continue
            size = _directory_size(resolved, stop_requested)
            if _stop_requested(stop_requested):
                result["stopped"] = True
                return result
            shutil.rmtree(resolved)
            result["removed_directories"] += 1
            result["bytes_freed"] += size
        except OSError as exc:
            result["errors"].append(f"{entry.name}: {exc}")

    deepseek_root = resolved_root / "deepseek"
    if deepseek_root.is_dir() and not _is_reparse_point(deepseek_root):
        for path in deepseek_root.glob("*.tmp"):
            if _stop_requested(stop_requested):
                result["stopped"] = True
                return result
            try:
                resolved = path.resolve(strict=True)
                if resolved.parent != deepseek_root.resolve() or not path.is_file():
                    result["skipped_unsafe"] += 1
                    continue
                if timestamp - path.stat().st_mtime < max(
                    0.0, temporary_file_age_seconds
                ):
                    result["skipped_recent"] += 1
                    continue
                size = path.stat().st_size
                path.unlink()
                result["removed_temporary_files"] += 1
                result["bytes_freed"] += size
            except OSError as exc:
                result["errors"].append(f"{path.name}: {exc}")
    result["stopped"] = _stop_requested(stop_requested)
    return result
