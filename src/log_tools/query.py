from __future__ import annotations

import codecs
import gzip
import heapq
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?"
)
LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b", re.I)
SECRETS = re.compile(
    r"""(?i)(["']?(?:password|passwd|token|access_token|refresh_token|api_key|secret)["']?\s*[:=]\s*["']?)([^\s,"';}]+)"""
)
BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")


def redact(text, enabled=True):
    if not enabled:
        return text
    return BEARER.sub(r"\1[REDACTED]", SECRETS.sub(r"\1[REDACTED]", text))


def parse_time(value, offset):
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise ValueError("Time must be ISO 8601, e.g. 2026-10-22T10:00:00+08:00")
    stamp = datetime.fromisoformat(value.replace(",", ".").replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.fromisoformat("2000-01-01T00:00:00" + offset).tzinfo)
    return stamp


def metadata(line, offset):
    raw_time = None
    level = None
    if line.lstrip().startswith("{"):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                raw_time = obj.get("timestamp") or obj.get("@timestamp") or obj.get("time")
                level = obj.get("level") or obj.get("severity") or obj.get("log_level")
        except ValueError:
            pass
    if not raw_time and (match := TIMESTAMP.search(line)):
        raw_time = match.group()
    stamp = None
    if raw_time:
        try:
            stamp = parse_time(raw_time, offset)
        except ValueError:
            pass
    if not level and (match := LEVEL.search(line)):
        level = match.group()
    level = str(level).upper() if level else None
    return stamp, "WARN" if level == "WARNING" else level


class ScanLimit(Exception):
    pass


@dataclass
class Budget:
    max_bytes: int
    seconds: int
    scanned_bytes: int = 0
    deadline: float = field(init=False)

    def __post_init__(self):
        self.deadline = time.monotonic() + self.seconds

    def check(self):
        if time.monotonic() > self.deadline:
            raise ScanLimit("scan_timeout")


def lines(path, compressed, encoding, budget):
    """Stream text with bounded decompressed bytes and a bounded single-line buffer."""
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    pending = ""
    with gzip.open(path, "rb") if compressed else path.open("rb") as source:
        while True:
            budget.check()
            remaining = budget.max_bytes - budget.scanned_bytes
            block = source.read(min(64 * 1024, remaining + 1))
            if len(block) > remaining:
                raise ScanLimit("scan_byte_limit")
            budget.scanned_bytes += len(block)
            pending += decoder.decode(block, final=not block)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                if len(line) > 64 * 1024:
                    raise ScanLimit("line_exceeds_65536_characters")
                budget.check()
                yield line.rstrip("\r")
            if len(pending) > 64 * 1024:
                raise ScanLimit("line_exceeds_65536_characters")
            if not block:
                if pending:
                    yield pending.rstrip("\r")
                break


