"""Bounded metadata discovery with short-lived, stable pagination snapshots."""
from __future__ import annotations

import fnmatch
import posixpath
import threading
import time
import uuid
from collections import deque

from .config import remote_path
from .remote import modification_time, valid_child


class Discovery:
    def __init__(self):
        self.snapshots = {}
        self.lock = threading.Lock()

    def list(self, connector, config, server, directory=".", pattern=None, limit=100,
             recursive=None, max_depth=None, cursor=None, *, all_files=False):
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        if cursor:
            try:
                key, offset = cursor.split(":")
                offset = int(offset)
            except (ValueError, AttributeError):
                raise ValueError("Invalid discovery cursor") from None
            with self.lock:
                snapshot = self.snapshots.get(key)
                if not snapshot or snapshot["expires"] < time.monotonic():
                    self.snapshots.pop(key, None)
                    raise ValueError("Discovery cursor expired; start a new scan")
                if snapshot["server"] != server or not 0 <= offset < len(snapshot["files"]):
                    raise ValueError("Discovery cursor does not match this server/page")
            return self.page(snapshot, key, offset, limit)

        options = config.discovery
        directory = remote_path(directory, config.root)
        pattern = options.pattern if pattern is None else pattern
        recursive = options.enabled if recursive is None else recursive
        max_depth = options.max_depth if max_depth is None else max_depth
        if not 0 <= max_depth <= 10 or not 1 <= len(pattern) <= 200:
            raise ValueError("max_depth must be 0..10 and pattern 1..200 characters")
        relative = posixpath.relpath(directory, config.root)
        parts = [] if relative == "." else relative.split("/")
        if any(part in options.exclude_dirs for part in parts):
            raise ValueError("Requested directory is excluded by project discovery settings")
        files, directories, skipped, warnings = [], [], [], []
        scanned_entries = scanned_directories = skipped_count = warning_count = 0

        def skip(path, reason):
            nonlocal skipped_count
            skipped_count += 1
            if len(skipped) < 100:
                skipped.append({"path": path, "reason": reason})

        def warn(path, reason):
            nonlocal warning_count
            warning_count += 1
            if len(warnings) < 100:
                warnings.append({"path": path, "reason": reason})

        queue = deque([(directory, len(parts))])
        # Bound the whole traversal as well as each transport operation's own timeout.
        deadline = time.monotonic() + 20
        with connector(config) as client:
            while queue:
                path, depth = queue.popleft()
                if scanned_entries >= options.max_entries or time.monotonic() >= deadline:
                    warn(path, "entry_limit" if scanned_entries >= options.max_entries else "time_limit")
                    break
                if len(directories) < 200:
                    directories.append(path)
                scanned_directories += 1
                iterator = iter(client.entries(path))
                try:
                    while True:
                        if scanned_entries >= options.max_entries or time.monotonic() >= deadline:
                            warn(path, "entry_limit" if scanned_entries >= options.max_entries else "time_limit")
                            queue.clear()
                            break
                        try:
                            item = next(iterator)
                        except StopIteration:
                            break
                        scanned_entries += 1
                        name = item["name"]
                        # FTP cdir/pdir entries are protocol metadata, not children.
                        if name in (".", ".."):
                            continue
                        try:
                            child = valid_child(path, name, config.root)
                        except ValueError:
                            warn(path, "invalid_child_name")
                            continue
                        kind = item["type"]
                        if kind == "directory":
                            if name in options.exclude_dirs:
                                skip(child, "excluded")
                            elif not recursive:
                                skip(child, "recursive_disabled")
                            elif path == config.root and name not in options.history_dirs:
                                skip(child, "outside_history_dirs")
                            elif depth >= max_depth:
                                skip(child, "depth_limit")
                                warn(child, "depth_limit")
                            else:
                                queue.append((child, depth + 1))
                        elif kind == "file" and fnmatch.fnmatchcase(name, pattern):
                            try:
                                modified = modification_time(item["mtime"])
                            except (ValueError, TypeError, OverflowError, OSError):
                                modified = None
                            files.append({"remote_path": child, "relative_path": posixpath.relpath(child, config.root),
                                          "name": name, "size": item["size"], "modified_at": modified})
                        elif kind in ("symlink", "other"):
                            skip(child, "symlink_or_unsupported_type")
                except Exception as error:
                    # Never expose server reply text, which may contain sensitive data.
                    warn(path, "directory_unreadable:" + type(error).__name__)
                finally:
                    if hasattr(iterator, "close"):
                        iterator.close()

        files.sort(key=lambda row: row["remote_path"])
        snapshot = {"server": server, "directory": directory, "pattern": pattern,
                    "recursive": recursive, "max_depth": max_depth,
                    "history_dirs": options.history_dirs, "exclude_dirs": options.exclude_dirs,
                    "files": files, "scanned_entries": scanned_entries,
                    "scanned_directories": scanned_directories, "directories": directories,
                    "skipped_directories": skipped, "skipped_count": skipped_count,
                    "warnings": warnings, "warning_count": warning_count,
                    "diagnostics_truncated": skipped_count > len(skipped) or warning_count > len(warnings) or scanned_directories > len(directories),
                    "incomplete": bool(warning_count), "expires": time.monotonic() + 300}
        if all_files:
            return self.page(snapshot, None, 0, len(files))
        key = uuid.uuid4().hex
        with self.lock:
            self.snapshots = {k: v for k, v in self.snapshots.items() if v["expires"] > time.monotonic()}
            while len(self.snapshots) >= 8:
                self.snapshots.pop(next(iter(self.snapshots)))
            self.snapshots[key] = snapshot
        return self.page(snapshot, key, 0, limit)

    @staticmethod
    def page(snapshot, key, offset, limit):
        files = snapshot["files"]
        end = offset + limit
        has_more = end < len(files)
        return {**{k: v for k, v in snapshot.items() if k not in ("files", "expires")},
                "files": files[offset:end], "total_files": len(files),
                "has_more": has_more, "next_cursor": f"{key}:{end}" if has_more else None,
                "truncated": snapshot["incomplete"] or has_more,
                "snapshot_ttl_seconds": 300}
