"""Publish workflow: revision allocation, atomic activation and recovery.

Guarantees
----------
* Revisions are allocated under SQLite's write lock (``BEGIN IMMEDIATE``),
  so concurrent uploads of the same exhibit receive strictly increasing
  numbers in arrival order.
* The active pointer only moves in the same transaction that marks a
  revision ``ready``; all tile files are verified on disk beforehand.
* A revision that finishes after a newer upload has gone live cannot take
  over: the activation step re-checks that it is still the newest revision.
* Builds interrupted by a restart stay ``building`` and are marked
  ``failed`` at startup; ready revisions whose files have vanished are
  demoted the same way. The active pointer is then recomputed from the
  surviving ready revisions, so a manifest can never point at missing files.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import config, tiling
from .tiling import TilingError, _decode, _prepare_mode, plan_pyramid

# Stable identity of this process/worker for the lifetime of the process.
# Every build it starts is fenced with this owner id; only rows whose owner
# has a stale (or missing) heartbeat can be reclaimed by startup recovery.
OWNER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"

# Heartbeat cadence and the threshold after which an owner is considered
# dead. The interval is well below the threshold so ordinary scheduling
# jitter never trips it.
_HEARTBEAT_INTERVAL_SECONDS = 2.0
_OWNER_STALE_SECONDS = float(
    os.environ.get("EXHIBITION_OWNER_STALE_SECONDS", "30")
)
_heartbeat_timer: threading.Timer | None = None
_heartbeat_refs = 0
_heartbeat_lock = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def register_owner(conn: sqlite3.Connection) -> None:
    """Insert/refresh this process's owner heartbeat row."""
    conn.execute(
        """
        INSERT INTO build_owners (owner_id, last_beat)
        VALUES (?, ?)
        ON CONFLICT(owner_id) DO UPDATE SET last_beat = excluded.last_beat
        """,
        (OWNER_ID, _utc_now_iso()),
    )


def _heartbeat_tick() -> None:
    global _heartbeat_timer
    try:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                register_owner(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()
    except Exception:
        # A missed beat is harmless: the next tick retries, and the row only
        # goes stale after the much longer _OWNER_STALE_SECONDS window.
        pass
    _heartbeat_timer = threading.Timer(
        _HEARTBEAT_INTERVAL_SECONDS, _heartbeat_tick
    )
    _heartbeat_timer.daemon = True
    _heartbeat_timer.start()


def start_heartbeat() -> None:
    """Begin periodic owner heartbeats. Reference counted so nested app
    lifecycles (e.g. one TestClient per test thread) share one timer."""
    global _heartbeat_timer, _heartbeat_refs
    with _heartbeat_lock:
        _heartbeat_refs += 1
        if _heartbeat_timer is not None:
            return
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                register_owner(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()
        _heartbeat_tick()


def stop_heartbeat() -> None:
    global _heartbeat_timer, _heartbeat_refs
    with _heartbeat_lock:
        _heartbeat_refs = max(0, _heartbeat_refs - 1)
        if _heartbeat_refs == 0 and _heartbeat_timer is not None:
            _heartbeat_timer.cancel()
            _heartbeat_timer = None


def _connect() -> sqlite3.Connection:
    # Local import to avoid a circular import at module load time.
    from .db import connect
    return connect()


def _owner_is_stale(conn: sqlite3.Connection, owner_id: str | None,
                    now: datetime) -> bool:
    """True when the owner is absent or its heartbeat is past the threshold."""
    if owner_id is None:
        return True
    row = conn.execute(
        "SELECT last_beat FROM build_owners WHERE owner_id = ?",
        (owner_id,),
    ).fetchone()
    if row is None:
        return True
    try:
        last = datetime.fromisoformat(row["last_beat"])
    except ValueError:
        return True
    return (now - last) > timedelta(seconds=_OWNER_STALE_SECONDS)


def allocate_revision(
    conn: sqlite3.Connection, exhibit_id: str, source_format: str,
    width: int, height: int, max_level: int,
) -> int:
    """Insert the next ``building`` revision row. Caller holds a write txn."""
    row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) + 1 FROM image_revisions WHERE exhibit_id = ?",
        (exhibit_id,),
    ).fetchone()
    revision = row[0]
    conn.execute(
        """
        INSERT INTO image_revisions
            (exhibit_id, revision, source_format, width, height, max_level,
             manifest, status, error_message, created_at, owner_id)
        VALUES (?, ?, ?, ?, ?, ?, '', 'building', NULL, ?, ?)
        """,
        (exhibit_id, revision, source_format, width, height, max_level,
         _utc_now_iso(), OWNER_ID),
    )
    return revision


