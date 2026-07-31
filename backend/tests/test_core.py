import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from authlib.jose import JsonWebKey, jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import AuthService, oidc_username, verify_password
from app.config import Settings, repository_path, validate_repo_id
from app.database import Database, _postgres_query
from app.download_schedule import validate_schedule, window_is_open
from app.downloads import DownloadManager, resolve_selection
from app.hub_service import HubService, parse_gguf_range, validate_gguf_filename, weight_groups
from app.indexer import LocalModelIndexer
from app.oidc import OIDCService
from app.runtimes import (
    RuntimeManager,
    ollama_files,
    parse_runtime_targets,
    remote_model_path,
)
from app.storage import FilesystemModelStorage, S3ModelStorage, inject_delete_objects_md5
from app.uploads import UploadManager, validate_upload_path
from app.vllm_agent import AgentSettings, VllmProcessManager


class FakeS3Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []

    def get_paginator(self, operation: str):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, *, Bucket: str, Prefix: str):
        return [
            {
                "Contents": [
                    {
                        "Key": key,
                        "Size": len(value),
                        "LastModified": "2026-07-24T12:00:00+00:00",
                    }
                    for key, value in sorted(self.objects.items())
                    if key.startswith(Prefix)
                ]
            }
        ]

    def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int):
        contents = [
            {"Key": key}
            for key in sorted(self.objects)
            if key.startswith(Prefix)
        ][:MaxKeys]
        return {"Contents": contents}

    def upload_file(self, filename: str, bucket: str, key: str, **kwargs):
        self.objects[key] = Path(filename).read_bytes()
        self.uploads.append(key)

    def download_file(self, bucket: str, key: str, filename: str, **kwargs):
        Path(filename).write_bytes(self.objects[key])

    def delete_objects(self, *, Bucket: str, Delete: dict):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {}

    def get_object(self, *, Bucket: str, Key: str):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": BytesIO(self.objects[Key])}


def test_delete_objects_md5_is_injected_without_overwriting_existing_header():
    class Request:
        body = b"<Delete><Object><Key>model.bin</Key></Object></Delete>"

        def __init__(self):
            self.headers: dict[str, str] = {}

    request = Request()
    inject_delete_objects_md5(request)
    assert request.headers["Content-MD5"] == "eJRthj36oORvbzQhz7emkQ=="

    request.headers["Content-MD5"] = "provided-by-botocore"
    inject_delete_objects_md5(request)
    assert request.headers["Content-MD5"] == "provided-by-botocore"


def test_repo_id_validation_rejects_path_traversal():
    assert validate_repo_id("google/gemma-2b") == "google/gemma-2b"
    for value in ("../etc", "owner/../../secret", "single-name", "/absolute/model", "owner/model/extra"):
        with pytest.raises(ValueError):
            validate_repo_id(value)


def test_gguf_range_validation_is_bounded_and_path_safe():
    assert validate_gguf_filename("Q4/model-00001-of-00002.gguf") == (
        "Q4/model-00001-of-00002.gguf"
    )
    assert parse_gguf_range("bytes=0-1999999") == (0, 1_999_999)

    for filename in ("../model.gguf", "/model.gguf", "folder\\..\\model.gguf", "model.bin"):
        with pytest.raises(ValueError):
            validate_gguf_filename(filename)


def test_weight_folders_resolve_all_shards_and_default_metadata():
    files = [
        {"path": "UD-Q8_K_XL/model-00001-of-00002.gguf", "size": 10},
        {"path": "UD-Q8_K_XL/model-00002-of-00002.gguf", "size": 11},
        {"path": "Q4_K_M/model.gguf", "size": 6},
        {"path": "config.json", "size": 2},
    ]
    groups = weight_groups(files, "gguf", ["gguf"])
    q8 = next(group for group in groups if group["label"] == "UD-Q8_K_XL")
    assert q8["total_bytes"] == 21
    assert q8["selection"] == [{"path": "UD-Q8_K_XL", "kind": "folder"}]

    allowed, ignored, selected = resolve_selection(
        {"files": files},
        "selection",
        [],
        [],
        {"paths": q8["selection"], "include_metadata": True},
    )
    assert ignored == []
    assert "UD-Q8_K_XL/*" in allowed
    assert {item["path"] for item in selected} == {
        "UD-Q8_K_XL/model-00001-of-00002.gguf",
        "UD-Q8_K_XL/model-00002-of-00002.gguf",
        "config.json",
    }


