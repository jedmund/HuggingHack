import os
import sqlite3
from pathlib import Path

import pytest

from app.database import Database
from app.migrate_database import (
    MigrationError,
    _sqlite_backup,
    migrate_sqlite_to_postgres,
)


MIGRATION_POSTGRES_URL = os.getenv("TEST_POSTGRES_MIGRATION_URL")
TIMESTAMP = "2026-07-24T12:00:00+00:00"


def _create_legacy_sqlite(path: Path) -> None:
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, username, display_name, password_hash, role, created_at, updated_at
            ) VALUES ('local', 'local', 'Local administrator', 'disabled', 'admin', ?, ?)
            """,
            (TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO sessions (
                token_hash, user_id, csrf_token, created_at, expires_at
            ) VALUES ('old-session', 'local', 'csrf', ?, '2099-07-24T12:00:00+00:00')
            """,
            (TIMESTAMP,),
        )
        connection.execute(
            """
            INSERT INTO local_models (
                repo_id, relative_path, size_bytes, file_count, modified_at,
                downloaded_at, revision, sha, pipeline_tag, library_name, license,
                tags_json, config_json, source_url, managed, storage_backend, cached,
                remote_uri
            ) VALUES (
                'owner/model', 'owner/model', ?, 2, ?, ?, 'main', 'abc123',
                'text-generation', 'transformers', 'mit', '["legacy"]',
                '{"model_type":"tiny"}', NULL, 1, 'filesystem', 1, NULL
            )
            """,
            (5 * 1024**3, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO downloads (
                id, repo_id, revision, status, total_bytes, downloaded_bytes,
                progress, speed_bps, error, target_path, payload_json,
                metadata_json, created_at, updated_at, completed_at, user_id
            ) VALUES (
                'download-1', 'owner/model', 'main', 'complete', ?, ?, 100, 0,
                NULL, '/models/owner/model', '{"mode":"full"}', '{"legacy":true}',
                ?, ?, ?, 'local'
            )
            """,
            (5 * 1024**3, 5 * 1024**3, TIMESTAMP, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO runtime_jobs (
                id, target_id, target_name, target_kind, repo_id, runtime_model_name,
                source_file, status, total_bytes, processed_bytes, progress, message,
                error, created_at, updated_at, completed_at, user_id
            ) VALUES (
                'runtime-1', 'ollama', 'Ollama', 'ollama', 'owner/model', 'model',
                NULL, 'ready', ?, ?, 100, 'Ready', NULL, ?, ?, ?, 'local'
            )
            """,
            (5 * 1024**3, 5 * 1024**3, TIMESTAMP, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO collections (
                id, user_id, name, description, created_at, updated_at
            ) VALUES ('collection-1', 'local', 'Keep', 'Legacy collection', ?, ?)
            """,
            (TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO saved_models (
                id, user_id, repo_id, note, metadata_json, created_at, updated_at
            ) VALUES (
                'saved-1', 'local', 'owner/model', 'Important', '{}', ?, ?
            )
            """,
            (TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO collection_items (collection_id, saved_model_id, created_at)
            VALUES ('collection-1', 'saved-1', ?)
            """,
            (TIMESTAMP,),
        )
        connection.execute(
            """
            INSERT INTO owned_repositories (
                id, owner_id, repo_id, description, visibility, status,
                created_at, updated_at
            ) VALUES (
                'owned-1', 'local', 'local/upload', 'Legacy upload', 'private',
                'ready', ?, ?
            )
            """,
            (TIMESTAMP, TIMESTAMP),
        )

    # Match the pre-download-controls/Pocket ID schema used by the existing instance.
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE oidc_login_states;
            DROP TABLE oidc_identities;
            DROP TABLE hardware_components;
            DROP TABLE hardware_rigs;
            DROP TABLE download_schedule;
            ALTER TABLE downloads DROP COLUMN resolved_revision;
            ALTER TABLE downloads DROP COLUMN staging_path;
            ALTER TABLE downloads DROP COLUMN pause_reason;
            ALTER TABLE downloads DROP COLUMN cleaned_at;
            """
        )


def _clear_migration_target(database: Database) -> None:
    with database.connect() as connection:
        for table in (
            "oidc_identities",
            "hardware_components",
            "hardware_rigs",
            "download_schedule",
            "owned_repositories",
            "collection_items",
            "saved_models",
            "collections",
            "runtime_jobs",
            "downloads",
            "local_models",
            "oidc_login_states",
            "sessions",
            "users",
        ):
            connection.execute(f"DELETE FROM {table}")
    database.initialize()


def test_sqlite_backup_is_consistent_and_never_overwritten(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    _create_legacy_sqlite(source)

    _sqlite_backup(source, backup)

    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT username FROM users").fetchone()[0] == "local"
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    with pytest.raises(MigrationError, match="overwrite"):
        _sqlite_backup(source, backup)


@pytest.mark.skipif(
    not MIGRATION_POSTGRES_URL,
    reason="TEST_POSTGRES_MIGRATION_URL is not configured",
)
def test_sqlite_to_postgresql_migration_upgrades_and_verifies_legacy_data(tmp_path: Path):
    source = tmp_path / "hugginghack.sqlite3"
    backup = tmp_path / "hugginghack.pre-postgres.sqlite3"
    refusal_backup = tmp_path / "refused.sqlite3"
    _create_legacy_sqlite(source)
    target = Database(MIGRATION_POSTGRES_URL or "")
    target.initialize()
    _clear_migration_target(target)

    try:
        result = migrate_sqlite_to_postgres(
            source,
            MIGRATION_POSTGRES_URL or "",
            backup,
        )

        assert result.row_counts["users"] == 1
        assert result.row_counts["downloads"] == 1
        assert result.row_counts["saved_models"] == 1
        assert result.skipped_row_counts == {"sessions": 1, "oidc_login_states": 0}
        assert backup.is_file()

        with sqlite3.connect(source) as connection:
            source_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            download_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(downloads)").fetchall()
            }
        assert "hardware_rigs" not in source_tables
        assert "resolved_revision" not in download_columns

        with target.connect() as connection:
            assert connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()[
                "count"
            ] == 1
            assert connection.execute(
                "SELECT size_bytes FROM local_models WHERE repo_id = ?",
                ("owner/model",),
            ).fetchone()["size_bytes"] == 5 * 1024**3
            download = connection.execute(
                "SELECT * FROM downloads WHERE id = ?", ("download-1",)
            ).fetchone()
            assert download["user_id"] == "local"
            assert download["resolved_revision"] is None
            assert connection.execute(
                "SELECT COUNT(*) AS count FROM collection_items"
            ).fetchone()["count"] == 1
            assert connection.execute(
                "SELECT COUNT(*) AS count FROM owned_repositories"
            ).fetchone()["count"] == 1
            assert connection.execute(
                "SELECT COUNT(*) AS count FROM sessions"
            ).fetchone()["count"] == 0

        with pytest.raises(MigrationError, match="containing data"):
            migrate_sqlite_to_postgres(
                source,
                MIGRATION_POSTGRES_URL or "",
                refusal_backup,
            )
    finally:
        _clear_migration_target(target)
