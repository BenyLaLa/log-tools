"""SFTP range/stream filtering. Only selected, redacted lines are written locally."""
from __future__ import annotations

import collections
import gzip
import hashlib
import posixpath
import threading
import time
import uuid
from contextlib import contextmanager

from .config import remote_path
from .query import metadata, parse_time, redact
from .store import now

CHUNK = 64 * 1024
MAX_LINE = 64 * 1024


class StopScan(Exception):
    pass


def bounded(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be {low}..{high}")
    return value


def version(attributes):
    return {"size": attributes.st_size, "mtime": attributes.st_mtime}


class Meter:
    def __init__(self, options):
        self.options = options
        self.deadline = time.monotonic() + options["timeout_seconds"]
        self.received = self.scanned = self.saved = self.lines = self.matches = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise StopScan("time_limit")


@contextmanager
def bounded_connection(connector, config, meter):
    # SSH connection/authentication has its own existing transport timeout. The scan timer
    # starts once connected and closes only this collection's connection to interrupt blocked IO.
    with connector(config) as remote:
        meter.deadline = time.monotonic() + meter.options["timeout_seconds"]
        timer = None
        if hasattr(remote, "close"):
            def expire():
                try:
                    remote.close()
                except Exception:
                    pass
            timer = threading.Timer(meter.options["timeout_seconds"], expire)
            timer.daemon = True
            timer.start()
        try:
            yield remote
        finally:
            if timer:
                timer.cancel()


class Reader:
    """Bound network payload bytes including gzip's read-ahead; never prefetch entire files."""
    def __init__(self, stream, start, end, meter):
        self.stream, self.start, self.end, self.meter = stream, start, end, meter
        self.position, self.received = start, 0
        stream.seek(start)

    def read(self, size=-1):
        self.meter.check()
        if size == 0:
            return b""
        if self.position >= self.end:
            return b""
        remaining = self.meter.options["network_bytes"] - self.meter.received
        file_remaining = self.meter.options["file_bytes"] - self.received
        if remaining <= 0 or file_remaining <= 0:
            raise StopScan("network_byte_limit" if remaining <= 0 else "file_byte_limit")
        count = min(CHUNK, self.end - self.position, remaining, file_remaining,
                    size if size >= 0 else CHUNK)
        self.stream.settimeout(max(0.1, min(10, self.meter.deadline - time.monotonic())))
        data = self.stream.read(count)
        if not data:
            raise StopScan("source_short_read")
        self.position += len(data)
        self.received += len(data)
        self.meter.received += len(data)
        return data


def records(source, start, end, encoding, meter, *, source_line=None, skip_first=False):
    """Keep byte positions without inventing absolute line numbers for range reads."""
    pending, offset = b"", start
    utf16 = encoding.startswith("utf-16")
    token = b"\n\x00" if encoding == "utf-16-le" else b"\x00\n" if utf16 else b"\n"
    number = source_line
    while True:
        meter.check()
        remaining = meter.options["scan_bytes"] - meter.scanned
        if remaining <= 0 and not (isinstance(source, Reader) and source.position >= source.end):
            raise StopScan("decoded_byte_limit")
        block = source.read(min(CHUNK, max(0, remaining)))
        meter.scanned += len(block)
        if meter.scanned > meter.options["scan_bytes"]:
            raise StopScan("decoded_byte_limit")
        pending += block
        consumed = 0
        while True:
            index = pending.find(token, consumed)
            if utf16:
                while index >= 0 and index % 2:
                    index = pending.find(token, index + 1)
            if index < 0:
                break
            raw = pending[consumed:index + len(token)]
            consumed = index + len(token)
            if len(raw) > MAX_LINE:
                raise StopScan("line_byte_limit")
            row = {"byte_start": offset, "byte_end": offset + len(raw), "source_line": number}
            offset += len(raw)
            if number is not None:
                number += 1
            if skip_first:
                skip_first = False
                continue
            row["text"] = raw.decode(encoding, errors="strict").rstrip("\r\n").lstrip("\ufeff")
            yield row
        pending = pending[consumed:]
        if len(pending) > MAX_LINE:
            raise StopScan("line_byte_limit")
        if not block:
            # A non-EOF range boundary can split a line: never pass that fragment as evidence.
            if pending and end is None and not skip_first:
                yield {"byte_start": offset, "byte_end": offset + len(pending), "source_line": number,
                       "text": pending.decode(encoding, errors="strict").rstrip("\r\n").lstrip("\ufeff")}
            break


class Collector:
    def __init__(self, settings, store, connector):
        self.settings, self.store, self.connector = settings, store, connector

    def collect(self, server, config, paths=None, mode="tail", query="", start_time=None,
                end_time=None, level=None, trace_id=None, before=20, after=60,
                tail_bytes=8 * 1024**2, max_network_bytes=None, max_file_bytes=None,
                max_output_bytes=None, timeout_seconds=None, limit=100, resume_dataset_id=None):
        if config.protocol != "sftp":
            raise ValueError("Fragment collection requires SFTP; FTP/FTPS full-download tools remain separate")
        options = {"network_bytes": max_network_bytes if max_network_bytes is not None else self.settings.fragment_network_bytes,
                   "file_bytes": max_file_bytes if max_file_bytes is not None else self.settings.fragment_file_bytes,
                   "output_bytes": max_output_bytes if max_output_bytes is not None else self.settings.fragment_output_bytes,
                   "scan_bytes": self.settings.fragment_scan_bytes,
                   "timeout_seconds": timeout_seconds if timeout_seconds is not None else self.settings.fragment_timeout_seconds}
        for key, cap in (("network_bytes", self.settings.fragment_network_bytes),
                         ("file_bytes", self.settings.fragment_file_bytes),
                         ("output_bytes", self.settings.fragment_output_bytes),
                         ("timeout_seconds", self.settings.fragment_timeout_seconds)):
            bounded(options[key], 1, cap, key)
        if resume_dataset_id:
            previous = self.store.read(resume_dataset_id)
            connection = {"host": config.host, "port": config.port, "username": config.username, "root": config.root}
            if previous.get("kind") != "fragments" or previous.get("source") != server or previous.get("connection") != connection:
                raise ValueError("Resume dataset does not match this SFTP project")
            state = previous["collection"]
            if not state.get("remaining"):
                raise ValueError("No resumable ranges remain; inspect warnings for skipped/changed/compressed files")
            parameters = state["parameters"]
            tasks = state["remaining"]
        else:
            if mode not in ("tail", "history", "full"):
                raise ValueError("mode must be tail/history/full")
            if not isinstance(paths, list) or not 1 <= len(paths) <= min(self.settings.max_files, 200):
                raise ValueError("Choose 1..200 explicit paths from list_remote_logs")
            paths = list(dict.fromkeys(remote_path(path, config.root) for path in paths))
            for path in paths:
                relative = posixpath.relpath(path, config.root)
                if (len(path) > 4096 or not path.endswith((".log", ".log.gz")) or
                        any(part in {"target", "third", "dist", ".git", *config.discovery.exclude_dirs}
                            for part in relative.split("/")[:-1])):
                    raise ValueError("Only allowed .log/.log.gz paths can be collected")
            if not isinstance(query, str) or len(query) > 1000 or (trace_id is not None and (not isinstance(trace_id, str) or len(trace_id) > 256)):
                raise ValueError("Search text too long")
            if not query and not trace_id and not level and not start_time and not end_time:
                level = "ERROR"  # Never save an entire unfiltered file by accident.
            if mode == "history" and not (start_time or end_time):
                raise ValueError("history mode requires start_time or end_time; select dated files with list_remote_logs")
            before, after = bounded(before, 0, 50, "before"), bounded(after, 0, 100, "after")
            bounded(limit, 1, 100, "limit")
            bounded(tail_bytes, 1024, 32 * 1024**2, "tail_bytes")
            level = level.upper() if level else None
            if level == "WARNING":
                level = "WARN"
            if level and level not in ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL", "CRITICAL"):
                raise ValueError("Unsupported log level")
            parameters = dict(mode=mode, query=query, start_time=start_time, end_time=end_time,
                              level=level, trace_id=trace_id, before=before, after=after,
                              tail_bytes=tail_bytes, limit=limit)
            tasks = [{"path": path} for path in paths]
        for task in tasks:
            path = remote_path(task["path"], config.root)
            if (len(path) > 4096 or not path.endswith((".log", ".log.gz")) or
                    any(part in {"target", "third", "dist", ".git", *config.discovery.exclude_dirs}
                        for part in posixpath.relpath(path, config.root).split("/")[:-1])):
                raise ValueError("Invalid fragment resume path")
            for name in ("offset", "end", "window_start", "window_end", "skip_matches_before", "processed_until"):
                if name in task:
                    bounded(task[name], 0, 10**15, name)
        start = parse_time(parameters["start_time"], self.settings.timezone_offset) if parameters["start_time"] else None
        end = parse_time(parameters["end_time"], self.settings.timezone_offset) if parameters["end_time"] else None
        if start and end and start > end:
            raise ValueError("start_time must be before end_time")
        manifest, lease = self.store.new(server, "utf-8", reserve=True)
        manifest.update(kind="fragments", status="collecting", parent_dataset_id=resume_dataset_id,
                        connection={"host": config.host, "port": config.port, "username": config.username, "root": config.root})
        state = manifest["collection"] = {"parameters": parameters, "limits": options, "coverage": [],
                                           "remaining": [], "warnings": [], "incomplete": True}
        meter = Meter(options)
        self.store.save(manifest)
        next_index = 0
        try:
            with bounded_connection(self.connector, config, meter) as remote:
                for index, task in enumerate(tasks):
                    if (meter.received >= options["network_bytes"] or meter.matches >= parameters["limit"]
                            or meter.saved >= options["output_bytes"] or meter.scanned >= options["scan_bytes"]
                            or meter.lines >= 10000 or time.monotonic() >= meter.deadline):
                        state["remaining"].extend(tasks[index:])
                        next_index = len(tasks)
                        state["warnings"].append({"reason": "task_budget_reached"})
                        break
                    self.file(remote, task, parameters, start, end, manifest, meter)
                    next_index = index + 1
        except Exception as error:
            # Do not persist SSH replies or credentials; completed fragment files remain queryable.
            state["warnings"].append({"reason": "collection_failed:" + type(error).__name__})
            manifest["errors"].append({"reason": type(error).__name__})
            state["remaining"].extend(tasks[next_index:])
        finally:
            state.update(network_bytes=meter.received, decoded_bytes=meter.scanned, saved_bytes=meter.saved,
                         matching_lines=meter.matches, remaining_count=len(state["remaining"]),
                         incomplete=bool(state["warnings"] or state["remaining"]),
                         scope="Only explicit selected files/ranges were inspected; SFTP attributes are not an atomic snapshot")
            manifest.update(status="partial" if state["incomplete"] else "completed",
                            bytes_downloaded=meter.received, bytes_saved=meter.saved, finished_at=now())
            self.store.save(manifest)
            lease.close()
        return {"dataset_id": manifest["dataset_id"], "status": manifest["status"], "kind": "fragments",
                "files": [{key: value for key, value in row.items() if key != "line_map"} for row in manifest["files"]], "collection": state,
                "resume_dataset_id": manifest["dataset_id"] if state["remaining"] else None,
                "local_directory": str(self.store.directory(manifest["dataset_id"])),
                "evidence": "Use search_logs/read_log_context on this dataset; cite source.path and source byte/line fields"}

    def file(self, remote, task, p, start_time, end_time, manifest, meter):
        state, path = manifest["collection"], task["path"]
        warnings = state["warnings"]
        coverage = {"path": path, "complete_file": False, "processed_until": task.get("offset", 0),
                    "network_bytes": 0, "matched_lines": 0, "context_truncated": False}
        state["coverage"].append(coverage)
        selected, ring = {}, collections.deque(maxlen=p["before"] + p["after"] + 2)
        pending_after, initial, checkpoint, reader = 0, None, None, None
        reason = None
        compressed = path.endswith(".gz")
        last_end = task.get("offset", 0)
        last_line = task.get("source_line", 1)
        window_lo = 0
        hi = 0
        received_before = meter.received
        try:
            if compressed and p["mode"] == "tail":
                raise StopScan("gzip_requires_history_or_full_stream")
            with remote.open_reader(path) as stream:
                initial = version(stream.stat())
                coverage["source_version"] = initial
                if task.get("version") and task["version"] != initial:
                    raise StopScan("source_changed_restart_search")
                size = initial["size"]
                lo, hi = task.get("offset", 0), size
                if p["mode"] == "tail":
                    hi = task.get("window_end", task.get("end", size))
                    lo = task.get("offset", max(0, hi - min(p["tail_bytes"], meter.options["file_bytes"])))
                    window_lo = task.get("window_start", lo)
                if lo > size or hi > size:
                    raise StopScan("source_changed_restart_search")
                encoding = self.settings.log_encoding
                header_cost = 0
                if encoding == "utf-16":
                    if compressed:
                        encoding = "utf-16-le"  # BOM is checked below after decompression.
                    else:
                        header = Reader(stream, 0, min(2, size), meter).read(2) if size else b""
                        header_cost = len(header)
                        if header not in (b"\xff\xfe", b"\xfe\xff"):
                            raise StopScan("utf16_bom_missing")
                        encoding = "utf-16-le" if header == b"\xff\xfe" else "utf-16-be"
                        lo -= lo % 2
                coverage.update(byte_start=lo, byte_end=hi, byte_unit="decompressed" if compressed else "source_file")
                coverage["leading_line_discarded"] = lo > 0 and "source_line" not in task and not compressed
                reader = Reader(stream, 0 if compressed else lo, size if compressed else hi, meter)
                reader.received += header_cost
                source = gzip.GzipFile(fileobj=reader, mode="rb") if compressed else reader
                if compressed and self.settings.log_encoding == "utf-16":
                    # Buffered prefix supports both BOM variants without a second decompression pass.
                    head = source.read(2)
                    meter.scanned += len(head)
                    if head not in (b"\xff\xfe", b"\xfe\xff"):
                        raise StopScan("utf16_bom_missing")
                    encoding = "utf-16-le" if head == b"\xff\xfe" else "utf-16-be"
                    lo = 2
                stamp = parse_time(task["initial_timestamp"], self.settings.timezone_offset) if task.get("initial_timestamp") else None
                severity = task.get("initial_level")
                unknown_time = 0
                line_start = 1 if lo == 0 or (compressed and lo == 2) else task.get("source_line")
                last_end = lo
                last_line = line_start - 1 if line_start is not None else None
                iterator = records(source, lo, None if compressed or hi == size else hi, encoding, meter,
                                   source_line=line_start, skip_first=lo > 0 and "source_line" not in task and not compressed)
                try:
                    for row in iterator:
                        meter.check()
                        current_stamp, current_level = metadata(row["text"], self.settings.timezone_offset)
                        if current_stamp:
                            stamp, severity = current_stamp, current_level
                        elif current_level:
                            stamp, severity = None, current_level
                        row.update(timestamp=stamp.isoformat() if stamp else None, level=severity)
                        match = p["query"].casefold() in row["text"].casefold()
                        match = match and (not p["trace_id"] or p["trace_id"] in row["text"])
                        match = match and (not p["level"] or severity == p["level"])
                        if start_time or end_time:
                            if stamp is None:
                                unknown_time += 1
                                match = False
                            else:
                                match = match and (not start_time or stamp >= start_time) and (not end_time or stamp <= end_time)
                        if match:
                            replayed = row["byte_start"] < task.get("skip_matches_before", 0)
                            if not replayed and meter.matches >= p["limit"]:
                                raise StopScan("match_limit")
                            if not replayed:
                                meter.matches += 1
                            coverage["matched_lines"] += 1
                            pending_after = p["after"]
                            # Save the hit before spending the remaining budget on preceding context.
                            self.select(selected, row, meter)
                            for previous in list(ring)[-p["before"]:] if p["before"] else []:
                                self.select(selected, previous, meter)
                        elif pending_after:
                            self.select(selected, row, meter)
                            pending_after -= 1
                        ring.append(row)
                        last_end, last_line = row["byte_end"], row["source_line"]
                finally:
                    iterator.close()
                    if compressed:
                        source.close()
                if unknown_time:
                    warnings.append({"path": path, "reason": "unknown_time_lines_skipped", "count": unknown_time})
                coverage["complete_file"] = lo == 0 and hi == size
                if p["mode"] == "tail" and window_lo:
                    overlap = min(CHUNK, max(1, (hi - window_lo) // 4))
                    checkpoint = {"path": path, "end": min(hi - 1, window_lo + overlap), "version": initial}
                    reason = "tail_range_only"
                elif p["mode"] == "tail" and hi < size:
                    reason = "tail_range_only"
                if hi == size:
                    pending_after = 0  # All context available at snapshot EOF was read.
                coverage["context_truncated"] = bool(pending_after)
                final = version(stream.stat())
                current = remote.metadata(path)
                if final != initial or current != initial:
                    checkpoint = None
                    reason = "source_changed_during_read"
        except (StopScan, OSError, EOFError, UnicodeError, ValueError) as error:
            reason = ("time_limit" if time.monotonic() >= meter.deadline else
                      str(error) if isinstance(error, StopScan) else "file_read_failed:" + type(error).__name__)
            coverage["context_truncated"] = bool(pending_after)
            if initial and reason in ("file_byte_limit", "network_byte_limit", "time_limit", "match_limit", "output_byte_limit", "saved_line_limit", "decoded_byte_limit"):
                if not compressed:
                    # Re-read only a bounded context overlap on resume; it counts toward the next budget.
                    anchor = ring[0] if ring else {"byte_start": task.get("offset", 0), "source_line": task.get("source_line")}
                    checkpoint = {"path": path, "offset": anchor["byte_start"], "version": initial}
                    checkpoint.update(initial_timestamp=anchor.get("timestamp"), initial_level=anchor.get("level"))
                    if anchor["source_line"] is not None:
                        checkpoint["source_line"] = anchor["source_line"]
                    if p["mode"] == "tail":
                        checkpoint.update(window_start=window_lo, window_end=hi)
                    checkpoint["processed_until"] = last_end
                    checkpoint["skip_matches_before"] = max(task.get("skip_matches_before", 0), last_end)
                    if task.get("processed_until", -1) >= last_end:
                        warnings.append({"path": path, "reason": "resume_no_progress_increase_budget_or_reduce_context"})
                else:
                    warnings.append({"path": path, "reason": "gzip_partial_not_resumable_restart_with_larger_budget"})
        except Exception as error:
            reason = "time_limit" if time.monotonic() >= meter.deadline else "file_read_failed:" + type(error).__name__
        if reason:
            warnings.append({"path": path, "reason": reason})
        if checkpoint:
            state["remaining"].append(checkpoint)
        if pending_after:
            warnings.append({"path": path, "reason": "context_ended_at_file_or_scan_boundary"})
        coverage.update(processed_until=last_end, last_source_line=last_line,
                        network_bytes=meter.received - received_before,
                        complete_file=coverage["complete_file"] and reason is None)
        self.save(selected, path, initial, compressed, manifest)

    def select(self, selected, row, meter):
        if row["byte_start"] in selected:
            return
        text = redact(row["text"], self.settings.redact)
        amount = len(text.encode("utf-8")) + 1
        if meter.saved + amount > meter.options["output_bytes"]:
            raise StopScan("output_byte_limit")
        if meter.lines >= 10000:
            raise StopScan("saved_line_limit")
        meter.saved += amount
        meter.lines += 1
        selected[row["byte_start"]] = {**row, "text": text}

    def save(self, selected, path, initial, compressed, manifest):
        rows = sorted(selected.values(), key=lambda row: row["byte_start"])
        groups = []
        for row in rows:
            if not groups or groups[-1][-1]["byte_end"] != row["byte_start"]:
                groups.append([])
            groups[-1].append(row)
        for group in groups:
            file_id = str(uuid.uuid4())
            data = ("\n".join(row["text"] for row in group) + "\n").encode("utf-8")
            destination = self.store.file_path(manifest["dataset_id"], file_id)
            with destination.open("xb") as stream:
                stream.write(data)
            manifest["files"].append({"file_id": file_id, "name": posixpath.basename(path) + " [fragment]",
                                      "remote_path": path, "bytes": len(data), "compressed": False,
                                      "sha256": hashlib.sha256(data).hexdigest(), "downloaded_at": now(),
                                      "source_version": initial, "source_compressed": compressed,
                                      "byte_unit": "decompressed" if compressed else "source_file",
                                      "initial_timestamp": group[0]["timestamp"], "initial_level": group[0]["level"],
                                      "line_map": [{key: row[key] for key in ("byte_start", "byte_end", "source_line")} for row in group]})
