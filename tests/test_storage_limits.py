from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from log_tools.config import MIB, load_settings
from log_tools.desktop_config import DesktopSettings
from log_tools.service import LogService
from log_tools.store import Store


def cached_log(store, day, size):
    dataset = store.new("test", "utf-8")
    dataset.update(created_at=f"2026-10-{day:02d}T00:00:00+00:00", status="completed")
    file_id = str(uuid4())
    store.file_path(dataset["dataset_id"], file_id).write_bytes(b"a" * size)
    store.save(dataset)
    return dataset["dataset_id"]


def test_cache_prunes_in_order_after_readers_release(tmp_path):
    ui = DesktopSettings(tmp_path)
    service = LogService(load_settings(ui.path), config_path=ui.path)
    store = service.store
    try:
        oldest = cached_log(store, 1, 300)
        newer = cached_log(store, 2, 400)
        newest = cached_log(store, 3, 500)
        newest_size = sum(file.stat().st_size for file in store.directory(newest).iterdir())
        unrelated = store.datasets / "notes.log"
        unrelated.write_text("leave me alone")
        with store.reading(oldest), Store(store.root).reading(oldest):
            ui.set_storage_limits(newest_size, MIB)
            from_ui = Store(store.root, lambda: load_settings(ui.path).max_cache_bytes)
            assert from_ui.prune() == {"deleted": [], "pending": True}
            assert store.directory(newer).is_dir()
        assert [row["dataset_id"] for row in store.list()] == [newest]
        assert unrelated.read_text() == "leave me alone"
        assert not store.pending_cleanup.exists()
    finally:
        service.close()


def test_running_service_uses_updated_download_limit_for_batch(tmp_path):
    ui = DesktopSettings(tmp_path)
    ui.update_server("example", {"host": "example.test", "username": "reader", "root": "/logs"})
    downloads = []

    class Remote:
        def metadata(self, path):
            return {"size": 6, "mtime": 0}

        def download(self, path, consume):
            downloads.append(path)
            consume(b"sample")

    @contextmanager
    def connect(_):
        yield Remote()

    service = LogService(load_settings(ui.path), connect, config_path=ui.path)
    try:
        old = cached_log(service.store, 1, 1000)
        ui.set_storage_limits(1500, 10)
        job_id = service.download("example", ["/logs/first.log", "/logs/second.log"])["job_id"]
        service.jobs[job_id].result(timeout=5)
        service.close()  # Wait for the download's cleanup callback.
        result = service.status(job_id)
        assert result["status"] == "partial"
        assert result["bytes_downloaded"] == 6
        assert result["errors"] == [{"remote_path": "/logs/second.log", "reason": "download_size_limit"}]
        assert downloads == ["/logs/first.log"]
        assert not service.store.directory(old).exists()
    finally:
        service.close()


def test_cache_skips_active_writer(tmp_path):
    store = Store(Path(tmp_path), lambda: MIB)
    old = cached_log(store, 1, 300)
    manifest, lease = store.new("test", "utf-8", reserve=True)
    dataset_id = manifest["dataset_id"]
    try:
        store.file_path(dataset_id, str(uuid4())).with_suffix(".part").write_bytes(b"a" * MIB)
        assert store.prune() == {"deleted": [], "pending": True}
        assert store.directory(old).exists()
        store.finish(manifest)
    finally:
        lease.close()
    assert store.prune() == {"deleted": [], "pending": False}
    assert store.directory(old).exists()


def test_log_at_quota_with_manifest_does_not_evict_existing_log(service):
    service.settings.max_cache_bytes = MIB
    oldest = cached_log(service.store, 1, 300)
    source = service.settings.local_roots[0] / "at-cache-limit.log"
    source.write_bytes(b"a" * MIB)
    with pytest.raises(ValueError, match="exceeds the cache limit including its manifest"):
        service.import_local([str(source)])
    assert service.store.directory(oldest).exists()
    assert source.stat().st_size == MIB
    rejected = next(item for item in service.store.list() if item["dataset_id"] != oldest)
    assert rejected["status"] == "failed"
    assert not list(service.store.directory(rejected["dataset_id"]).glob("*.log"))
