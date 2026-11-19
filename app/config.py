"""Runtime configuration, driven by environment variables."""
from __future__ import annotations

import os

# SQLite database file location. Overridable for tests / containers.
DB_PATH = os.environ.get("EXHIBITION_DB", "/data/exhibition.db")

# Root directory holding original uploads, staged builds and published tiles.
# Lives on the same persistent volume as the database so that revisions survive
# container restarts.
TILE_ROOT = os.environ.get("EXHIBITION_TILE_ROOT", "/data/tiles")

# Edge length of every (square) tile in pixels.
TILE_SIZE = int(os.environ.get("EXHIBITION_TILE_SIZE", "256"))

# SQLite busy timeout in milliseconds: writers retry on SQLITE_BUSY
# instead of failing when another transaction holds the write lock.
BUSY_TIMEOUT_MS = int(os.environ.get("EXHIBITION_BUSY_TIMEOUT_MS", "5000"))
