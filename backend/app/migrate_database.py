from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .database import Database


PERSISTENT_TABLES = (
    "users",
    "local_models",
    "downloads",
    "runtime_jobs",
    "collections",
    "saved_models",
    "collection_items",
    "owned_repositories",
    "download_schedule",
    "hardware_rigs",
    "hardware_components",
    "oidc_identities",
)
EPHEMERAL_TABLES = ("sessions", "oidc_login_states")
EXPECTED_TABLES = frozenset((*PERSISTENT_TABLES, *EPHEMERAL_TABLES))


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    backup_path: Path
    row_counts: dict[str, int]
    skipped_row_counts: dict[str, int]


def _sqlite_backup(source_path: Path, backup_path: Path) -> None:
    source = source_path.expanduser().resolve()
    backup = backup_path.expanduser().resolve()
    if not source.is_file():
        raise MigrationError(f"SQLite source does not exist: {source}")
    if source == backup:
        raise MigrationError("The backup path must differ from the SQLite source path.")

    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise MigrationError(f"Refusing to overwrite existing backup: {backup}") from error
    os.close(descriptor)

    try:
        source_uri = f"{source.as_uri()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source_connection:
            source_connection.execute("PRAGMA query_only=ON")
            with sqlite3.connect(backup) as backup_connection:
                source_connection.backup(backup_connection)
    except Exception:
        backup.unlink(missing_ok=True)
        raise


def _table_names(connection: Any, backend: str) -> set[str]:
    if backend == "sqlite":
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT table_name AS name
            FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
            """
        ).fetchall()
    return {str(row["name"]) for row in rows}


def _columns(connection: Any, backend: str, table: str) -> tuple[str, ...]:
    if backend == "sqlite":
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return tuple(str(row["name"]) for row in rows)
    rows = connection.execute(
        """
        SELECT column_name AS name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = ?
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return tuple(str(row["name"]) for row in rows)


def _rows(connection: Any, table: str, columns: Sequence[str]) -> list[tuple[Any, ...]]:
    selected = ", ".join(columns)
    return [
        tuple(row[column] for column in columns)
        for row in connection.execute(f"SELECT {selected} FROM {table}").fetchall()
    ]


def _canonical_rows(rows: Sequence[Sequence[Any]]) -> list[str]:
    return sorted(
        json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        for row in rows
    )


def _assert_expected_tables(connection: Any, backend: str) -> None:
    actual = _table_names(connection, backend)
    if actual != EXPECTED_TABLES:
        missing = sorted(EXPECTED_TABLES - actual)
        unexpected = sorted(actual - EXPECTED_TABLES)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        raise MigrationError(
            f"{backend} schema does not match this HuggingHack release "
            f"({'; '.join(details)})."
        )


def _schedule_is_initial_seed(connection: Any) -> bool:
    rows = connection.execute(
        """
        SELECT id, enabled, timezone, weekdays_json, start_time, end_time, updated_by
        FROM download_schedule
        """
    ).fetchall()
    if len(rows) != 1:
        return False
    row = rows[0]
    return (
        row["id"] == 1
        and row["enabled"] == 0
        and row["timezone"] == "UTC"
        and row["weekdays_json"] == "[]"
        and row["start_time"] == "00:00"
        and row["end_time"] == "06:00"
        and row["updated_by"] is None
    )


def _assert_pristine_target(connection: Any) -> None:
    actual = _table_names(connection, "postgresql")
    unexpected = actual - EXPECTED_TABLES
    if unexpected:
        raise MigrationError(
            "Refusing to use a PostgreSQL target with unexpected tables: "
            + ", ".join(sorted(unexpected))
        )

    populated = []
    for table in sorted(actual):
        row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        count = int(row["count"])
        if count == 0:
            continue
        if table == "download_schedule" and _schedule_is_initial_seed(connection):
            continue
        populated.append(f"{table} ({count})")
    if populated:
        raise MigrationError(
            "Refusing to migrate into a PostgreSQL target containing data: "
            + ", ".join(populated)
        )