def test_weekly_download_window_handles_overnight_ranges():
    schedule = validate_schedule(
        {
            "enabled": True,
            "timezone": "America/Los_Angeles",
            "weekdays": [0],
            "start_time": "22:00",
            "end_time": "06:00",
        }
    )
    monday_2230 = datetime(2026, 8, 4, 5, 30, tzinfo=timezone.utc)
    tuesday_0530 = datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc)
    tuesday_0630 = datetime(2026, 8, 4, 13, 30, tzinfo=timezone.utc)
    assert window_is_open(schedule, monday_2230) is True
    assert window_is_open(schedule, tuesday_0530) is True
    assert window_is_open(schedule, tuesday_0630) is False

    with pytest.raises(ValueError, match="must differ"):
        validate_schedule({**schedule, "start_time": "06:00", "end_time": "06:00"})
    for range_header in (
        None,
        "bytes=0-1,3-4",
        "bytes=10-9",
        "bytes=0-2100000",
        "bytes=49000000-50000000",
    ):
        with pytest.raises(ValueError):
            parse_gguf_range(range_header)


def test_hub_service_proxies_only_the_requested_gguf_range(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        hf_token="read-token",
    )
    service = HubService(settings)
    service.range_client.close()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/acme/model/resolve/main/Q4/model.gguf")
        assert request.headers["Range"] == "bytes=0-3"
        assert request.headers["Authorization"] == "Bearer read-token"
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            206,
            content=b"GGUF",
            headers={"Content-Range": "bytes 0-3/100"},
        )

    service.range_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    try:
        result = service.read_gguf_range(
            "acme/model", "Q4/model.gguf", "main", "bytes=0-3"
        )
    finally:
        service.close()

    assert result["status_code"] == 206
    assert result["content"] == b"GGUF"
    assert result["headers"]["Content-Range"] == "bytes 0-3/100"
    assert result["headers"]["Cache-Control"] == "private, max-age=3600"


def test_database_target_defaults_to_sqlite_and_accepts_postgresql(tmp_path: Path):
    sqlite_settings = Settings(data_dir=(tmp_path / "data").resolve())
    assert sqlite_settings.database_target == sqlite_settings.database_path
    assert Database(sqlite_settings.database_target).backend == "sqlite"

    postgres_url = "postgresql://hugginghack:secret@postgres:5432/hugginghack"
    postgres_settings = Settings(
        data_dir=(tmp_path / "data").resolve(),
        database_url=postgres_url,
    )
    assert postgres_settings.database_target == postgres_url
    assert Database(postgres_settings.database_target).backend == "postgresql"


def test_postgresql_query_adapter_handles_positional_and_named_parameters():
    assert _postgres_query("SELECT * FROM users WHERE id = ?", ("owner",)) == (
        "SELECT * FROM users WHERE id = %s"
    )
    assert _postgres_query(
        "UPDATE users SET display_name = :display_name WHERE id = :id",
        {"display_name": "Owner", "id": "owner"},
    ) == (
        "UPDATE users SET display_name = %(display_name)s WHERE id = %(id)s"
    )


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


def test_indexer_fails_closed_for_upload_without_ownership_metadata(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    model = storage / "former-owner" / "private-model"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / ".hugginghack.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "repo_id": "former-owner/private-model",
                "source": "user-upload",
                "owner_id": "missing-user",
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
    )
    database = Database(settings.database_path)
    database.initialize()
    indexer = LocalModelIndexer(settings, database)

    result = indexer.scan()

    assert result["count"] == 0
    assert database.get_local_model("former-owner/private-model") is None


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


