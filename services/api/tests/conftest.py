from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from centaur_pocket.config import Settings
from centaur_pocket.main import create_app

OWNER_TOKEN = "cp_owner_test-token"
AGENT_TOKEN = "cp_live_test-token"


@pytest.fixture
def watched_folder(tmp_path: Path) -> Path:
    path = tmp_path / "watched"
    path.mkdir()
    return path


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_root=tmp_path / "runtime",
        owner_token=OWNER_TOKEN,
        agent_token=AGENT_TOKEN,
        scheduler_poll_seconds=0,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def owner_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_TOKEN}"}


@pytest.fixture
def agent_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {AGENT_TOKEN}"}