def _check_sqlite_snapshot(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if [str(row[0]) for row in integrity] != ["ok"]:
        raise MigrationError("The SQLite backup failed its integrity check.")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise MigrationError(
            f"The SQLite backup has {len(violations)} foreign-key violation(s)."
        )


def migrate_sqlite_to_postgres(
    source_path: Path,
    postgres_url: str,
    backup_path: Path,
) -> MigrationResult:
    target = postgres_url.strip()
    if not target.startswith(("postgresql://", "postgres://")):
        raise MigrationError("DATABASE_URL must be a PostgreSQL connection URL.")

    source = source_path.expanduser().resolve()
    backup = backup_path.expanduser().resolve()
    _sqlite_backup(source, backup)

    target_database = Database(target)
    with target_database.connect() as target_connection:
        _assert_pristine_target(target_connection)

    with tempfile.TemporaryDirectory(prefix="hugginghack-migration-") as directory:
        working_path = Path(directory) / "source.sqlite3"
        shutil.copy2(backup, working_path)
        source_database = Database(working_path)
        source_database.initialize()

        with source_database.connect() as source_connection:
            _check_sqlite_snapshot(source_connection)
            _assert_expected_tables(source_connection, "sqlite")
            source_columns = {
                table: _columns(source_connection, "sqlite", table)
                for table in PERSISTENT_TABLES
            }
            source_rows = {
                table: _rows(source_connection, table, source_columns[table])
                for table in PERSISTENT_TABLES
            }
            skipped_row_counts = {
                table: int(
                    source_connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()["count"]
                )
                for table in EPHEMERAL_TABLES
            }

        target_database.initialize()
        with target_database.connect() as target_connection:
            _assert_expected_tables(target_connection, "postgresql")
            _assert_pristine_target(target_connection)
            target_connection.execute("DELETE FROM download_schedule")

            for table in PERSISTENT_TABLES:
                columns = source_columns[table]
                target_columns = _columns(target_connection, "postgresql", table)
                if set(columns) != set(target_columns):
                    raise MigrationError(
                        f"Column mismatch for {table}: SQLite has {sorted(columns)}, "
                        f"PostgreSQL has {sorted(target_columns)}."
                    )
                rows = source_rows[table]
                if rows:
                    column_list = ", ".join(columns)
                    placeholders = ", ".join("?" for _ in columns)
                    target_connection.executemany(
                        f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})",
                        rows,
                    )

            for table in PERSISTENT_TABLES:
                migrated_rows = _rows(
                    target_connection,
                    table,
                    source_columns[table],
                )
                if _canonical_rows(migrated_rows) != _canonical_rows(source_rows[table]):
                    raise MigrationError(f"PostgreSQL verification failed for {table}.")

            for table in EPHEMERAL_TABLES:
                count = int(
                    target_connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()["count"]
                )
                if count:
                    raise MigrationError(f"Ephemeral table {table} is not empty.")

    return MigrationResult(
        backup_path=backup,
        row_counts={table: len(source_rows[table]) for table in PERSISTENT_TABLES},
        skipped_row_counts=skipped_row_counts,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Copy a HuggingHack SQLite database into an empty PostgreSQL database. "
            "The source is never modified."
        )
    )
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to hugginghack.sqlite3",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        required=True,
        help="New path for the verified pre-migration SQLite backup",
    )
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    database_url = (os.getenv("DATABASE_URL") or "").strip()
    try:
        result = migrate_sqlite_to_postgres(
            arguments.source,
            database_url,
            arguments.backup,
        )
    except MigrationError as error:
        parser.error(str(error))

    print(f"Backup: {result.backup_path}")
    print("Migrated persistent rows:")
    for table, count in result.row_counts.items():
        print(f"  {table}: {count}")
    print("Skipped ephemeral rows (sessions must sign in again):")
    for table, count in result.skipped_row_counts.items():
        print(f"  {table}: {count}")
    print("Migration committed and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
