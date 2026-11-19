"""Pytest fixtures: each test gets an isolated SQLite file, tile dir and app."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

# Make the app package importable when pytest is run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import config  # noqa: E402
from app import image_crud  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    tmp_dir = tempfile.mkdtemp(prefix="exhibit-test-")
    db_path = os.path.join(tmp_dir, "test.db")
    tile_root = os.path.join(tmp_dir, "tiles")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "TILE_ROOT", tile_root)
    # Recovery treats a build owner without a fresh heartbeat as dead; keep
    # the window short so tests do not wait on the production 30s default.
    monkeypatch.setattr(image_crud, "_OWNER_STALE_SECONDS", 1.0)
    init_db(db_path)
    # The lifespan handler also runs init_db() and startup recovery against
    # the same paths, both of which are harmless no-ops on a fresh fixture.
    with TestClient(app) as test_client:
        yield test_client