def test_cleanup_removes_only_staging_and_keeps_completed_model(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    data = (tmp_path / "data").resolve()
    target = storage / "acme" / "model"
    staging = storage / ".hugginghack-downloads" / "paused-job"
    target.mkdir(parents=True)
    staging.mkdir(parents=True)
    (target / "model.gguf").write_bytes(b"complete")
    (target / ".hugginghack.json").write_text(
        json.dumps({"status": "complete", "repo_id": "acme/model"}),
        encoding="utf-8",
    )
    (staging / "model.gguf.incomplete").write_bytes(b"partial")
    (staging / ".hugginghack.json").write_text(
        json.dumps({"status": "paused", "repo_id": "acme/model"}),
        encoding="utf-8",
    )
    settings = Settings(model_storage=storage, data_dir=data)
    database = Database(settings.database_path)
    database.initialize()
    created = "2026-07-23T12:00:00+00:00"
    database.create_download(
        {
            "id": "paused-job",
            "repo_id": "acme/model",
            "revision": "main",
            "status": "paused",
            "total_bytes": 100,
            "downloaded_bytes": 7,
            "progress": 7,
            "speed_bps": 0,
            "error": None,
            "target_path": str(target),
            "staging_path": str(staging),
            "payload_json": json.dumps({"mode": "full"}),
            "metadata_json": "{}",
            "created_at": created,
            "updated_at": created,
            "completed_at": None,
        }
    )
    manager = DownloadManager(settings, database, object(), object())
    cleaned = manager.cleanup("paused-job")
    manager.shutdown()

    assert cleaned is not None
    assert cleaned["status"] == "cancelled"
    assert cleaned["cleaned_at"] is not None
    assert cleaned["downloaded_bytes"] == 0
    assert not staging.exists()
    assert (target / "model.gguf").read_bytes() == b"complete"


def test_completed_download_syncs_durable_storage_before_indexing(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    indexer = LocalModelIndexer(settings, database)

    class Hub:
        @staticmethod
        def model_details(repo_id: str, revision: str):
            return {
                "total_bytes": 7,
                "files": [{"path": "model.safetensors", "size": 7}],
                "source_url": "https://huggingface.co/acme/tiny",
                "sha": "abc123",
                "pipeline_tag": "text-generation",
                "library_name": "transformers",
                "license": "mit",
                "tags": ["transformers"],
                "gated": False,
            }

    class DurableStorage(FilesystemModelStorage):
        def __init__(self, configured_settings: Settings):
            super().__init__(configured_settings)
            self.synced: list[str] = []

        def sync_repository(self, repo_id: str, root: Path):
            manifest = json.loads(
                (root / ".hugginghack.json").read_text(encoding="utf-8")
            )
            assert manifest["status"] == "complete"
            self.synced.append(repo_id)
            return "s3://model-bucket/models/acme/tiny"

    durable = DurableStorage(settings)
    manager = DownloadManager(settings, database, Hub(), indexer, durable)
    created = "2026-07-24T12:00:00+00:00"
    database.create_download(
        {
            "id": "complete-me",
            "repo_id": "acme/tiny",
            "revision": "main",
            "status": "queued",
            "total_bytes": 0,
            "downloaded_bytes": 0,
            "progress": 0,
            "speed_bps": 0,
            "error": None,
            "target_path": str(storage / "acme" / "tiny"),
            "payload_json": "{}",
            "metadata_json": "{}",
            "created_at": created,
            "updated_at": created,
            "completed_at": None,
        }
    )

    def write_snapshot(download_id, download, target, cancel_event):
        (target / "model.safetensors").write_bytes(b"weights")

    manager._download_snapshot = write_snapshot  # type: ignore[method-assign]
    manager._run("complete-me")
    manager.shutdown()

    assert durable.synced == ["acme/tiny"]
    assert database.get_download("complete-me")["status"] == "complete"
    assert database.get_local_model("acme/tiny") is not None


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


def test_auth_mode_keeps_legacy_accounts_setting_and_validates_oidc(tmp_path: Path):
    disabled = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        accounts_enabled=False,
    )
    assert disabled.effective_auth_mode == "disabled"

    oidc_settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        auth_mode="oidc",
        app_base_url="https://models.example.com",
        oidc_issuer="https://id.example.com",
        oidc_client_id="client-id",
        oidc_client_secret="client-secret",
    )
    oidc_settings.validate_auth()
    assert oidc_settings.oidc_redirect_uri == (
        "https://models.example.com/api/auth/oidc/callback"
    )
    assert oidc_settings.effective_session_ttl_hours == 12

    with pytest.raises(ValueError, match="requires"):
        Settings(auth_mode="oidc").validate_auth()


