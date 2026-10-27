from __future__ import annotations

import fnmatch
import hashlib
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import remote_path
from .query import Query
from .remote import connect, modification_time, valid_child
from .store import Store, now


class LogService:
    def __init__(self, settings, connector=connect):
        self.settings = settings
        self.store = Store(settings.cache_dir)
        self.query = Query(settings, self.store)
        self.connector = connector
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="log-download")
        self.jobs = {}
        self.imports = set()
        self.lock = threading.RLock()
        self.stopping = threading.Event()

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

    def list_remote(self, server, directory=".", pattern="*.log*", limit=100):
        if not 1 <= limit <= 1000 or len(pattern) > 200:
            raise ValueError("limit must be 1..1000 and pattern at most 200 characters")
        config = self.server(server)
        directory = remote_path(directory, config.root)
        rows, truncated, scanned = [], False, 0
        with self.connector(config) as client:
            for item in client.listing(directory):
                scanned += 1
                if scanned > 10000:
                    truncated = True
                    break
                name = item["name"]
                path = valid_child(directory, name, config.root)
                if not fnmatch.fnmatchcase(name, pattern):
                    continue
                if len(rows) == limit:
                    truncated = True
                    break
                rows.append(
                    {
                        "remote_path": path,
                        "name": name,
                        "size": item["size"],
                        "modified_at": modification_time(item["mtime"]),
                    }
                )
        return {
            "server": server,
            "directory": directory,
            "files": rows,
            "truncated": truncated,
            "recursive": False,
        }

    def download(self, server, paths, max_bytes=None):
        config = self.server(server)
        if not paths or len(paths) > self.settings.max_files:
            raise ValueError(f"Choose 1..{self.settings.max_files} files")
        paths = list(dict.fromkeys(remote_path(path, config.root) for path in paths))
        cap = self.settings.max_download_bytes if max_bytes is None else max_bytes
        if not 1 <= cap <= self.settings.max_download_bytes:
            raise ValueError("max_bytes exceeds configured download limit")
        with self.lock:
            self.jobs = {key: future for key, future in self.jobs.items() if not future.done()}
            if sum(not future.done() for future in self.jobs.values()) >= 4:
                raise ValueError("Download queue is full; wait for an existing job")
            manifest = self.store.new(server, self.settings.log_encoding)
            job_id = manifest["dataset_id"]
            self.jobs[job_id] = self.pool.submit(self._download, manifest, config, paths, cap)
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
            manifest["finished_at"] = now()
            self.store.save(manifest)

    def status(self, job_id):
        manifest = self.store.read(job_id)
        with self.lock:
            future = self.jobs.get(job_id)
            importing = job_id in self.imports
        if (
            not importing
            and manifest["status"] in ("queued", "downloading")
            and (future is None or future.done())
        ):
            # The worker may have finished after the first read; never overwrite its final state.
            manifest = self.store.read(job_id)
            if manifest["status"] not in ("queued", "downloading"):
                return manifest
            manifest["status"] = "interrupted"
            manifest["errors"].append(
                {"reason": "Process stopped before download finished; retry download"}
            )
            self.store.save(manifest)
        return manifest

    def datasets(self, limit=50):
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        rows = self.store.list()
        for row in rows[:limit]:
            row["status"] = self.status(row["dataset_id"])["status"]
        return {"datasets": rows[:limit], "truncated": len(rows) > limit}

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
        if total > self.settings.max_download_bytes:
            raise ValueError("Import size exceeds configured download limit")
        with self.lock:
            manifest = self.store.new("local-import", self.settings.log_encoding)
            self.imports.add(manifest["dataset_id"])
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
                            if (
                                manifest["bytes_downloaded"] + len(block)
                                > self.settings.max_download_bytes
                            ):
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
                    }
                )
            manifest["status"] = "completed"
        except Exception:
            manifest["status"] = "partial" if manifest["files"] else "failed"
            manifest["errors"].append({"reason": "local_import_failed"})
            raise
        finally:
            manifest["finished_at"] = now()
            self.store.save(manifest)
            with self.lock:
                self.imports.discard(manifest["dataset_id"])
        return manifest
