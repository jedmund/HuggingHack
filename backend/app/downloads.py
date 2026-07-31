from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .config import Settings, repository_path, validate_repo_id
from .database import Database
from .download_schedule import window_is_open
from .hub_service import HubService
from .indexer import LocalModelIndexer, directory_stats
from .storage import FilesystemModelStorage


METADATA_PATTERNS = (
    "*.json",
    "*.md",
    "*.txt",
    "*.yaml",
    "*.yml",
    "*.jinja",
    "*.model",
    "*.tiktoken",
    "LICENSE*",
    "tokenizer*",
)
UNSAFE_PATTERNS = ("*.bin", "*.pt", "*.pth", "*.pkl", "*.pickle", "*.ckpt")
RUNNING_STATUSES = {"queued", "scheduled", "preparing", "downloading"}
PENDING_STATUSES = RUNNING_STATUSES | {"paused", "finalizing"}
STOPPED_STATUSES = {"paused", "cancelled", "failed"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DownloadInterrupted(Exception):
    pass


def _safe_selection_path(value: str, folder: bool) -> str:
    normalized = value.strip().replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or len(normalized) > 500
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("Selected paths must be safe repository-relative paths.")
    return f"{path.as_posix()}/" if folder else path.as_posix()


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def resolve_selection(
    details: dict[str, Any],
    mode: str,
    allow_patterns: list[str] | None,
    ignore_patterns: list[str] | None,
    selection: dict[str, Any] | None = None,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    files = [item for item in details.get("files") or [] if item.get("path")]
    allowed = [value.strip() for value in (allow_patterns or []) if value.strip()]
    ignored = [value.strip() for value in (ignore_patterns or []) if value.strip()]

    if mode == "safetensors" and not allowed:
        allowed = ["*.safetensors", *METADATA_PATTERNS]
    elif mode == "metadata" and not allowed:
        allowed = list(METADATA_PATTERNS)
    elif mode == "selection":
        selected_patterns: list[str] = []
        available_paths = {str(item["path"]) for item in files}
        for item in (selection or {}).get("paths") or []:
            kind = str(item.get("kind") or "file")
            if kind not in {"file", "folder"}:
                raise ValueError("Selection entries must be files or folders.")
            selected = _safe_selection_path(str(item.get("path") or ""), kind == "folder")
            if kind == "folder":
                prefix = selected
                if not any(path.startswith(prefix) for path in available_paths):
                    raise ValueError(f"Selected folder does not exist: {prefix}")
                selected_patterns.append(f"{prefix}*")
            else:
                if selected not in available_paths:
                    raise ValueError(f"Selected file does not exist: {selected}")
                selected_patterns.append(selected)
        if not selected_patterns:
            raise ValueError("Choose at least one file or folder.")
        allowed = list(dict.fromkeys(selected_patterns))
        if (selection or {}).get("include_metadata", True):
            allowed.extend(pattern for pattern in METADATA_PATTERNS if pattern not in allowed)

    selected_files = [
        item
        for item in files
        if (not allowed or _matches(str(item["path"]), allowed))
        and not _matches(str(item["path"]), ignored)
    ]
    if not selected_files:
        raise ValueError("The selected download does not match any repository files.")
    return allowed, ignored, selected_files


class DownloadManager:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        hub: HubService,
        indexer: LocalModelIndexer,
        model_storage: FilesystemModelStorage | None = None,
    ):
        self.settings = settings
        self.database = database
        self.hub = hub
        self.indexer = indexer
        self.model_storage = model_storage or FilesystemModelStorage(settings)
        self.executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_downloads,
            thread_name_prefix="hugginghack-download",
        )
        self._submitted: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.RLock()
        self._scheduler_stop = threading.Event()
        self._scheduler_wake = threading.Event()
        self._scheduler_enabled = False
        self._scheduler = threading.Thread(
            target=self._schedule_loop,
            name="hugginghack-download-scheduler",
            daemon=True,
        )
        self._scheduler.start()

    def schedule(self) -> dict[str, Any]:
        current = self.database.get_download_schedule()
        return {**current, "window_open": window_is_open(current)}

    def notify_schedule_changed(self) -> None:
        self._scheduler_enabled = True
        self._scheduler_wake.set()

    def _window_open(self) -> bool:
        return window_is_open(self.database.get_download_schedule())

    def queue(
        self,
        repo_id: str,
        revision: str = "main",
        allow_patterns: list[str] | None = None,
        ignore_patterns: list[str] | None = None,
        mode: str = "full",
        user_id: str | None = None,
        selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validated = validate_repo_id(repo_id)
        active = self.database.find_active_download(validated)
        if active:
            if active.get("user_id") != user_id:
                raise ValueError("This model is already being downloaded by another account.")
            return active

        requested_revision = revision.strip() or "main"
        details = self.hub.model_details(validated, requested_revision)
        allowed, ignored, selected_files = resolve_selection(
            details, mode, allow_patterns, ignore_patterns, selection
        )
        target = repository_path(validated, self.settings.model_storage)
        download_id = uuid.uuid4().hex
        staging = (self.settings.download_staging_path / download_id).resolve()
        created = now_iso()
        resolved_revision = str(details.get("sha") or requested_revision)
        payload = {
            "allow_patterns": allowed,
            "ignore_patterns": ignored,
            "mode": mode,
            "selection": selection or None,
            "selected_files": [str(item["path"]) for item in selected_files],
        }
        record = {
            "id": download_id,
            "repo_id": validated,
            "revision": requested_revision,
            "resolved_revision": resolved_revision,
            "status": "queued" if self._window_open() else "scheduled",
            "total_bytes": sum(int(item.get("size") or 0) for item in selected_files),
            "downloaded_bytes": 0,
            "progress": 0,
            "speed_bps": 0,
            "error": None,
            "target_path": str(target),
            "staging_path": str(staging),
            "pause_reason": None if self._window_open() else "window",
            "cleaned_at": None,
            "payload_json": json.dumps(payload),
            "metadata_json": json.dumps(
                {
                    "pipeline_tag": details.get("pipeline_tag"),
                    "library_name": details.get("library_name"),
                    "license": details.get("license"),
                    "file_count": len(selected_files),
                    "repository_file_count": len(details.get("files") or []),
                    "gated": details.get("gated"),
                }
            ),
            "created_at": created,
            "updated_at": created,
            "completed_at": None,
            "user_id": user_id,
        }
        download = self.database.create_download(record)
        self.notify_schedule_changed()
        return download

    def _submit(self, download_id: str) -> None:
        with self._lock:
            if download_id in self._submitted:
                return
            self._submitted.add(download_id)
            self._cancel_events[download_id] = threading.Event()
        self.executor.submit(self._run, download_id)

    def _stop_process(self, download_id: str) -> None:
        with self._lock:
            event = self._cancel_events.setdefault(download_id, threading.Event())
            event.set()
            process = self._processes.get(download_id)
            if process and process.poll() is None:
                process.terminate()

    def _retained_bytes(self, download: dict[str, Any]) -> int:
        staging = self._staging_for(download)
        return directory_stats(staging, include_cache=True)[0] if staging.is_dir() else 0

    def _write_stopped_manifest(
        self, download: dict[str, Any], status: str, timestamp: str
    ) -> None:
        staging = self._staging_for(download)
        if not staging.is_dir():
            return
        try:
            (staging / ".hugginghack.json").write_text(
                json.dumps(
                    {
                        "status": status,
                        "repo_id": download["repo_id"],
                        "revision": download["revision"],
                        f"{status}_at": timestamp,
                        "partial_bytes": self._retained_bytes(download),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def pause(self, download_id: str, reason: str = "user") -> dict[str, Any] | None:
        download = self.database.get_download(download_id)
        if not download:
            return None
        if download["status"] not in RUNNING_STATUSES:
            raise ValueError(f"Download is already {download['status']}.")
        paused_at = now_iso()
        status = "scheduled" if reason == "window" else "paused"
        self._stop_process(download_id)
        updated = self.database.update_download_if_status(
            download_id,
            RUNNING_STATUSES,
            status=status,
            pause_reason=reason,
            downloaded_bytes=self._retained_bytes(download),
            speed_bps=0,
            error=None,
            updated_at=paused_at,
            completed_at=None,
        )
        if updated:
            self._write_stopped_manifest(updated, status, paused_at)
        return updated or self.database.get_download(download_id)

    def resume(self, download_id: str) -> dict[str, Any] | None:
        download = self.database.get_download(download_id)
        if not download:
            return None
        if download["status"] != "paused":
            raise ValueError(f"Download is already {download['status']}.")
        status = "queued" if self._window_open() else "scheduled"
        updated = self.database.update_download(
            download_id,
            status=status,
            pause_reason=None if status == "queued" else "window",
            error=None,
            speed_bps=0,
            updated_at=now_iso(),
            completed_at=None,
        )
        self.notify_schedule_changed()
        return updated

    def cancel(self, download_id: str) -> dict[str, Any] | None:
        download = self.database.get_download(download_id)
        if not download:
            return None
        if download["status"] not in RUNNING_STATUSES | {"paused"}:
            raise ValueError(f"Download is already {download['status']}.")
        cancelled_at = now_iso()
        self._stop_process(download_id)
        updated = self.database.update_download(
            download_id,
            status="cancelled",
            pause_reason=None,
            downloaded_bytes=self._retained_bytes(download),
            speed_bps=0,
            error=None,
            updated_at=cancelled_at,
            completed_at=cancelled_at,
        )
        if updated:
            self._write_stopped_manifest(updated, "cancelled", cancelled_at)
        return updated

    def cleanup(self, download_id: str) -> dict[str, Any] | None:
        download = self.database.get_download(download_id)
        if not download:
            return None
        if download["status"] not in STOPPED_STATUSES:
            raise ValueError("Pause or stop the download before deleting retained data.")
        staging = self._staging_for(download)
        if staging.exists():
            manifest = staging / ".hugginghack.json"
            try:
                state = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            if state.get("status") == "complete":
                raise ValueError("Completed model data cannot be removed from download history.")
            if staging == Path(download.get("target_path") or "").resolve():
                local = self.database.get_local_model(download["repo_id"])
                if local and state.get("status") not in {"paused", "scheduled", "cancelled", "failed"}:
                    raise ValueError("This path contains an indexed model and cannot be cleaned here.")
            shutil.rmtree(staging)
        cleaned_at = now_iso()
        return self.database.update_download(
            download_id,
            status="cancelled" if download["status"] == "paused" else download["status"],
            downloaded_bytes=0,
            progress=0,
            speed_bps=0,
            staging_path=None,
            cleaned_at=cleaned_at,
            pause_reason=None,
            updated_at=cleaned_at,
            completed_at=download.get("completed_at") or cleaned_at,
        )

    def resume_unfinished(self) -> None:
        schedule_open = self._window_open()
        for download in self.database.unfinished_downloads():
            if download["status"] == "finalizing":
                next_status = "queued"
            else:
                next_status = "queued" if schedule_open else "scheduled"
            self.database.update_download(
                download["id"],
                status=next_status,
                pause_reason=None if next_status == "queued" else "window",
                error=None,
                speed_bps=0,
                updated_at=now_iso(),
            )
        self.notify_schedule_changed()

    def _schedule_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            self._scheduler_wake.wait(1.0)
            self._scheduler_wake.clear()
            if not self._scheduler_enabled or self._scheduler_stop.is_set():
                continue
            try:
                downloads = self.database.unfinished_downloads()
                open_now = self._window_open()
                if not open_now:
                    for download in downloads:
                        if download["status"] == "queued":
                            self.database.update_download(
                                download["id"],
                                status="scheduled",
                                pause_reason="window",
                                speed_bps=0,
                                updated_at=now_iso(),
                            )
                        elif download["status"] in {"preparing", "downloading"}:
                            self.pause(download["id"], "window")
                    continue

                for download in downloads:
                    if download["status"] == "scheduled":
                        self.database.update_download(
                            download["id"],
                            status="queued",
                            pause_reason=None,
                            updated_at=now_iso(),
                        )
                with self._lock:
                    available = self.settings.max_concurrent_downloads - len(self._submitted)
                if available <= 0:
                    continue
                queued = [
                    item
                    for item in self.database.unfinished_downloads()
                    if item["status"] == "queued"
                ]
                for download in queued[:available]:
                    self._submit(download["id"])
            except Exception:
                # A transient database or clock failure must not kill future scheduling.
                continue

    def _monitor(
        self,
        download_id: str,
        staging: Path,
        total_bytes: int,
        stop: threading.Event,
        cancel_event: threading.Event,
    ) -> None:
        last_bytes = 0
        last_time = time.monotonic()
        speed = 0.0
        while not stop.wait(1.25):
            if cancel_event.is_set():
                return
            current, _, _ = directory_stats(staging, include_cache=True)
            current_time = time.monotonic()
            elapsed = max(current_time - last_time, 0.001)
            instant_speed = max(0, current - last_bytes) / elapsed
            speed = instant_speed if speed == 0 else speed * 0.6 + instant_speed * 0.4
            progress = min(99.5, current * 100 / total_bytes) if total_bytes else 0
            self.database.update_download_if_status(
                download_id,
                {"preparing", "downloading"},
                status="downloading",
                downloaded_bytes=current,
                progress=round(progress, 2),
                speed_bps=round(speed, 2),
                updated_at=now_iso(),
            )
            last_bytes = current
            last_time = current_time

    @staticmethod
    def _raise_if_interrupted(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise DownloadInterrupted()

    def _download_snapshot(
        self,
        download_id: str,
        download: dict[str, Any],
        target: Path,
        cancel_event: threading.Event,
    ) -> None:
        payload = download.get("payload") or {}
        command = [
            sys.executable,
            "-m",
            "app.download_worker",
            "--repo-id",
            download["repo_id"],
            "--revision",
            download.get("resolved_revision") or download["revision"],
            "--target",
            str(target),
            "--endpoint",
            self.settings.hf_endpoint,
            "--allow-patterns",
            json.dumps(payload.get("allow_patterns") or []),
            "--ignore-patterns",
            json.dumps(payload.get("ignore_patterns") or []),
            "--workers",
            str(self.settings.download_workers_per_job),
        ]
        environment = os.environ.copy()
        environment["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        if self.settings.hf_token:
            environment["HF_TOKEN"] = self.settings.hf_token

        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as error_output:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=error_output,
                text=True,
                env=environment,
            )
            with self._lock:
                self._processes[download_id] = process
            try:
                while process.poll() is None:
                    if cancel_event.wait(0.25):
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise DownloadInterrupted()
                if process.returncode != 0:
                    error_output.seek(0)
                    message = error_output.read().strip()
                    raise RuntimeError(
                        message or f"Download worker exited with code {process.returncode}."
                    )
            finally:
                with self._lock:
                    self._processes.pop(download_id, None)

    def _staging_for(self, download: dict[str, Any]) -> Path:
        configured = download.get("staging_path")
        if configured:
            staging = Path(configured).resolve()
            root = self.settings.download_staging_path.resolve()
            if staging != root and root not in staging.parents:
                raise ValueError("Download staging path escapes model storage.")
            return staging
        target = Path(download.get("target_path") or "").resolve()
        # Downloads created by releases before isolated staging wrote directly to target_path.
        if target.is_dir():
            return target
        return (self.settings.download_staging_path / download["id"]).resolve()

    @staticmethod
    def _link_or_copy(source: str, destination: str) -> str:
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
        return destination

    def _seed_existing(self, target: Path, staging: Path) -> None:
        if not target.is_dir() or any(staging.iterdir()):
            return
        try:
            manifest = json.loads((target / ".hugginghack.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if manifest.get("status") != "complete":
            return
        shutil.copytree(
            target,
            staging,
            dirs_exist_ok=True,
            copy_function=self._link_or_copy,
        )

    @staticmethod
    def _prune_selection(staging: Path, selected_paths: set[str]) -> None:
        if not selected_paths:
            return
        for current, directories, names in os.walk(staging, topdown=False):
            current_path = Path(current)
            relative_root = current_path.relative_to(staging)
            if relative_root.parts and relative_root.parts[0] == ".cache":
                continue
            for name in names:
                path = current_path / name
                relative = path.relative_to(staging).as_posix()
                if relative != ".hugginghack.json" and relative not in selected_paths:
                    path.unlink(missing_ok=True)
            for name in directories:
                path = current_path / name
                if name != ".cache":
                    try:
                        path.rmdir()
                    except OSError:
                        pass

    def _promote(self, download_id: str, staging: Path, target: Path) -> None:
        if staging == target:
            return
        backups = self.settings.model_storage / ".hugginghack-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / download_id
        if backup.exists():
            shutil.rmtree(backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        had_target = target.exists()
        if had_target:
            os.replace(target, backup)
        try:
            os.replace(staging, target)
            self.model_storage.sync_repository(
                validate_repo_id(target.relative_to(self.settings.model_storage).as_posix()),
                target,
            )
            self.indexer.index_path(target)
        except Exception:
            if target.exists():
                os.replace(target, staging)
            if had_target and backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)

    def _run(self, download_id: str) -> None:
        with self._lock:
            cancel_event = self._cancel_events.setdefault(download_id, threading.Event())
        try:
            download = self.database.get_download(download_id)
            if not download or download["status"] != "queued":
                return
            self._raise_if_interrupted(cancel_event)
            repo_id = validate_repo_id(download["repo_id"])
            target = repository_path(repo_id, self.settings.model_storage)
            staging = self._staging_for(download)
            staging.mkdir(parents=True, exist_ok=True)
            if not download.get("staging_path") and staging != target:
                download = self.database.update_download(
                    download_id, staging_path=str(staging), updated_at=now_iso()
                ) or download
            self._seed_existing(target, staging)
            if not self.database.update_download_if_status(
                download_id,
                {"queued"},
                status="preparing",
                error=None,
                updated_at=now_iso(),
            ):
                return
            self._raise_if_interrupted(cancel_event)
            details = self.hub.model_details(repo_id, download["revision"])
            payload = download.get("payload") or {}
            allowed, ignored, selected_files = resolve_selection(
                details,
                str(payload.get("mode") or "full"),
                payload.get("allow_patterns") or [],
                payload.get("ignore_patterns") or [],
                payload.get("selection"),
            )
            selected_paths = {str(item["path"]) for item in selected_files}
            total_bytes = sum(int(item.get("size") or 0) for item in selected_files)
            resolved_revision = str(download.get("resolved_revision") or details.get("sha") or download["revision"])
            payload.update(
                {
                    "allow_patterns": allowed,
                    "ignore_patterns": ignored,
                    "selected_files": sorted(selected_paths),
                }
            )
            download = self.database.update_download(
                download_id,
                resolved_revision=resolved_revision,
                total_bytes=total_bytes,
                payload_json=json.dumps(payload),
                metadata_json=json.dumps(
                    {
                        "pipeline_tag": details.get("pipeline_tag"),
                        "library_name": details.get("library_name"),
                        "license": details.get("license"),
                        "file_count": len(selected_files),
                        "repository_file_count": len(details.get("files") or []),
                        "gated": details.get("gated"),
                    }
                ),
                updated_at=now_iso(),
            ) or download
            self._raise_if_interrupted(cancel_event)
            manifest_path = staging / ".hugginghack.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "downloading",
                        "repo_id": repo_id,
                        "revision": download["revision"],
                        "resolved_revision": resolved_revision,
                        "source_url": details.get("source_url"),
                        "started_at": download["created_at"],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            stop = threading.Event()
            monitor = threading.Thread(
                target=self._monitor,
                args=(download_id, staging, total_bytes, stop, cancel_event),
                name=f"hugginghack-progress-{download_id[:8]}",
                daemon=True,
            )
            monitor.start()
            try:
                self._download_snapshot(download_id, download, staging, cancel_event)
            finally:
                stop.set()
                monitor.join(timeout=3)
            self._raise_if_interrupted(cancel_event)
            self._prune_selection(staging, selected_paths)
            final_size, file_count, _ = directory_stats(staging)
            completed_at = now_iso()
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "repo_id": repo_id,
                        "revision": download["revision"],
                        "resolved_revision": resolved_revision,
                        "sha": details.get("sha"),
                        "downloaded_at": completed_at,
                        "source_url": details.get("source_url"),
                        "pipeline_tag": details.get("pipeline_tag"),
                        "library_name": details.get("library_name"),
                        "license": details.get("license"),
                        "tags": details.get("tags") or [],
                        "gated": details.get("gated"),
                        "total_bytes": final_size,
                        "file_count": file_count,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            if not self.database.update_download_if_status(
                download_id,
                {"preparing", "downloading"},
                status="finalizing",
                downloaded_bytes=final_size,
                progress=99.5,
                speed_bps=0,
                updated_at=completed_at,
            ):
                raise DownloadInterrupted()
            self._promote(download_id, staging, target)
            if staging == target:
                self.model_storage.sync_repository(repo_id, target)
                self.indexer.index_path(target)
            self.database.update_download(
                download_id,
                status="complete",
                total_bytes=total_bytes or final_size,
                downloaded_bytes=final_size,
                progress=100,
                speed_bps=0,
                error=None,
                target_path=str(target),
                staging_path=None,
                pause_reason=None,
                cleaned_at=None,
                updated_at=completed_at,
                completed_at=completed_at,
            )
        except DownloadInterrupted:
            return
        except Exception as error:
            current = self.database.get_download(download_id)
            if current and current["status"] in {"paused", "scheduled", "cancelled"}:
                return
            message = str(error).strip() or error.__class__.__name__
            if "gated" in message.lower() or "401" in message or "403" in message:
                message = (
                    f"{message} Accept the model terms on Hugging Face and configure "
                    "a read-only HF_TOKEN in .env."
                )
            failed_at = now_iso()
            self.database.update_download(
                download_id,
                status="failed",
                downloaded_bytes=self._retained_bytes(current) if current else 0,
                speed_bps=0,
                error=message[:2000],
                updated_at=failed_at,
                completed_at=failed_at,
            )
            if current:
                self._write_stopped_manifest(current, "failed", failed_at)
        finally:
            with self._lock:
                self._submitted.discard(download_id)
                self._cancel_events.pop(download_id, None)
                self._processes.pop(download_id, None)
            self._scheduler_wake.set()

    def shutdown(self) -> None:
        self._scheduler_stop.set()
        self._scheduler_wake.set()
        self._scheduler.join(timeout=3)
        with self._lock:
            running = list(self._processes)
        for download_id in running:
            download = self.database.get_download(download_id)
            if download and download["status"] in {"preparing", "downloading"}:
                self.database.update_download(
                    download_id,
                    status="queued",
                    speed_bps=0,
                    updated_at=now_iso(),
                )
            self._stop_process(download_id)
        self.executor.shutdown(wait=False, cancel_futures=False)
