from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .leases import Lease


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid dataset/file ID") from None
    return value


class Store:
    def __init__(self, root: Path, cache_limit=None):
        self.root = root.resolve()
        self.datasets = self.root / "datasets"
        self.datasets.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.cache_limit = cache_limit
        self.pending_cleanup = self.datasets / ".cache-prune-pending"

    @contextmanager
    def cache_guard(self):
        deadline = time.monotonic() + 15
        while True:
            lease = Lease(self.datasets / ".cache-lease")
            if lease.acquire():
                break
            if time.monotonic() >= deadline:
                raise ValueError("Log cache is busy; try again shortly")
            time.sleep(0.025)
        try:
            yield
        finally:
            lease.close()

    def directory(self, dataset_id):
        path = (self.datasets / identifier(dataset_id)).resolve()
        if path != self.datasets / dataset_id or path.parent != self.datasets:
            raise ValueError("Dataset path escaped cache directory")
        return path

    def read(self, dataset_id):
        with self.lock:
            return json.loads((self.directory(dataset_id) / "manifest.json").read_text("utf-8"))

    def save(self, manifest):
        with self.lock:
            directory = self.directory(manifest["dataset_id"])
            directory.mkdir(parents=True, exist_ok=True)
            temp = directory / (uuid.uuid4().hex + ".tmp")
            try:
                temp.write_bytes(self.manifest_bytes(manifest))
                os.replace(temp, directory / "manifest.json")
            finally:
                temp.unlink(missing_ok=True)

    @staticmethod
    def manifest_bytes(manifest):
        return json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    def finish(self, manifest):
        """Publish a terminal manifest while the writer still holds its lease."""
        manifest["finished_at"] = now()
        limit = self.cache_limit() if self.cache_limit else 0
        if not limit:
            self.save(manifest)
            return
        with self.cache_guard():
            limit = self.cache_limit()
            files = self.cache_files(self.directory(manifest["dataset_id"]))
            size = sum(size for path, size in files if path.name != "manifest.json")
            if limit and size + len(self.manifest_bytes(manifest)) > limit:
                # A dataset that can never fit must not displace older evidence.
                for path, _ in files:
                    if path.suffix in {".log", ".part", ".tmp"}:
                        path.unlink(missing_ok=True)
                manifest.update(status="failed", files=[], cache_rejected=True)
                manifest["errors"] = [{"reason": "dataset_exceeds_cache_limit"}]
                if collection := manifest.get("collection"):
                    manifest["bytes_saved"] = 0
                    collection.update(coverage=[], remaining=[], remaining_count=0, saved_bytes=0,
                                      warnings=manifest["errors"], incomplete=True)
            self.save(manifest)

    def lease(self, dataset_id, *, create=False):
        with self.cache_guard():
            directory = self.directory(dataset_id)
            if create:
                directory.mkdir(exist_ok=False)
            elif not directory.is_dir():
                raise FileNotFoundError("Dataset was removed from the cache")
            lease = Lease(directory / ".job-lease")
            return lease if lease.acquire() else None

    @contextmanager
    def reading(self, dataset_id):
        with self.cache_guard():
            directory = self.directory(dataset_id)
            if not (directory / "manifest.json").is_file():
                raise FileNotFoundError("Dataset was removed from the cache")
            marker = directory / f".read-lease-{uuid.uuid4().hex}"
            lease = Lease(marker)
            if not lease.acquire():
                raise ValueError("Could not reserve dataset for reading")
        try:
            yield
        finally:
            lease.close()
            try:
                with self.cache_guard():
                    marker.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass  # An unlocked marker does not prevent eviction.
            if self.cache_limit and self.pending_cleanup.exists():
                self.prune()

    def new(self, source, encoding, *, reserve=False):
        manifest = {
            "dataset_id": str(uuid.uuid4()),
            "source": source,
            "created_at": now(),
            "status": "queued",
            "encoding": encoding,
            "files": [],
            "errors": [],
            "bytes_downloaded": 0,
        }
        lease = self.lease(manifest["dataset_id"], create=True) if reserve else None
        if reserve and lease is None:
            raise RuntimeError("Could not reserve new job")
        try:
            self.save(manifest)
        except BaseException:
            if lease:
                lease.close()
            raise
        return (manifest, lease) if reserve else manifest

    def file_path(self, dataset_id, file_id):
        path = (self.directory(dataset_id) / (identifier(file_id) + ".log")).resolve()
        if not path.is_relative_to(self.directory(dataset_id)):
            raise ValueError("File path escaped dataset directory")
        return path

    def cache_files(self, directory):
        """Only the flat, tool-owned dataset layout is eligible for eviction."""
        info = directory.lstat()
        if (directory.parent != self.datasets or not stat.S_ISDIR(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or
                directory.resolve() != directory):
            raise ValueError("Unsafe dataset directory")
        files = []
        for path in directory.iterdir():
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or
                    getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError("Unexpected entry in dataset directory")
            if path.name not in {"manifest.json", ".job-lease"} and not re.fullmatch(
                r"(?:[0-9a-f-]{36}\.(?:log|part)|[0-9a-f]{32}\.tmp|\.read-lease-[0-9a-f]{32})",
                path.name,
            ):
                raise ValueError("Unexpected file in dataset directory")
            files.append((path, info.st_size))
        return files

    def prune(self):
        """Evict oldest managed datasets by creation time after writers/readers release them."""
        limit = self.cache_limit() if self.cache_limit else 0
        if not limit and not self.pending_cleanup.exists():
            return {"deleted": [], "pending": False}
        if self.datasets.resolve() != self.datasets:
            raise ValueError("Cache datasets directory must not be a symbolic link")
        with self.cache_guard():
            # Recheck under the lock so a concurrent settings change takes effect here.
            limit = self.cache_limit() if self.cache_limit else 0
            deleted, datasets, total, uncommitted = [], [], 0, 0
            pending = False
            for directory in self.datasets.iterdir():
                evicted = directory.name.startswith(".evicted-")
                try:
                    dataset_id = identifier(directory.name.removeprefix(".evicted-"))
                    files = self.cache_files(directory)
                    if evicted:
                        # A prior attempt may have been interrupted after unlinking some files.
                        for path, _ in files:
                            path.unlink(missing_ok=True)
                        directory.rmdir()
                        continue
                    manifest = self.read(dataset_id)
                    if manifest["dataset_id"] != dataset_id:
                        continue
                    created_at = datetime.fromisoformat(manifest["created_at"]).timestamp()
                    size = sum(size for _, size in files)
                    writer = directory / ".job-lease"
                    if manifest["status"] in ("queued", "downloading", "collecting") and writer.is_file():
                        lease = Lease(writer)
                        if not lease.acquire():
                            # The writer may still reject its dataset before publishing it.
                            uncommitted += size
                            continue
                        lease.close()
                except (OSError, ValueError, TypeError, KeyError, OverflowError):
                    pending |= evicted
                    continue
                total += size
                datasets.append((not manifest.get("cache_rejected", False), created_at,
                                 dataset_id, directory, size, files))

            for _, _, dataset_id, directory, size, files in sorted(datasets):
                if not limit or total <= limit or pending:
                    break
                locks = [directory / ".job-lease"]
                locks.extend(path for path, _ in files if path.name.startswith(".read-lease-"))
                try:
                    for path in locks:
                        lease = Lease(path)
                        if not lease.acquire():
                            pending = True
                            break
                        lease.close()
                    if pending:
                        break
                    tombstone = self.datasets / f".evicted-{dataset_id}"
                    self.pending_cleanup.touch()
                    directory.rename(tombstone)
                    deleted.append(dataset_id)
                    for path, _ in self.cache_files(tombstone):
                        path.unlink(missing_ok=True)
                    tombstone.rmdir()
                    total -= size
                except (OSError, ValueError):
                    pending = True
                    break
            pending |= bool(limit and total + uncommitted > limit)
            if pending:
                self.pending_cleanup.touch()
            else:
                self.pending_cleanup.unlink(missing_ok=True)
            return {"deleted": deleted, "pending": pending}

    def list(self):
        result = []
        with self.lock:
            for entry in self.datasets.iterdir():
                try:
                    data = self.read(entry.name)
                    result.append(
                        {
                            **({"kind": data["kind"], "bytes_saved": data.get("bytes_saved", 0)} if "kind" in data else {}),
                            **{key: data[key] for key in (
                                "dataset_id",
                                "source",
                                "created_at",
                                "status",
                                "bytes_downloaded",
                            )},
                        }
                    )
                except (ValueError, OSError, KeyError):
                    continue
        return sorted(result, key=lambda row: row["created_at"], reverse=True)