def latest_revision(conn: sqlite3.Connection, exhibit_id: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(revision) FROM image_revisions WHERE exhibit_id = ?",
        (exhibit_id,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def mark_failed(conn: sqlite3.Connection, exhibit_id: str, revision: int,
                message: str) -> None:
    """Record a build failure and release its owner claim. The active
    pointer is never touched here."""
    conn.execute(
        """
        UPDATE image_revisions
           SET status = 'failed', error_message = ?, owner_id = NULL
         WHERE exhibit_id = ? AND revision = ? AND status = 'building'
        """,
        (message, exhibit_id, revision),
    )


def activate_if_current(
    conn: sqlite3.Connection, exhibit_id: str, revision: int,
) -> bool:
    """Mark the revision ready and publish it — but only if still newest.

    Returns ``False`` when another (later) revision already exists; the
    older build then stays a complete, readable but inactive revision.
    Either way the row becomes ``ready`` with its full manifest persisted.
    """
    is_current = latest_revision(conn, exhibit_id) == revision
    conn.execute(
        "UPDATE image_revisions SET status = 'ready', owner_id = NULL "
        "WHERE exhibit_id = ? AND revision = ?",
        (exhibit_id, revision),
    )
    if is_current:
        conn.execute(
            """
            INSERT INTO exhibits (exhibit_id, active_revision, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(exhibit_id) DO UPDATE SET
                active_revision = excluded.active_revision,
                updated_at = excluded.updated_at
            """,
            (exhibit_id, revision, _utc_now_iso()),
        )
    return is_current


def get_active_revision(conn: sqlite3.Connection, exhibit_id: str) -> int | None:
    row = conn.execute(
        "SELECT active_revision FROM exhibits WHERE exhibit_id = ?",
        (exhibit_id,),
    ).fetchone()
    return row[0] if row else None


def get_revision_row(conn: sqlite3.Connection, exhibit_id: str,
                     revision: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM image_revisions WHERE exhibit_id = ? AND revision = ?",
        (exhibit_id, revision),
    ).fetchone()


def get_active_manifest_row(conn: sqlite3.Connection,
                            exhibit_id: str) -> sqlite3.Row | None:
    revision = get_active_revision(conn, exhibit_id)
    if revision is None:
        return None
    return get_revision_row(conn, exhibit_id, revision)


def list_revisions(conn: sqlite3.Connection, exhibit_id: str) -> list[dict]:
    active = get_active_revision(conn, exhibit_id)
    rows = conn.execute(
        "SELECT * FROM image_revisions WHERE exhibit_id = ? ORDER BY revision",
        (exhibit_id,),
    ).fetchall()
    return [dict(row, active=row["revision"] == active) for row in rows]


def _recompute_active(conn: sqlite3.Connection, exhibit_id: str) -> None:
    """Point the exhibit at its highest surviving ready revision, or nowhere."""
    row = conn.execute(
        """
        SELECT MAX(revision) FROM image_revisions
         WHERE exhibit_id = ? AND status = 'ready'
        """,
        (exhibit_id,),
    ).fetchone()
    new_active = row[0]
    if new_active is None:
        conn.execute(
            "DELETE FROM exhibits WHERE exhibit_id = ?", (exhibit_id,)
        )
    else:
        conn.execute(
            """
            INSERT INTO exhibits (exhibit_id, active_revision, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(exhibit_id) DO UPDATE SET
                active_revision = excluded.active_revision,
                updated_at = excluded.updated_at
            """,
            (exhibit_id, new_active, _utc_now_iso()),
        )


def publish_upload(conn: sqlite3.Connection, exhibit_id: str,
                   image_bytes: bytes) -> tuple[dict, int | None, bool]:
    """Run the whole publish flow for one uploaded original.

    Uses ``conn`` for short serialized transactions; the expensive tile
    rendering happens *between* them (no write lock held) at the revision's
    own directory, which stays unreachable through the API until the row is
    marked ready. Returns ``(manifest, previous_revision, became_active)``.
    """
    tiling.validate_exhibit_id(exhibit_id)

    # Decode before taking any lock so malformed uploads never consume a
    # revision number or leave a 'building' row behind.
    img, source_format = _decode(image_bytes)
    img = _prepare_mode(img, source_format)
    width, height = img.size
    geometry = plan_pyramid(width, height, config.TILE_SIZE)
    max_level = geometry[-1].level

    # Phase 1: serialize uploads and claim the next revision number.
    conn.execute("BEGIN IMMEDIATE")
    try:
        revision = allocate_revision(
            conn, exhibit_id, source_format, width, height, max_level,
        )
        previous = get_active_revision(conn, exhibit_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    revision_path = tiling.revision_dir(config.TILE_ROOT, exhibit_id, revision)
    staging_path: str | None = None
    try:
        # Phase 2: render everything into a unique staging directory (its name
        # embeds pid + revision, so concurrent builds never share a path).
        # No DB write lock is held while the CPU/IO work runs.
        staging_path, manifest = tiling.build_revision(
            exhibit_id, revision, image_bytes,
            tile_root=config.TILE_ROOT, tile_size=config.TILE_SIZE,
        )

        # Phase 3: atomic publish. Both staging and revision directories are
        # flat, uniquely named leaves under the (stable, pre-created) tile
        # root: no shared per-exhibit parent exists whose mkdir/rmdir could
        # interleave with a sibling build, and the rename itself is atomic.
        os.makedirs(config.TILE_ROOT, exist_ok=True)
        if os.path.exists(revision_path):  # pragma: no cover - defensive
            raise TilingError(f"修订目录已存在，拒绝覆盖: {revision_path}")
        os.replace(staging_path, revision_path)
        staging_path = None

        if not tiling.verify_revision_files(
            config.TILE_ROOT, exhibit_id, revision,
            source_format, manifest,
        ):
            raise TilingError("切片发布前校验发现缺失文件")

        # Phase 4: one transaction persists the full manifest and moves the
        # active pointer together. The staleness check means a build that
        # started earlier cannot overwrite a later, already live upload.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                UPDATE image_revisions
                   SET manifest = ?
                 WHERE exhibit_id = ? AND revision = ?
                """,
                (json.dumps(manifest, ensure_ascii=False), exhibit_id, revision),
            )
            became_active = activate_if_current(conn, exhibit_id, revision)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return manifest, previous, became_active
    except Exception as exc:
        # Failure handling only ever removes THIS build's private staging
        # directory (a uuid-unique path no other build can touch). The
        # published revision path is deliberately left alone here: sweeping
        # it under concurrency could interleave with another build, and any
        # directory belonging to a now-failed row is reclaimed at startup by
        # recover_interrupted_builds(). The previous active revision stays
        # fully served throughout. Unexpected errors normalize to HTTP 400.
        if not isinstance(exc, TilingError):
            exc = TilingError(f"切片构建失败: {exc!r}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            mark_failed(conn, exhibit_id, revision, str(exc)[:500])
            conn.commit()
        except Exception:
            conn.rollback()
        if staging_path is not None and os.path.isdir(staging_path):
            shutil.rmtree(staging_path, ignore_errors=True)
        raise exc


def recover_interrupted_builds(conn: sqlite3.Connection) -> dict[str, int]:
    """Startup recovery. Safe to run while other workers are already live.

    * A ``building`` row is only reaped when its owning process's heartbeat
      is stale (or it has no owner): that owner is genuinely dead. A build
      owned by a still-heartbeating worker — including a second worker
      booting while the first serves — is left completely untouched.
    * ``ready`` rows whose files are missing are demoted to ``failed`` and
      their (already incomplete) directories are swept.
    * Active pointers are recomputed per affected exhibit, so every active
      manifest resolves to a complete, verified revision on disk.

    Only directories belonging to rows this call itself marks failed are
    removed. The shared ``.staging`` tree is never bulk-deleted: a live
    worker may be building in it, and staging directories are uuid-unique.
    """
    stats = {"interrupted": 0, "missing_files": 0, "affected_exhibits": 0}
    to_sweep: list[tuple[str, int]] = []
    now = _utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        building = conn.execute(
            "SELECT exhibit_id, revision, owner_id "
            "FROM image_revisions WHERE status = 'building'"
        ).fetchall()
        reaped: list[sqlite3.Row] = []
        for row in building:
            if not _owner_is_stale(conn, row["owner_id"], now):
                # Owner is alive and (presumably) still building. Hands off.
                continue
            mark_failed(conn, row["exhibit_id"], row["revision"],
                        "构建进程已退出（心跳过期），启动恢复标记失败")
            to_sweep.append((row["exhibit_id"], row["revision"]))
            reaped.append(row)
            stats["interrupted"] += 1

        ready = conn.execute(
            "SELECT exhibit_id, revision, source_format, manifest "
            "FROM image_revisions WHERE status = 'ready'"
        ).fetchall()
        affected: set[str] = set()
        for row in ready:
            manifest = json.loads(row["manifest"])
            if not tiling.verify_revision_files(
                config.TILE_ROOT, row["exhibit_id"], row["revision"],
                row["source_format"], manifest,
            ):
                conn.execute(
                    """
                    UPDATE image_revisions
                       SET status = 'failed',
                           error_message = '重启校验发现瓦片/清单文件缺失'
                     WHERE exhibit_id = ? AND revision = ?
                    """,
                    (row["exhibit_id"], row["revision"]),
                )
                to_sweep.append((row["exhibit_id"], row["revision"]))
                stats["missing_files"] += 1
                affected.add(row["exhibit_id"])

        for row in reaped:
            affected.add(row["exhibit_id"])

        for exhibit_id in affected:
            _recompute_active(conn, exhibit_id)
        stats["affected_exhibits"] = len(affected)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Filesystem cleanup outside the DB lock, strictly limited to the rows
    # this call just failed. Each revision directory is a uniquely named
    # leaf, so removing it cannot disturb any other (possibly live) build.
    for exhibit_id, revision in to_sweep:
        shutil.rmtree(
            tiling.revision_dir(config.TILE_ROOT, exhibit_id, revision),
            ignore_errors=True,
        )
    return stats
