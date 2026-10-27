from pathlib import Path

import pytest

from log_tools.config import Settings
from log_tools.service import LogService


@pytest.fixture
def service(tmp_path):
    settings = Settings(cache_dir=tmp_path / "cache", local_roots=[tmp_path / "logs"])
    settings.local_roots[0].mkdir()
    service = LogService(settings)
    yield service
    service.close()


@pytest.fixture
def sample():
    return (Path(__file__).parents[1] / "examples/logs/order.log").read_text("utf-8")
