import json
import sqlite3
from pathlib import Path

import pytest

from app.auth import AuthService, verify_password
from app.config import Settings, repository_path, validate_repo_id
from app.database import Database
from app.downloads import DownloadManager
from app.indexer import LocalModelIndexer
from app.uploads import UploadManager, validate_upload_path


def test_repo_id_validation_rejects_path_traversal():
    assert validate_repo_id("google/gemma-2b") == "google/gemma-2b"
    for value in ("../etc", "owner/../../secret", "single-name", "/absolute/model", "owner/model/extra"):
        with pytest.raises(ValueError):
            validate_repo_id(value)


def test_indexer_discovers_manually_copied_model(tmp_path: Path):
    storage = tmp_path / "models"
    data = tmp_path / "data"
    model = storage / "acme" / "tiny-model"
    model.mkdir(parents=True)
    (model / "config.json").write_text(
        json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"]}),
        encoding="utf-8",
    )
    (model / "model.safetensors").write_bytes(b"safe-weights")

    settings = Settings(model_storage=storage.resolve(), data_dir=data.resolve())
    database = Database(settings.database_path)
    database.initialize()
    indexer = LocalModelIndexer(settings, database)

    result = indexer.scan()

    assert result["count"] == 1
    indexed = database.get_local_model("acme/tiny-model")
    assert indexed is not None
    assert indexed["relative_path"] == "acme/tiny-model"
    assert indexed["config"]["model_type"] == "llama"
    assert indexed["managed"] is False


def test_indexer_marks_pickle_compatible_files(tmp_path: Path):
    storage = tmp_path / "models"
    data = tmp_path / "data"
    model = storage / "unsafe" / "legacy"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "pytorch_model.bin").write_bytes(b"not-executed")

    settings = Settings(model_storage=storage.resolve(), data_dir=data.resolve())
    database = Database(settings.database_path)
    database.initialize()
    indexer = LocalModelIndexer(settings, database)
    indexer.scan()

    details = indexer.files_for_model("unsafe/legacy")
    assert details is not None
    assert details["unsafe_file_count"] == 1
    assert details["files"][0]["unsafe_serialization"] is True