class Query:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def records(self, dataset_id, budget, file_id=None):
        manifest = self.store.read(dataset_id)
        if manifest["status"] in ("queued", "downloading", "collecting"):
            raise ValueError("Dataset is still downloading; check get_download_status first")
        files = manifest["files"]
        if file_id:
            files = [file for file in files if file["file_id"] == file_id]
            if not files:
                raise ValueError("File ID does not belong to this dataset")
        for file in files:
            stamp = parse_time(file["initial_timestamp"], self.settings.timezone_offset) if file.get("initial_timestamp") else None
            level = file.get("initial_level")
            path = self.store.file_path(dataset_id, file["file_id"])
            for number, text in enumerate(
                lines(path, file["compressed"], manifest["encoding"], budget), 1
            ):
                current_stamp, current_level = metadata(text, self.settings.timezone_offset)
                # A new event clears inherited severity; stack lines inherit preceding event metadata.
                if current_stamp is not None:
                    stamp, level = current_stamp, current_level
                elif current_level:
                    level = current_level
                origin = {}
                if "line_map" in file and number <= len(file["line_map"]):
                    mapping = file["line_map"][number - 1]
                    origin["source"] = {"path": file["remote_path"], "line": mapping["source_line"],
                                        "byte_start": mapping["byte_start"], "byte_end": mapping["byte_end"],
                                        "byte_unit": file["byte_unit"], "version": file["source_version"]}
                yield {
                    **origin,
                    "file_id": file["file_id"],
                    "filename": file["name"],
                    "line": number,
                    "timestamp": stamp.isoformat() if stamp else None,
                    "level": level,
                    "text": text,
                    "_stamp": stamp,
                    "_event_start": bool(current_stamp or current_level),
                }

    def public(self, record):
        result = {key: value for key, value in record.items() if not key.startswith("_")}
        text = redact(result["text"], self.settings.redact)
        result["text"] = text[:2000]
        result["text_truncated"] = len(text) > 2000
        return result

    def scope_warnings(self, dataset_id):
        manifest = self.store.read(dataset_id)
        if manifest.get("kind") == "fragments":
            warnings = ["fragment_dataset_contains_selected_context_only_not_original_files"]
            if manifest["collection"].get("incomplete"):
                warnings.append("remote_collection_incomplete_check_manifest_coverage_and_warnings")
            return warnings
        return []

    def search(
        self,
        dataset_id,
        query="",
        start_time=None,
        end_time=None,
        level=None,
        trace_id=None,
        limit=50,
        offset=0,
    ):
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise ValueError("limit must be 1..100 and offset 0..100000")
        if len(query) > 1000 or (trace_id and len(trace_id) > 256):
            raise ValueError("Search text too long")
        start = parse_time(start_time, self.settings.timezone_offset) if start_time else None
        end = parse_time(end_time, self.settings.timezone_offset) if end_time else None
        if start and end and start > end:
            raise ValueError("start_time must be before end_time")
        level = level.upper() if level else None
        if level == "WARNING":
            level = "WARN"
        if level and level not in ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL", "CRITICAL"):
            raise ValueError("Unsupported log level")
        budget = Budget(self.settings.scan_max_bytes, self.settings.scan_timeout_seconds)
        results, warnings, matches, unknown_times = [], self.scope_warnings(dataset_id), 0, 0
        more = False
        try:
            for record in self.records(dataset_id, budget):
                stamp = record["_stamp"]
                if (start or end) and stamp is None:
                    unknown_times += 1
                    continue
                if (start and stamp < start) or (end and stamp > end):
                    continue
                if level and record["level"] != level:
                    continue
                if query.casefold() not in record["text"].casefold():
                    continue
                if trace_id and trace_id not in record["text"]:
                    continue
                matches += 1
                if matches <= offset:
                    continue
                if len(results) == limit:
                    more = True
                    break
                results.append(self.public(record))
        except (ScanLimit, OSError, EOFError) as error:
            warnings.append(str(error) if isinstance(error, ScanLimit) else "file_read_failed")
        return {
            "dataset_id": dataset_id,
            "matches": results,
            "truncated": more or bool(warnings),
            "warnings": warnings,
            "next_offset": offset + len(results) if more else None,
            "scanned_bytes": budget.scanned_bytes,
            "unknown_time_lines_skipped": unknown_times,
        }

    def context(self, dataset_id, file_id, line, before=10, after=20):
        if line < 1 or not 0 <= before <= 50 or not 0 <= after <= 50:
            raise ValueError("line must be positive; before/after must be 0..50")
        budget = Budget(self.settings.scan_max_bytes, self.settings.scan_timeout_seconds)
        results, warnings = [], self.scope_warnings(dataset_id)
        try:
            for record in self.records(dataset_id, budget, file_id):
                if record["line"] >= max(1, line - before):
                    results.append(self.public(record))
                if record["line"] >= line + after:
                    break
        except (ScanLimit, OSError, EOFError) as error:
            warnings.append(str(error) if isinstance(error, ScanLimit) else "file_read_failed")
        return {
            "dataset_id": dataset_id,
            "file_id": file_id,
            "lines": results,
            "target_found": any(item["line"] == line for item in results),
            "truncated": bool(warnings),
            "warnings": warnings,
        }

    def latest_errors(self, dataset_id, limit=5):
        """Find event headers by event time, without treating stack lines as new errors."""
        if not 1 <= limit <= 20:
            raise ValueError("limit must be 1..20")
        budget = Budget(self.settings.scan_max_bytes, self.settings.scan_timeout_seconds)
        selected, warnings = [], self.scope_warnings(dataset_id)
        events = undated = 0
        try:
            for sequence, record in enumerate(self.records(dataset_id, budget)):
                stamp, severity = metadata(record["text"], self.settings.timezone_offset)
                if severity not in ("ERROR", "FATAL", "CRITICAL"):
                    continue
                # Untimestamped plain-text headers must start with the level. Arbitrary
                # occurrences of ERROR inside a stack/message are not event headers.
                standalone = re.match(r"^\s*\[?(ERROR|FATAL|CRITICAL)\b", record["text"], re.I)
                structured = record["text"].lstrip().startswith("{")
                if stamp is None and not (standalone or structured):
                    continue
                events += 1
                undated += int(stamp is None)
                record["timestamp"] = stamp.isoformat() if stamp else None
                record["level"] = severity
                rank = (stamp is not None, stamp.timestamp() if stamp else 0, sequence)
                item = (*rank, self.public(record))
                if len(selected) < limit:
                    heapq.heappush(selected, item)
                elif rank > selected[0][:3]:
                    heapq.heapreplace(selected, item)
        except (ScanLimit, OSError, EOFError) as error:
            warnings.append(str(error) if isinstance(error, ScanLimit) else "file_read_failed")
        if undated:
            warnings.append("undated_error_events_cannot_be_ordered_by_time")
        manifest = self.store.read(dataset_id)
        if manifest["status"] != "completed":
            warnings.append("dataset_is_not_complete")
        return {
            "dataset_id": dataset_id,
            "errors": [item[3] for item in sorted(selected, reverse=True)],
            "error_events_scanned": events,
            "undated_error_events": undated,
            "more_errors": events > limit,
            "truncated": bool(warnings),
            "warnings": warnings,
            "scanned_bytes": budget.scanned_bytes,
            "ordering": "timestamp descending; dated events first; ties use reverse source order",
            "scope": "Only the saved dataset files/fragments were inspected; this is not necessarily the full remote source",
        }

    def summary(self, dataset_id):
        budget = Budget(self.settings.scan_max_bytes, self.settings.scan_timeout_seconds)
        groups, warnings = {}, self.scope_warnings(dataset_id)
        total = 0
        try:
            for record in self.records(dataset_id, budget):
                if not record["_event_start"] or record["level"] not in (
                    "ERROR",
                    "FATAL",
                    "CRITICAL",
                ):
                    continue
                total += 1
                sample = self.public(record)
                key = TIMESTAMP.sub("<time>", sample["text"])
                key = re.sub(r"\b\d+\b", "<n>", key)
                if key not in groups:
                    if len(groups) >= 1000:
                        raise ScanLimit("error_group_limit")
                    groups[key] = {
                        "signature": key,
                        "count": 0,
                        "example": sample,
                        "first_seen": None,
                        "last_seen": None,
                    }
                group = groups[key]
                group["count"] += 1
                stamp = record["_stamp"]
                if stamp:
                    if group["first_seen"] is None or stamp < datetime.fromisoformat(
                        group["first_seen"]
                    ):
                        group["first_seen"] = stamp.isoformat()
                    if group["last_seen"] is None or stamp > datetime.fromisoformat(
                        group["last_seen"]
                    ):
                        group["last_seen"] = stamp.isoformat()
        except (ScanLimit, OSError, EOFError) as error:
            warnings.append(str(error) if isinstance(error, ScanLimit) else "file_read_failed")
        top = sorted(groups.values(), key=lambda group: group["count"], reverse=True)[:30]
        return {
            "dataset_id": dataset_id,
            "error_events": total,
            "groups": top,
            "truncated": bool(warnings) or len(groups) > 30,
            "warnings": warnings,
            "grouping": "heuristic; not a root-cause diagnosis",
        }
