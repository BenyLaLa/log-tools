from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


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
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.datasets = self.root / "datasets"
        self.datasets.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def directory(self, dataset_id):
        path = (self.datasets / identifier(dataset_id)).resolve()
        if not path.is_relative_to(self.root):
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
            temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
            os.replace(temp, directory / "manifest.json")

    def new(self, source, encoding):
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
        self.save(manifest)
        return manifest

    def file_path(self, dataset_id, file_id):
        path = (self.directory(dataset_id) / (identifier(file_id) + ".log")).resolve()
        if not path.is_relative_to(self.directory(dataset_id)):
            raise ValueError("File path escaped dataset directory")
        return path

    def list(self):
        result = []
        with self.lock:
            for entry in self.datasets.iterdir():
                try:
                    data = self.read(entry.name)
                    result.append(
                        {
                            key: data[key]
                            for key in (
                                "dataset_id",
                                "source",
                                "created_at",
                                "status",
                                "bytes_downloaded",
                            )
                        }
                    )
                except (ValueError, OSError, KeyError):
                    continue
        return sorted(result, key=lambda row: row["created_at"], reverse=True)