def test_cancelled_download_keeps_partial_files_and_resume_metadata(tmp_path: Path):
    storage = tmp_path / "models"
    data = tmp_path / "data"
    target = storage / "acme" / "large-model"
    target.mkdir(parents=True)
    (target / "weights.safetensors.incomplete").write_bytes(b"partial-data")

    settings = Settings(model_storage=storage.resolve(), data_dir=data.resolve())
    database = Database(settings.database_path)
    database.initialize()
    created = "2026-07-23T12:00:00+00:00"
    database.create_download(
        {
            "id": "cancel-me",
            "repo_id": "acme/large-model",
            "revision": "main",
            "status": "downloading",
            "total_bytes": 100,
            "downloaded_bytes": 0,
            "progress": 0,
            "speed_bps": 0,
            "error": None,
            "target_path": str(target),
            "payload_json": json.dumps({"mode": "full"}),
            "metadata_json": "{}",
            "created_at": created,
            "updated_at": created,
            "completed_at": None,
        }
    )
    manager = DownloadManager(settings, database, object(), object())

    class RunningProcess:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    process = RunningProcess()
    manager._processes["cancel-me"] = process  # type: ignore[assignment]

    cancelled = manager.cancel("cancel-me")
    manager.shutdown()

    assert cancelled is not None
    assert process.terminated is True
    assert cancelled["status"] == "cancelled"
    assert cancelled["downloaded_bytes"] >= len(b"partial-data")
    assert (target / "weights.safetensors.incomplete").is_file()
    manifest = json.loads((target / ".hugginghack.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"


def test_accounts_use_scrypt_and_separate_saved_libraries(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        accounts_enabled=True,
    )
    database = Database(settings.database_path)
    database.initialize()
    service = AuthService(settings, database)

    owner = service.create_user("owner", "NAS Owner", "correct horse battery", "admin")
    member = service.create_user("member", "Model Curator", "another secure phrase", "member")

    secret = database.get_user_by_username("owner")
    assert secret is not None
    assert secret["password_hash"].startswith("scrypt$")
    assert verify_password("correct horse battery", secret["password_hash"]) is True
    assert verify_password("incorrect", secret["password_hash"]) is False
    raw_session, _ = service.create_session(owner["id"])
    service.change_password(
        owner["id"],
        "correct horse battery",
        "replacement secure phrase",
        raw_session,
    )
    changed = database.get_user_by_username("owner")
    assert changed is not None
    assert verify_password("correct horse battery", changed["password_hash"]) is False
    assert verify_password("replacement secure phrase", changed["password_hash"]) is True

    saved_at = "2026-07-24T12:00:00+00:00"
    database.save_model(
        {
            "id": "saved-owner",
            "user_id": owner["id"],
            "repo_id": "acme/private-model",
            "note": "Keep for the vision rig",
            "metadata_json": json.dumps({"pipeline_tag": "image-text-to-text"}),
            "created_at": saved_at,
            "updated_at": saved_at,
        },
        [],
    )
    assert database.saved_repo_ids(owner["id"]) == {"acme/private-model"}
    assert database.saved_repo_ids(member["id"]) == set()


def test_accounts_disabled_keeps_single_user_compatibility(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        accounts_enabled=False,
    )
    database = Database(settings.database_path)
    database.initialize()
    service = AuthService(settings, database)
    service.ensure_local_user()

    session = service.session(None)
    assert session is not None
    assert session["user"]["id"] == "local"
    assert session["user"]["role"] == "admin"
    assert "password_hash" not in session["user"]
    assert service.verify_csrf(session, None) is True


def test_database_migrates_existing_download_history(tmp_path: Path):
    database_path = tmp_path / "data" / "hugginghack.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE downloads (
                id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                revision TEXT NOT NULL,
                status TEXT NOT NULL,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                progress REAL NOT NULL DEFAULT 0,
                speed_bps REAL NOT NULL DEFAULT 0,
                error TEXT,
                target_path TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO downloads (
                id, repo_id, revision, status, created_at, updated_at
            ) VALUES ('legacy', 'acme/model', 'main', 'complete', 'now', 'now')
            """
        )

    database = Database(database_path)
    database.initialize()

    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(downloads)").fetchall()
        }
    assert "user_id" in columns
    legacy = database.get_download("legacy")
    assert legacy is not None
    assert legacy["repo_id"] == "acme/model"
    assert legacy["user_id"] is None


def test_chunked_upload_is_confined_owned_and_indexed(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    data = (tmp_path / "data").resolve()
    settings = Settings(
        model_storage=storage,
        data_dir=data,
        accounts_enabled=True,
        upload_chunk_mb=1,
        max_upload_size_gb=1,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    auth = AuthService(settings, database)
    owner = auth.create_user("owner", "Owner", "correct horse battery", "admin")
    member = auth.create_user("member", "Member", "another secure phrase", "member")
    indexer = LocalModelIndexer(settings, database)
    manager = UploadManager(settings, database, indexer)

    repository = manager.create_repository(
        owner, "tiny-upload", "Private test repository", "private"
    )
    first = manager.upload_chunk(
        repository["repo_id"], owner["id"], "config.json", 0, 22, b'{"model_type":"tiny"'
    )
    assert first == {"offset": 20, "complete": False, "path": "config.json"}
    with pytest.raises(RuntimeError):
        manager.upload_chunk(
            repository["repo_id"], owner["id"], "config.json", 0, 22, b"{}"
        )
    complete = manager.upload_chunk(
        repository["repo_id"], owner["id"], "config.json", 20, 22, b"}\n"
    )
    assert complete["complete"] is True
    empty = manager.upload_chunk(
        repository["repo_id"], owner["id"], "empty.marker", 0, 0, b""
    )
    assert empty["complete"] is True
    finalized = manager.finalize(repository["repo_id"], owner["id"])
    assert finalized["status"] == "ready"
    manifest = json.loads(
        (storage / repository["repo_id"] / ".hugginghack.json").read_text(encoding="utf-8")
    )
    assert manifest["file_count"] == 3
    assert database.get_local_model(repository["repo_id"]) is not None
    assert database.get_visible_local_model(owner["id"], repository["repo_id"]) is not None
    assert database.get_visible_local_model(member["id"], repository["repo_id"]) is None

    manager.update_repository(
        repository["repo_id"], owner["id"], "Shared test repository", "shared"
    )
    assert database.get_visible_local_model(member["id"], repository["repo_id"]) is not None
    with pytest.raises(ValueError):
        manager.delete_repository(repository["repo_id"], owner["id"], "wrong/name")
    manager.delete_repository(
        repository["repo_id"], owner["id"], repository["repo_id"]
    )
    assert not (storage / repository["repo_id"]).exists()
    assert database.get_owned_repository(repository["repo_id"]) is None

    for value in ("../secret", "/absolute/file", ".git/config", ".hugginghack.json"):
        with pytest.raises(ValueError):
            validate_upload_path(value)
