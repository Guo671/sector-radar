from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import local_now  # noqa: E402
from app.database import Database, SCHEMA_VERSION  # noqa: E402


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    return value


def business_digests(path: Path) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(path)
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name<>'settings' ORDER BY name"
            )
        ]
        result: dict[str, dict[str, Any]] = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})")
            ]
            rows = connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid").fetchall()
            payload = json.dumps(
                [[json_value(value) for value in row] for row in rows],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            result[table] = {
                "rows": len(rows),
                "columns": columns,
                "sha256": hashlib.sha256(payload).hexdigest().upper(),
            }
        return result
    finally:
        connection.close()


def projected_row_digest(path: Path, table: str, columns: list[str]) -> str:
    """Hash old columns after a schema-only migration.

    Adding a defaulted audit column changes a table's full shape, but it must
    not change any value that existed before the migration.  Comparing the
    post-migration projection with the original row digest proves that
    distinction instead of incorrectly treating every safe ADD COLUMN as a
    business-data rewrite.
    """
    connection = sqlite3.connect(path)
    try:
        quoted_table = '"' + table.replace('"', '""') + '"'
        quoted_columns = ",".join(
            '"' + column.replace('"', '""') + '"' for column in columns
        )
        rows = connection.execute(
            f"SELECT {quoted_columns} FROM {quoted_table} ORDER BY rowid"
        ).fetchall()
        payload = json.dumps(
            [[json_value(value) for value in row] for row in rows],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest().upper()
    finally:
        connection.close()


def settings(path: Path) -> dict[str, str]:
    connection = sqlite3.connect(path)
    try:
        return {
            str(key): str(value)
            for key, value in connection.execute("SELECT key,value FROM settings")
        }
    finally:
        connection.close()


def integrity(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate a consistent temporary copy and prove business data did not change.",
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    formal = args.database.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    formal_before = file_sha256(formal)
    report: dict[str, Any] = {
        "status": "FAILED",
        "checked_at": local_now().isoformat(timespec="seconds"),
        "formal_database": str(formal),
        "formal_hash_before": formal_before,
        "target_schema": SCHEMA_VERSION,
    }
    temporary_deleted = False
    try:
        with tempfile.TemporaryDirectory(prefix="sector-radar-migration-") as folder:
            temporary = Path(folder) / "migration.sqlite3"
            source = sqlite3.connect(f"file:{formal.as_posix()}?mode=ro", uri=True)
            destination = sqlite3.connect(temporary)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()

            before_integrity = integrity(temporary)
            before_settings = settings(temporary)
            before_business = business_digests(temporary)
            old_triplet = {
                "pro_input_price_cny_per_million": "3.15",
                "pro_cached_input_price_cny_per_million": "0.03",
                "pro_output_price_cny_per_million": "6.30",
            }
            prices_owned_by_software = all(
                before_settings.get(key) == value for key, value in old_triplet.items()
            )

            Database(temporary)

            after_integrity = integrity(temporary)
            after_settings = settings(temporary)
            after_business = business_digests(temporary)
            source_schema = int(before_settings.get("schema_version") or 0)
            changed_settings = {
                key: {"before": before_settings.get(key), "after": after_settings.get(key)}
                for key in sorted(set(before_settings) | set(after_settings))
                if before_settings.get(key) != after_settings.get(key)
            }
            expected_keys = {"schema_version"} if source_schema < SCHEMA_VERSION else set()
            if source_schema < 37:
                expected_keys.update({
                    "deepseek_pricing_revision",
                    "deepseek_pricing_source_url",
                    "migration_v37_official_pro_prices_updated",
                    "fund_execution_refresh_workers",
                })
                if prices_owned_by_software:
                    expected_keys.update(old_triplet)
            unexpected = sorted(set(changed_settings) - expected_keys)
            missing_expected = sorted(
                key for key in ({"schema_version"} if source_schema < SCHEMA_VERSION else set())
                if before_settings.get(key) == after_settings.get(key)
            )
            business_changed = sorted(
                table for table in set(before_business) | set(after_business)
                if before_business.get(table) != after_business.get(table)
            )
            expected_schema_changes: dict[str, Any] = {}
            if source_schema < 38 <= SCHEMA_VERSION and "recurring_occurrences" in business_changed:
                table = "recurring_occurrences"
                old_columns = list(before_business[table]["columns"])
                new_columns = list(after_business[table]["columns"])
                expected_added = {
                    "deduction_confirmed",
                    "deduction_confirmed_at",
                    "deduction_confirmation_source",
                }
                added = set(new_columns) - set(old_columns)
                old_values_unchanged = (
                    projected_row_digest(temporary, table, old_columns)
                    == before_business[table]["sha256"]
                )
                connection = sqlite3.connect(temporary)
                try:
                    non_default = int(connection.execute(
                        """SELECT COUNT(*) FROM recurring_occurrences
                             WHERE deduction_confirmed<>0 OR deduction_confirmed_at<>''
                                OR deduction_confirmation_source<>''"""
                    ).fetchone()[0])
                finally:
                    connection.close()
                schema_change_safe = (
                    added == expected_added
                    and len(new_columns) == len(old_columns) + len(expected_added)
                    and old_values_unchanged
                    and non_default == 0
                )
                expected_schema_changes[table] = {
                    "added_columns": sorted(added),
                    "old_values_unchanged": old_values_unchanged,
                    "new_non_default_rows": non_default,
                    "safe": schema_change_safe,
                }
                if schema_change_safe:
                    business_changed.remove(table)
            report.update({
                "source_schema": str(source_schema),
                "migrated_schema": after_settings.get("schema_version"),
                "prices_owned_by_software": prices_owned_by_software,
                "integrity_before": before_integrity,
                "integrity_after": after_integrity,
                "changed_settings": changed_settings,
                "unexpected_setting_changes": unexpected,
                "missing_expected_setting_changes": missing_expected,
                "business_table_count": len(before_business),
                "business_tables_changed": business_changed,
                "expected_schema_table_changes": expected_schema_changes,
                "business_tables_unchanged": not business_changed,
            })
        temporary_deleted = not temporary.exists()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"

    formal_after = file_sha256(formal)
    report.update({
        "temporary_database_deleted": temporary_deleted,
        "formal_hash_after": formal_after,
        "formal_unchanged": formal_after == formal_before,
    })
    if (
        report.get("integrity_before") == "ok"
        and report.get("integrity_after") == "ok"
        and str(report.get("migrated_schema")) == str(SCHEMA_VERSION)
        and report.get("business_tables_unchanged") is True
        and not report.get("unexpected_setting_changes")
        and not report.get("missing_expected_setting_changes")
        and temporary_deleted
        and report["formal_unchanged"]
    ):
        report["status"] = "PASSED"
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
