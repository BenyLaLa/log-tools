from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

from .config import load_settings, remote_path
from .discovery import Discovery
from .fragments import Collector
from .query import Query
from .remote import connect, modification_time
from .store import Store, now


class LogService:
    def __init__(self, settings, connector=connect, *, config_path=None):
        self.settings = settings
        self.config_path = config_path
        self.store = Store(settings.cache_dir, lambda: self.storage_settings().max_cache_bytes)
        self.query = Query(settings, self.store)
        self.connector = connector
        self.discovery = Discovery()
        self.collector = Collector(settings, self.store, connector)
        self.collection_slots = threading.BoundedSemaphore(2)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="log-download")
        self.jobs = {}
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.store.prune()

    def storage_settings(self):
        if self.config_path is not None:
            try:
                current = load_settings(self.config_path)
                if current.cache_dir == self.settings.cache_dir:
                    return current
            except (OSError, ValueError):
                pass
        return self.settings

    def close(self):
        self.stopping.set()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def server(self, name):
        if name not in self.settings.servers:
            raise ValueError("Unknown server alias; call list_servers")
        return self.settings.servers[name]

    def servers(self):
        return {
            "servers": [
                {"alias": name, "protocol": value.protocol, "root": value.root}
                for name, value in self.settings.servers.items()
            ]
        }

    def list_remote(self, server, directory=".", pattern=None, limit=100,
                    recursive=None, max_depth=None, cursor=None, *, all_files=False):
        return self.discovery.list(self.connector, self.server(server), server,
                                   directory, pattern, limit, recursive, max_depth, cursor,
                                   all_files=all_files)

    def collect_fragments(self, server, **arguments):
        if not self.collection_slots.acquire(blocking=False):
            raise ValueError("Two fragment scans are already running; wait for one to finish")
        try:
            with ExitStack() as leases:
                result = self.collector.collect(
                    server, self.server(server), **arguments, lease_scope=leases,
                    cache_limit=self.storage_settings().max_cache_bytes,
                )
                parameters = result["collection"]["parameters"]
                result["preview"] = self.query.search(result["dataset_id"],
                    **{key: parameters[key] for key in ("query", "start_time", "end_time", "level", "trace_id")}, limit=10)
                if not result["preview"]["matches"] and result["files"]:
                    result["preview"] = self.query.search(result["dataset_id"], limit=10)
                    result["preview_note"] = "Original filter may no longer match redacted text; showing saved context"
                result["preview"]["contexts"] = [self.query.context(result["dataset_id"], row["file_id"], row["line"], 3, 10)
                                                 for row in result["preview"]["matches"][:3]]
        finally:
            try:
                self.store.prune()
            finally:
                self.collection_slots.release()
        if result["status"] == "failed" or not (self.store.directory(result["dataset_id"]) / "manifest.json").exists():
            result.update(cache_retained=False, resume_dataset_id=None, local_directory=None)
        return result

    def download(self, server, paths, max_bytes=None):
        config = self.server(server)
        if not paths or len(paths) > self.settings.max_files:
            raise ValueError(f"Choose 1..{self.settings.max_files} files")
        paths = list(dict.fromkeys(remote_path(path, config.root) for path in paths))
        storage = self.storage_settings()
        cap = storage.max_download_bytes if max_bytes is None else max_bytes
        if not 1 <= cap <= storage.max_download_bytes:
            raise ValueError("max_bytes exceeds configured download limit")
        if storage.max_cache_bytes:
            cap = min(cap, storage.max_cache_bytes)
        with self.lock:
            self.jobs = {key: future for key, future in self.jobs.items() if not future.done()}
            if sum(not future.done() for future in self.jobs.values()) >= 4:
                raise ValueError("Download queue is full; wait for an existing job")
            manifest, lease = self.store.new(server, self.settings.log_encoding, reserve=True)
            job_id = manifest["dataset_id"]
            try:
                future = self.pool.submit(self._download, manifest, config, paths, cap)
                def release(_):
                    lease.close()
                    self.store.prune()

                future.add_done_callback(release)
                self.jobs[job_id] = future
            except BaseException:
                lease.close()
                raise
        return {"job_id": job_id, "dataset_id": job_id, "status": "queued"}

    def _download(self, manifest, config, paths, cap):
        manifest["status"] = "downloading"
        manifest["requested_files"] = len(paths)
        self.store.save(manifest)
        try:
            for path in paths:
                if self.stopping.is_set():
                    raise RuntimeError("Service is stopping")
                file_id = str(uuid.uuid4())
                destination = self.store.file_path(manifest["dataset_id"], file_id)
                part = destination.with_suffix(".part")
                try:
                    # A fresh connection per file isolates transfer errors and reconnections.
                    with self.connector(config) as client:
                        initial = client.metadata(path)
                        size = initial["size"]
                        if size < 0 or size + manifest["bytes_downloaded"] > cap:
                            raise ValueError("download_size_limit")
                        digest = hashlib.sha256()
                        written = 0
                        last_flush = time.monotonic()
                        with part.open("xb") as target:

                            def consume(block, size=size, digest=digest, target=target):
                                nonlocal written, last_flush
                                if self.stopping.is_set():
                                    raise RuntimeError("Service is stopping")
                                if written + len(block) > size:
                                    raise ValueError("remote_file_grew_during_download")
                                if manifest["bytes_downloaded"] + len(block) > cap:
                                    raise ValueError("download_size_limit")
                                target.write(block)
                                digest.update(block)
                                written += len(block)
                                manifest["bytes_downloaded"] += len(block)
                                if time.monotonic() - last_flush >= 1:
                                    self.store.save(manifest)
                                    last_flush = time.monotonic()

                            client.download(path, consume)
                        final = client.metadata(path)
                        if initial != final or written != size:
                            raise ValueError("remote_file_changed_during_download")
                    os.replace(part, destination)
                    manifest["files"].append(
                        {
                            "file_id": file_id,
                            "name": path.rsplit("/", 1)[-1],
                            "remote_path": path,
                            "bytes": written,
                            "sha256": digest.hexdigest(),
                            "compressed": path.lower().endswith(".gz"),
                            "downloaded_at": now(),
                            "modified_at": modification_time(initial["mtime"]),
                        }
                    )
                except Exception as error:
                    # Do not persist server replies: they can contain secrets or untrusted text.
                    reason = (
                        str(error)
                        if isinstance(error, ValueError)
                        and str(error)
                        in (
                            "download_size_limit",
                            "remote_file_grew_during_download",
                            "remote_file_changed_during_download",
                        )
                        else type(error).__name__
                    )
                    manifest["errors"].append({"remote_path": path, "reason": reason})
                finally:
                    part.unlink(missing_ok=True)
                self.store.save(manifest)
            manifest["status"] = (
                ("partial" if manifest["files"] else "failed")
                if manifest["errors"]
                else "completed"
            )
        except Exception as error:
            manifest["status"] = "partial" if manifest["files"] else "failed"
            manifest["errors"].append({"reason": type(error).__name__})
        finally:
            self.store.finish(manifest)
        return manifest if manifest.get("cache_rejected") else None

    def status(self, job_id):
        with self.lock:
            future = self.jobs.get(job_id)
        try:
            manifest = self.store.read(job_id)
        except FileNotFoundError:
            if future and future.done() and not future.cancelled() and future.exception() is None:
                if rejected := future.result():
                    return rejected
            raise
        if (
            manifest["status"] in ("queued", "downloading", "collecting")
            and (future is None or future.done())
        ):
            # A different MCP process may own this job. Only recover it after its
            # OS-held lease becomes available, then re-read under that lease.
            lease = self.store.lease(job_id)
            if lease is None:
                return self.store.read(job_id)
            try:
                manifest = self.store.read(job_id)
                if manifest["status"] in ("queued", "downloading", "collecting"):
                    manifest["status"] = "interrupted"
                    manifest["errors"].append(
                        {"reason": "Fragment scan interrupted; start a new scan" if manifest.get("kind") == "fragments"
                         else "Process stopped before download finished; retry download"}
                    )
                    self.store.save(manifest)
            finally:
                lease.close()
        return manifest

    def datasets(self, limit=50):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        rows = self.store.list()
        available = []
        for row in rows[:limit]:
            try:
                row["status"] = self.status(row["dataset_id"])["status"]
            except FileNotFoundError:
                continue  # A concurrent cleanup evicted this snapshot.
            available.append(row)
        return {"datasets": available, "truncated": len(rows) > limit}

    def import_local(self, paths):
        """Copy allowlisted local files into an immutable query dataset."""
        if not paths or len(paths) > self.settings.max_files:
            raise ValueError(f"Choose 1..{self.settings.max_files} files")
        selected = []
        total = 0
        for value in dict.fromkeys(paths):
            if not Path(value).is_absolute():
                raise ValueError("Local file paths must be absolute")
            path = Path(value).resolve()
            if not any(path.is_relative_to(root.resolve()) for root in self.settings.local_roots):
                raise ValueError("Local file is outside configured local_roots")
            if not path.is_file():
                raise ValueError("Only regular files may be imported")
            total += path.stat().st_size
            selected.append(path)
        storage = self.storage_settings()
        cap = min(storage.max_download_bytes, storage.max_cache_bytes or storage.max_download_bytes)
        if total > cap:
            raise ValueError("Import size exceeds configured download limit")
        manifest, lease = self.store.new("local-import", self.settings.log_encoding, reserve=True)
        manifest["status"] = "downloading"
        try:
            for source in selected:
                initial = source.stat()
                file_id = str(uuid.uuid4())
                destination = self.store.file_path(manifest["dataset_id"], file_id)
                digest = hashlib.sha256()
                size = 0
                try:
                    with source.open("rb") as reader, destination.open("xb") as writer:
                        while block := reader.read(64 * 1024):
                            if manifest["bytes_downloaded"] + len(block) > cap:
                                raise ValueError("Import size exceeds configured download limit")
                            writer.write(block)
                            digest.update(block)
                            size += len(block)
                            manifest["bytes_downloaded"] += len(block)
                    final = source.stat()
                    if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
                        raise ValueError("Local file changed during import; retry on a rotated log")
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
                manifest["files"].append(
                    {
                        "file_id": file_id,
                        "name": source.name,
                        "bytes": size,
                        "sha256": digest.hexdigest(),
                        "compressed": source.name.lower().endswith(".gz"),
                        "downloaded_at": now(),
                        "modified_at": modification_time(initial.st_mtime),
                    }
                )
            manifest["status"] = "completed"
        except Exception:
            manifest["status"] = "partial" if manifest["files"] else "failed"
            manifest["errors"].append({"reason": "local_import_failed"})
            raise
        finally:
            try:
                self.store.finish(manifest)
            finally:
                lease.close()
                self.store.prune()
        if manifest.get("cache_rejected"):
            raise ValueError("Imported dataset exceeds the cache limit including its manifest")
        return manifest