def test_oidc_provisioning_links_username_and_maps_groups(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        auth_mode="oidc",
        oidc_allowed_groups=("model-users", "model-admins"),
        oidc_admin_groups=("model-admins",),
    )
    database = Database(settings.database_path)
    database.initialize()
    service = AuthService(settings, database)
    local = service.create_user("justin", "Local owner", "correct horse battery", "admin")

    linked = service.provision_oidc_user(
        {
            "iss": "https://id.example.com",
            "sub": "pocket-user-1",
            "preferred_username": "Justin",
            "name": "Justin from Pocket ID",
            "email": "justin@example.com",
            "groups": ["model-users", "model-admins"],
        }
    )
    assert linked["id"] == local["id"]
    assert linked["display_name"] == "Justin from Pocket ID"
    assert linked["role"] == "admin"

    distinct = service.provision_oidc_user(
        {
            "iss": "https://id.example.com",
            "sub": "pocket-user-2",
            "preferred_username": "justin",
            "groups": ["model-users"],
        }
    )
    assert distinct["id"] != local["id"]
    assert distinct["username"] == "justin-2"
    assert distinct["role"] == "member"
    assert oidc_username({"sub": "abc", "preferred_username": "A User@example"}) == "a-user-example"

    with pytest.raises(PermissionError, match="groups"):
        service.provision_oidc_user(
            {
                "iss": "https://id.example.com",
                "sub": "blocked-user",
                "preferred_username": "blocked",
                "groups": ["unrelated"],
            }
        )


def test_oidc_login_state_is_single_use(tmp_path: Path):
    database = Database((tmp_path / "data.sqlite3").resolve())
    database.initialize()
    database.create_oidc_login_state(
        {
            "state_hash": "state-hash",
            "nonce": "nonce",
            "code_verifier": "verifier",
            "return_to": "/downloads",
            "created_at": "2026-07-31T12:00:00+00:00",
            "expires_at": "2026-07-31T12:10:00+00:00",
        }
    )
    assert database.consume_oidc_login_state("state-hash")["nonce"] == "nonce"
    assert database.consume_oidc_login_state("state-hash") is None


def test_oidc_authorization_uses_pkce_and_server_side_state(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        auth_mode="oidc",
        app_base_url="https://models.example.com",
        oidc_issuer="https://id.example.com",
        oidc_client_id="client-id",
        oidc_client_secret="client-secret",
    )
    database = Database(settings.database_path)
    database.initialize()
    service = OIDCService(settings, database, AuthService(settings, database))

    async def run_authorization():
        async def metadata():
            return {
                "issuer": settings.oidc_issuer,
                "authorization_endpoint": "https://id.example.com/authorize",
                "token_endpoint": "https://id.example.com/token",
                "jwks_uri": "https://id.example.com/jwks",
            }

        service.metadata = metadata  # type: ignore[method-assign]
        url = await service.authorization_url("/downloads")
        query = parse_qs(urlparse(url).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["nonce"]
        state_hash = hashlib.sha256(query["state"][0].encode()).hexdigest()
        record = database.consume_oidc_login_state(state_hash)
        assert record["return_to"] == "/downloads"
        assert record["nonce"] == query["nonce"][0]
        await service.close()

    asyncio.run(run_authorization())


def test_oidc_id_token_validation_checks_signature_audience_and_nonce(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        auth_mode="oidc",
        app_base_url="https://models.example.com",
        oidc_issuer="https://id.example.com",
        oidc_client_id="client-id",
        oidc_client_secret="client-secret",
    )
    database = Database(settings.database_path)
    database.initialize()
    service = OIDCService(settings, database, AuthService(settings, database))
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_jwk = JsonWebKey.import_key(public_pem).as_dict()
    public_jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    now = datetime.now(timezone.utc)
    encoded = jwt.encode(
        {"alg": "RS256", "kid": "test-key"},
        {
            "iss": settings.oidc_issuer,
            "sub": "pocket-user",
            "aud": settings.oidc_client_id,
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "iat": int(now.timestamp()),
            "nonce": "expected-nonce",
        },
        private_pem,
    )

    async def run_validation():
        await service._client.aclose()
        service._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"keys": [public_jwk]})
            )
        )
        metadata = {
            "jwks_uri": "https://id.example.com/jwks",
            "id_token_signing_alg_values_supported": ["RS256", "none"],
        }
        claims = await service._validate_id_token(
            encoded.decode(), metadata, "expected-nonce", None
        )
        assert claims["sub"] == "pocket-user"
        with pytest.raises(Exception):
            await service._validate_id_token(
                encoded.decode(), metadata, "wrong-nonce", None
            )
        await service.close()

    asyncio.run(run_validation())


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


def test_database_migrates_local_models_for_s3_cache_state(tmp_path: Path):
    database_path = tmp_path / "data" / "hugginghack.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE local_models (
                repo_id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                file_count INTEGER NOT NULL DEFAULT 0,
                modified_at TEXT NOT NULL,
                downloaded_at TEXT,
                revision TEXT,
                sha TEXT,
                pipeline_tag TEXT,
                library_name TEXT,
                license TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                config_json TEXT NOT NULL DEFAULT '{}',
                source_url TEXT,
                managed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO local_models (
                repo_id, relative_path, modified_at
            ) VALUES ('acme/model', 'acme/model', 'now')
            """
        )

    database = Database(database_path)
    database.initialize()

    legacy = database.get_local_model("acme/model")
    assert legacy is not None
    assert legacy["storage_backend"] == "filesystem"
    assert legacy["cached"] is True
    assert legacy["remote_uri"] is None


def test_chunked_upload_is_confined_owned_and_indexed(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    data = (tmp_path / "data").resolve()
    settings = Settings(
        model_storage=storage,
        data_dir=data,
        model_storage_backend="s3",
        s3_bucket="model-bucket",
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
    fake_s3 = FakeS3Client()
    model_storage = S3ModelStorage(settings, client=fake_s3)
    manager = UploadManager(settings, database, indexer, model_storage)

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
    indexed_model = database.get_local_model(repository["repo_id"])
    assert indexed_model is not None
    assert indexed_model["storage_backend"] == "s3"
    assert indexed_model["cached"] is True
    assert (
        f"models/{repository['repo_id']}/.hugginghack.json"
        in fake_s3.objects
    )
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
    assert not any(
        key.startswith(f"models/{repository['repo_id']}/")
        for key in fake_s3.objects
    )
    assert database.get_owned_repository(repository["repo_id"]) is None

    for value in ("../secret", "/absolute/file", ".git/config", ".hugginghack.json"):
        with pytest.raises(ValueError):
            validate_upload_path(value)


def test_s3_storage_sync_discover_evict_and_restore(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
        model_storage_backend="s3",
        s3_bucket="model-bucket",
        s3_prefix="library",
    )
    root = storage / "acme" / "tiny"
    root.mkdir(parents=True)
    (root / "config.json").write_text(
        json.dumps({"model_type": "llama", "architectures": ["TinyLM"]}),
        encoding="utf-8",
    )
    (root / "model.safetensors").write_bytes(b"weights")
    (root / ".hugginghack.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "repo_id": "acme/tiny",
                "downloaded_at": "2026-07-24T12:00:00+00:00",
                "total_bytes": 7,
                "file_count": 3,
            }
        ),
        encoding="utf-8",
    )
    fake = FakeS3Client()
    fake.objects["library/acme/tiny/obsolete.bin"] = b"old"
    model_storage = S3ModelStorage(
        settings,
        client=fake,
        transfer_config=object(),
    )

    uri = model_storage.sync_repository("acme/tiny", root)

    assert uri == "s3://model-bucket/library/acme/tiny"
    assert "library/acme/tiny/obsolete.bin" not in fake.objects
    assert fake.uploads[-1] == "library/acme/tiny/.hugginghack.json"
    remote_manifest = json.loads(fake.objects[fake.uploads[-1]])
    assert remote_manifest["storage_backend"] == "s3"
    assert remote_manifest["config"]["model_type"] == "llama"
    discovered = model_storage.discover_repositories()
    assert len(discovered) == 1
    assert discovered[0]["repo_id"] == "acme/tiny"
    assert discovered[0]["cached"] is True
    assert model_storage.health()["connected"] is True

    model_storage.evict_repository_cache("acme/tiny")
    assert not root.exists()
    assert model_storage.discover_repositories()[0]["cached"] is False

    restored = model_storage.restore_repository("acme/tiny")
    assert restored == root
    assert (root / "model.safetensors").read_bytes() == b"weights"
    assert json.loads((root / ".hugginghack.json").read_text(encoding="utf-8"))[
        "status"
    ] == "complete"


def test_s3_restore_rejects_traversal_keys(tmp_path: Path):
    settings = Settings(
        model_storage=(tmp_path / "models").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        model_storage_backend="s3",
        s3_bucket="model-bucket",
    )
    fake = FakeS3Client()
    manifest = json.dumps({"status": "complete", "repo_id": "acme/tiny"}).encode()
    fake.objects["models/acme/tiny/.hugginghack.json"] = manifest
    fake.objects["models/acme/tiny/../escape.bin"] = b"nope"
    model_storage = S3ModelStorage(settings, client=fake)

    with pytest.raises(ValueError, match="escapes"):
        model_storage.restore_repository("acme/tiny")

    assert not (tmp_path / "models" / "acme" / "escape.bin").exists()


def test_runtime_target_configuration_and_remote_path_mapping():
    targets = parse_runtime_targets(
        json.dumps(
            [
                {
                    "id": "ollama-rig",
                    "name": "Ollama rig",
                    "kind": "ollama",
                    "base_url": "http://192.168.0.36:11434",
                    "keep_alive": "20m",
                },
                {
                    "id": "vllm-rig",
                    "name": "vLLM rig",
                    "kind": "vllm",
                    "base_url": "http://192.168.0.35:8090",
                    "remote_model_root": "/mnt/nas/models",
                    "token_env": "VLLM_AGENT_TOKEN",
                },
            ]
        )
    )

    assert [target.kind for target in targets] == ["ollama", "vllm"]
    assert targets[0].public()["transfer_mode"] == "blob-upload"
    assert (
        remote_model_path("/mnt/nas/models", "acme/tiny")
        == "/mnt/nas/models/acme/tiny"
    )
    assert (
        remote_model_path(r"Z:\models", "acme/tiny")
        == r"Z:\models\acme\tiny"
    )

    with pytest.raises(ValueError, match="requires remote_model_root"):
        parse_runtime_targets(
            '[{"id":"bad","kind":"vllm","base_url":"http://rig:8090"}]'
        )
    with pytest.raises(ValueError, match="without credentials"):
        parse_runtime_targets(
            '[{"id":"bad","kind":"ollama","base_url":"http://user:pass@rig:11434"}]'
        )


def test_ollama_import_requires_explicit_choice_for_multiple_ggufs(tmp_path: Path):
    root = tmp_path / "models" / "acme" / "quantized"
    root.mkdir(parents=True)
    (root / "model-q4.gguf").write_bytes(b"q4")
    (root / "model-q8.gguf").write_bytes(b"q8")

    with pytest.raises(ValueError, match="multiple GGUF"):
        ollama_files(root)
    assert ollama_files(root, "model-q4.gguf") == [
        (root / "model-q4.gguf").resolve()
    ]
    with pytest.raises(ValueError, match="invalid"):
        ollama_files(root, "../secret.bin")


def _wait_for_runtime_job(
    database: Database, job_id: str, timeout: float = 3
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = database.get_runtime_job(job_id)
        if job and job["status"] in {"ready", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("Runtime job did not finish in time")


def test_runtime_manager_transfers_and_preloads_ollama_model(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    root = storage / "acme" / "tiny-gguf"
    root.mkdir(parents=True)
    (root / "tiny.gguf").write_bytes(b"tiny-gguf-weights")
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
        runtime_targets_json=json.dumps(
            [
                {
                    "id": "ollama-rig",
                    "name": "Ollama rig",
                    "kind": "ollama",
                    "base_url": "http://ollama.test:11434",
                    "keep_alive": "15m",
                }
            ]
        ),
    )
    database = Database(settings.database_path)
    database.initialize()
    user = AuthService(settings, database).create_user(
        "owner", "Owner", "correct horse battery", "admin"
    )
    indexer = LocalModelIndexer(settings, database)
    indexer.scan()
    requests: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request):
        content = request.read()
        requests.append((request.method, request.url.path, content))
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.url.path.startswith("/api/blobs/"):
            return httpx.Response(201)
        if request.url.path == "/api/create":
            return httpx.Response(200, json={"status": "success"})
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"done": True})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    manager = RuntimeManager(
        settings,
        database,
        client_factory=lambda _: httpx.Client(transport=transport),
    )
    model = database.get_local_model("acme/tiny-gguf")
    assert model is not None
    job = manager.queue(
        "ollama-rig", model, "acme-tiny", None, user_id=user["id"]
    )
    finished = _wait_for_runtime_job(database, job["id"])
    manager.shutdown()

    assert finished["status"] == "ready"
    assert finished["progress"] == 100
    assert any(method == "POST" and path.startswith("/api/blobs/") for method, path, _ in requests)
    create_payload = json.loads(
        next(content for method, path, content in requests if path == "/api/create")
    )
    assert create_payload["model"] == "acme-tiny"
    assert set(create_payload["files"]) == {"tiny.gguf"}
    preload_payload = json.loads(
        next(content for method, path, content in requests if path == "/api/generate")
    )
    assert preload_payload["keep_alive"] == "15m"


def test_runtime_manager_maps_shared_model_path_for_vllm_agent(tmp_path: Path):
    storage = (tmp_path / "models").resolve()
    root = storage / "acme" / "tiny"
    root.mkdir(parents=True)
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"weights")
    settings = Settings(
        model_storage=storage,
        data_dir=(tmp_path / "data").resolve(),
        runtime_targets_json=json.dumps(
            [
                {
                    "id": "vllm-rig",
                    "name": "vLLM rig",
                    "kind": "vllm",
                    "base_url": "http://vllm-agent.test:8090",
                    "remote_model_root": "/srv/nas/models",
                }
            ]
        ),
    )
    database = Database(settings.database_path)
    database.initialize()
    user = AuthService(settings, database).create_user(
        "owner", "Owner", "correct horse battery", "admin"
    )
    LocalModelIndexer(settings, database).scan()
    payloads: list[dict] = []

    def handler(request: httpx.Request):
        payloads.append(json.loads(request.read()))
        return httpx.Response(200, json={"status": "ready"})

    manager = RuntimeManager(
        settings,
        database,
        client_factory=lambda _: httpx.Client(
            transport=httpx.MockTransport(handler)
        ),
    )
    model = database.get_local_model("acme/tiny")
    assert model is not None
    job = manager.queue(
        "vllm-rig", model, "tiny-chat", None, user_id=user["id"]
    )
    finished = _wait_for_runtime_job(database, job["id"])
    manager.shutdown()

    assert finished["status"] == "ready"
    assert payloads == [
        {
            "repo_id": "acme/tiny",
            "model_path": "/srv/nas/models/acme/tiny",
            "served_model_name": "tiny-chat",
        }
    ]


def test_vllm_agent_confines_requested_models_to_shared_root(tmp_path: Path):
    root = (tmp_path / "models").resolve()
    allowed = root / "acme" / "tiny"
    allowed.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    manager = VllmProcessManager(
        AgentSettings(
            token="test-token",
            model_root=root,
            log_path=(tmp_path / "agent.log").resolve(),
        )
    )

    assert manager._validated_model_path(str(allowed)) == allowed
    with pytest.raises(ValueError, match="must stay inside"):
        manager._validated_model_path(str(outside))
