"""End-to-end tests for the deep-zoom image upload/tile publish API.

Required self-test matrix:
  A. non-multiple-of-256 sizes: edge tiles keep every source pixel
  B. manifest geometry: levels down to a single tile, coordinates, revision
  C. tile fetching (200/404), media types, PNG and JPEG inputs
  D. replacing an image: old revision stays readable, pointer switches atomically
  E. concurrent re-uploads: strict revision order, newest wins, no missing files
  F. build failure: active manifest untouched, failed row recorded, cleanup
  G. restart recovery: interrupted 'building' rows and missing files demoted
  H. stale build cannot supersede a later upload
"""
from __future__ import annotations

import io
import json
import os
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, image_crud, tiling
from app.db import connect
from app.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_image(width: int, height: int, mode: str = "RGB",
               fmt: str = "PNG") -> bytes:
    """Deterministic gradient image so every pixel's expected value is known."""
    img = Image.new(mode, (width, height))
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (x % 256, y % 256, (x + y) % 256)  # type: ignore[index]
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def upload(client: TestClient, exhibit_id: str, payload: bytes,
           filename: str = "shot.png"):
    return client.post(
        f"/exhibits/{exhibit_id}/images",
        files={"file": (filename, payload, "image/png")},
    )


def expected_levels(width: int, height: int, tile_size: int = 256):
    return tiling.plan_pyramid(width, height, tile_size)


# ---------------------------------------------------------------------------
# A. Non-multiple dimensions: geometry and no lost content
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("width,height,fmt,ctype", [
    (300, 100, "PNG", "image/png"),          # both dims under one tile, odd
    (1000, 700, "PNG", "image/png"),         # non-multiple, multi-level
    (257, 257, "JPEG", "image/jpeg"),        # just over one tile
    (512, 300, "JPEG", "image/jpeg"),        # even width, odd height
    (1, 1, "PNG", "image/png"),              # degenerate smallest image
])
def test_non_multiple_sizes_keep_full_content(client, width, height, fmt, ctype):
    exhibit = f"EX-{width}x{height}-{fmt}"
    resp = upload(client, exhibit, make_image(width, height, fmt=fmt),
                  filename=f"shot.{ 'jpg' if fmt == 'JPEG' else 'png'}")
    assert resp.status_code == 201, resp.text
    manifest = resp.json()["manifest"]

    assert manifest["exhibit_id"] == exhibit
    assert manifest["revision"] == 1
    assert manifest["source_format"] == ("jpeg" if fmt == "JPEG" else "png")
    assert (manifest["width"], manifest["height"]) == (width, height)
    assert manifest["tile_size"] == 256

    geometry = expected_levels(width, height)
    assert manifest["max_level"] == geometry[-1].level
    assert [(lv["level"], lv["width"], lv["height"], lv["cols"], lv["rows"])
            for lv in manifest["levels"]] == [
        (g.level, g.width, g.height, g.cols, g.rows) for g in geometry
    ]

    # Level 0 must be exactly one tile, no larger than 256 on either side.
    top = manifest["levels"][0]
    assert top["level"] == 0
    assert top["cols"] == 1 and top["rows"] == 1
    assert top["width"] <= 256 and top["height"] <= 256
    # The deepest level preserves the original resolution exactly.
    deepest = manifest["levels"][-1]
    assert deepest["level"] == manifest["max_level"]
    assert (deepest["width"], deepest["height"]) == (width, height)

    # Every grid cell declared, edge tiles report actual (smaller) sizes,
    # and the declared tiles cover the full level extent — nothing cropped.
    for lv in manifest["levels"]:
        entries = [t for t in manifest["tiles"] if t["level"] == lv["level"]]
        assert len(entries) == lv["cols"] * lv["rows"]
        coords = {(t["x"], t["y"]) for t in entries}
        assert coords == {
            (x, y) for y in range(lv["rows"]) for x in range(lv["cols"])
        }
        for tile in entries:
            edge_x = tile["x"] == lv["cols"] - 1
            edge_y = tile["y"] == lv["rows"] - 1
            assert tile["width"] == (
                lv["width"] - tile["x"] * 256 if edge_x else 256
            )
            assert tile["height"] == (
                lv["height"] - tile["y"] * 256 if edge_y else 256
            )
            # Same revision on every tile URL.
            assert tile["url"] == (
                f"/exhibits/{exhibit}/tiles/1/{lv['level']}/{tile['x']}/{tile['y']}"
            )

    # Re-assemble the max-level from fetched tiles and compare every pixel
    # against the original: edge handling must not lose any picture content.
    original = Image.open(io.BytesIO(make_image(width, height, fmt=fmt)))
    rebuilt = Image.new(original.mode, (width, height))
    for tile in manifest["tiles"]:
        if tile["level"] != manifest["max_level"]:
            continue
        r = client.get(tile["url"])
        assert r.status_code == 200
        assert r.headers["content-type"] == ctype
        part = Image.open(io.BytesIO(r.content))
        assert part.size == (tile["width"], tile["height"])
        rebuilt.paste(part, (tile["x"] * 256, tile["y"] * 256))

    if fmt == "JPEG":
        # Lossy round trip: compare geometry, not exact pixels.
        assert rebuilt.size == original.size
    else:
        assert list(rebuilt.getdata()) == list(original.getdata())

    # The top level is a genuine downscale (whole image in one tile).
    top_url = manifest["tiles"][0]["url"]
    top_img = Image.open(io.BytesIO(client.get(top_url).content))
    assert top_img.size == (top["width"], top["height"])


def test_levels_halve_until_single_tile(client):
    # 1024x512 -> max level 2: L0 256x128 (one tile), L1 512x256, L2 1024x512.
    resp = upload(client, "EX-pyramid", make_image(1024, 512))
    levels = resp.json()["manifest"]["levels"]
    assert [(lv["level"], lv["width"], lv["height"]) for lv in levels] == [
        (0, 256, 128), (1, 512, 256), (2, 1024, 512),
    ]
    assert [(lv["cols"], lv["rows"]) for lv in levels] == [
        (1, 1), (2, 1), (4, 2),
    ]


def test_256_exact_image_has_single_level(client):
    resp = upload(client, "EX-256", make_image(256, 256))
    manifest = resp.json()["manifest"]
    assert manifest["max_level"] == 0
    assert len(manifest["levels"]) == 1
    assert manifest["levels"][0] == {
        "level": 0, "width": 256, "height": 256, "cols": 1, "rows": 1,
    }
    assert len(manifest["tiles"]) == 1


# ---------------------------------------------------------------------------
# C. Tile fetching, validation errors
# ---------------------------------------------------------------------------
def test_tile_404_for_bad_coordinates_and_unready_revision(client):
    upload(client, "EX-404", make_image(300, 300))
    # 300x300: level 0 is one 150x150 tile; deeper levels have a 2x2 grid.
    manifest = client.get("/exhibits/EX-404/manifest").json()
    deepest = manifest["max_level"]
    grid = next(lv for lv in manifest["levels"] if lv["level"] == deepest)
    assert (grid["cols"], grid["rows"]) == (2, 2)
    assert client.get(f"/exhibits/EX-404/tiles/1/{deepest}/2/0").status_code == 404
    assert client.get(f"/exhibits/EX-404/tiles/1/{deepest + 9}/0/0").status_code == 404
    assert client.get("/exhibits/EX-404/tiles/9/0/0/0").status_code == 404
    assert client.get("/exhibits/unknown/tiles/1/0/0/0").status_code == 404


def test_manifest_404_when_nothing_published(client):
    assert client.get("/exhibits/none/manifest").status_code == 404
    assert client.get("/exhibits/none/revisions").json() == []


def test_reject_non_image_and_empty_upload(client):
    resp = upload(client, "EX-bad", b"this is definitely not an image")
    assert resp.status_code == 415
    assert "PNG" in resp.json()["detail"]

    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, format="GIF")
    assert upload(client, "EX-gif", gif.getvalue()).status_code == 415

    empty = client.post(
        "/exhibits/EX-empty/images",
        files={"file": ("e.png", b"", "image/png")},
    )
    assert empty.status_code == 400

    # Rejected uploads leave no revisions behind.
    assert client.get("/exhibits/EX-bad/revisions").json() == []


def test_reject_bad_exhibit_id(client):
    # Characters outside the allow-list never reach storage (no path
    # traversal / filesystem tricks possible via the exhibit id).
    resp = upload(client, "bad id!", make_image(10, 10))
    assert resp.status_code == 400
    resp = upload(client, ".leadingdot", make_image(10, 10))
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# D. Replacing an image: old revision stays readable, atomic switch
# ---------------------------------------------------------------------------
def test_replacement_keeps_old_revision_readable_and_switches_pointer(client):
    first = upload(client, "EX-swap", make_image(400, 300)).json()
    assert first["previous_revision"] is None
    assert first["manifest"]["revision"] == 1

    second = upload(client, "EX-swap", make_image(800, 200)).json()
    assert second["previous_revision"] == 1
    assert second["manifest"]["revision"] == 2

    # Active manifest is the new one; old one is still fetchable explicitly.
    active = client.get("/exhibits/EX-swap/manifest").json()
    assert active["revision"] == 2
    assert (active["width"], active["height"]) == (800, 200)

    old = client.get("/exhibits/EX-swap/manifest", params={"revision": 1}).json()
    assert (old["width"], old["height"]) == (400, 300)

    # Tiles of both revisions resolve, each carrying its own revision.
    assert client.get(active["tiles"][0]["url"]).status_code == 200
    assert client.get(old["tiles"][0]["url"]).status_code == 200

    revisions = client.get("/exhibits/EX-swap/revisions").json()
    assert [(r["revision"], r["status"], r["active"]) for r in revisions] == [
        (1, "ready", False),
        (2, "ready", True),
    ]

    # Both revision directories exist on disk with complete file sets.
    for rev in (1, 2):
        base = tiling.revision_dir(config.TILE_ROOT, "EX-swap", rev)
        assert os.path.isfile(os.path.join(base, "manifest.json"))
        assert os.path.isdir(os.path.join(base, "tiles"))


# ---------------------------------------------------------------------------
# E. Concurrent re-uploads: ordering, newest wins, everything readable
# ---------------------------------------------------------------------------
def test_concurrent_reuploads_get_strict_revisions_and_newest_wins(client):
    # Seed an initial revision so there is always something active.
    upload(client, "EX-concurrent", make_image(256, 256))
    n_threads = 6
    errors: list[str] = []
    barrier = threading.Barrier(n_threads)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with TestClient(app) as local:
                resp = upload(local, "EX-concurrent",
                              make_image(260 + idx, 100 + idx))
                # Every build itself succeeds; 409 only means it lost the
                # active-pointer race against an even newer upload.
                if resp.status_code not in (201, 409):
                    errors.append(f"thread {idx}: {resp.status_code} {resp.text}")
        except Exception as exc:  # pragma: no cover
            errors.append(f"thread {idx}: {exc!r}")

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors

    revisions = client.get("/exhibits/EX-concurrent/revisions").json()
    numbers = [r["revision"] for r in revisions]
    # Revisions 2 .. n+1 allocated exactly once each, in arrival order.
    assert numbers == list(range(1, n_threads + 2))
    assert all(r["status"] == "ready" for r in revisions)
    assert sum(1 for r in revisions if r["active"]) == 1

    active = client.get("/exhibits/EX-concurrent/manifest").json()
    assert active["revision"] == n_threads + 1

    # No manifest points at a missing file: verify every revision's tile set.
    conn = connect()
    try:
        for r in revisions:
            row = image_crud.get_revision_row(
                conn, "EX-concurrent", r["revision"])
            assert row["status"] == "ready"
            manifest = json.loads(row["manifest"])
            assert tiling.verify_revision_files(
                config.TILE_ROOT, "EX-concurrent",
                r["revision"], row["source_format"], manifest)
    finally:
        conn.close()

    # No per-revision staging leftovers after successful concurrent builds.
    staging = os.path.join(config.TILE_ROOT, ".staging")
    if os.path.isdir(staging):
        assert os.listdir(staging) == []


# ---------------------------------------------------------------------------
# F. Build failure: active manifest untouched, failed row, files cleaned
# ---------------------------------------------------------------------------
def test_failed_build_keeps_active_manifest_and_records_failure(client, monkeypatch):
    upload(client, "EX-fail", make_image(256, 256))

    def broken_hook(exhibit_id, revision, staging_path):
        if revision == 2:
            raise RuntimeError("simulated tile writer crash")

    monkeypatch.setattr(tiling, "BUILD_HOOK", broken_hook)
    # raise_server_exceptions=False so the test observes the HTTP response a
    # real client would get instead of the exception crossing the test harness.
    with TestClient(app, raise_server_exceptions=False) as http:
        resp = upload(http, "EX-fail", make_image(300, 300))
    assert resp.status_code == 400

    # Active manifest stays at revision 1 and still serves.
    active = client.get("/exhibits/EX-fail/manifest").json()
    assert active["revision"] == 1
    assert client.get(active["tiles"][0]["url"]).status_code == 200

    revisions = client.get("/exhibits/EX-fail/revisions").json()
    assert revisions[1]["status"] == "failed"
    assert "simulated" in revisions[1]["error_message"]
    # A failed revision cannot be read as a manifest.
    assert client.get(
        "/exhibits/EX-fail/manifest", params={"revision": 2}).status_code == 404

    # The build died in staging (before the atomic publish), so no revision
    # directory exists; only the unique staging tree may be swept on restart.
    assert not os.path.exists(
        tiling.revision_dir(config.TILE_ROOT, "EX-fail", 2))
    # The failed build's own staging directory was removed.
    staging = os.path.join(config.TILE_ROOT, ".staging")
    assert not any(
        name.startswith("EX-fail__rev2__")
        for name in (os.listdir(staging) if os.path.isdir(staging) else [])
    )


# ---------------------------------------------------------------------------
# G. Restart recovery
# ---------------------------------------------------------------------------
def test_restart_recovers_interrupted_and_missing_file_revisions(client):
    upload(client, "EX-restart", make_image(256, 256))   # rev 1 active
    upload(client, "EX-restart", make_image(512, 256))   # rev 2 active

    conn = connect()
    try:
        # Simulate a process killed mid-build: row stuck at 'building' with
        # no owner row at all (its heartbeat died with the process).
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO image_revisions
                (exhibit_id, revision, source_format, width, height,
                 max_level, manifest, status, error_message, created_at,
                 owner_id)
            VALUES ('EX-restart', 3, 'png', 100, 100, 0, '', 'building',
                    NULL, '2026-11-19T00:00:00+00:00', 'dead-process')
            """
        )
        conn.commit()

        # And a ready revision whose tiles have been deleted from disk.
        rev2_dir = tiling.revision_dir(config.TILE_ROOT, "EX-restart", 2)
        assert os.path.isdir(rev2_dir)
        import shutil
        shutil.rmtree(rev2_dir)
    finally:
        conn.close()

    # Re-run the exact startup recovery the lifespan handler runs.
    conn = connect()
    try:
        stats = image_crud.recover_interrupted_builds(conn)
    finally:
        conn.close()
    assert stats["interrupted"] == 1
    assert stats["missing_files"] == 1

    revisions = client.get("/exhibits/EX-restart/revisions").json()
    statuses = {r["revision"]: r["status"] for r in revisions}
    assert statuses == {1: "ready", 2: "failed", 3: "failed"}

    # The active pointer must fall back to the highest intact ready revision.
    active = client.get("/exhibits/EX-restart/manifest").json()
    assert active["revision"] == 1
    assert client.get(active["tiles"][0]["url"]).status_code == 200

    # Recovery sweeps the directories of exactly the rows it marked failed
    # (rev 3 interrupted, rev 2 missing files); intact rev 1 is untouched.
    assert not os.path.exists(
        tiling.revision_dir(config.TILE_ROOT, "EX-restart", 3))
    assert not os.path.exists(
        tiling.revision_dir(config.TILE_ROOT, "EX-restart", 2))
    assert os.path.isdir(
        tiling.revision_dir(config.TILE_ROOT, "EX-restart", 1))


def test_restart_with_all_files_lost_hides_manifest_completely(client):
    upload(client, "EX-gone", make_image(256, 256))
    import shutil
    shutil.rmtree(tiling.revision_dir(config.TILE_ROOT, "EX-gone", 1))

    conn = connect()
    try:
        image_crud.recover_interrupted_builds(conn)
    finally:
        conn.close()

    assert client.get("/exhibits/EX-gone/manifest").status_code == 404
    revisions = client.get("/exhibits/EX-gone/revisions").json()
    assert revisions[0]["status"] == "failed" and revisions[0]["active"] is False


def test_recovery_spares_builds_owned_by_a_live_owner(client):
    # A second worker booting must not reap a build the first worker still
    # owns: that owner has a fresh heartbeat, so recovery leaves it alone.
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO image_revisions
                (exhibit_id, revision, source_format, width, height,
                 max_level, manifest, status, error_message, created_at,
                 owner_id)
            VALUES ('EX-live', 1, 'png', 64, 64, 0, '', 'building',
                    NULL, '2026-11-19T00:00:00+00:00', ?)
            """,
            (image_crud.OWNER_ID,),
        )
        image_crud.register_owner(conn)  # fresh heartbeat for THIS process
        conn.commit()

        stats = image_crud.recover_interrupted_builds(conn)
        assert stats["interrupted"] == 0
        row = image_crud.get_revision_row(conn, "EX-live", 1)
        assert row["status"] == "building"
    finally:
        conn.close()


def test_recovery_reaps_build_when_owner_heartbeat_is_stale(client):
    from datetime import datetime, timedelta, timezone
    stale = (
        datetime.now(timezone.utc)
        - timedelta(seconds=image_crud._OWNER_STALE_SECONDS + 60)
    ).isoformat()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO image_revisions
                (exhibit_id, revision, source_format, width, height,
                 max_level, manifest, status, error_message, created_at,
                 owner_id)
            VALUES ('EX-zombie', 1, 'png', 64, 64, 0, '', 'building',
                    NULL, '2026-11-19T00:00:00+00:00', 'zombie-owner')
            """
        )
        # A heartbeat row exists but is far older than the staleness window.
        conn.execute(
            "INSERT INTO build_owners (owner_id, last_beat) VALUES (?, ?)",
            ("zombie-owner", stale),
        )
        conn.commit()

        stats = image_crud.recover_interrupted_builds(conn)
        assert stats["interrupted"] == 1
        row = image_crud.get_revision_row(conn, "EX-zombie", 1)
        assert row["status"] == "failed" and row["owner_id"] is None
    finally:
        conn.close()


def test_builds_record_their_owner_and_release_it_on_finish(client):
    captured = {}

    def hook(exhibit_id, revision, staging_path):
        conn = connect()
        try:
            row = image_crud.get_revision_row(conn, exhibit_id, revision)
            captured["owner_during_build"] = row["owner_id"]
        finally:
            conn.close()

    import app.tiling as tiling_mod
    original_hook = tiling_mod.BUILD_HOOK
    tiling_mod.BUILD_HOOK = hook
    try:
        upload(client, "EX-owner", make_image(256, 256))
    finally:
        tiling_mod.BUILD_HOOK = original_hook

    assert captured["owner_during_build"] == image_crud.OWNER_ID
    conn = connect()
    try:
        row = image_crud.get_revision_row(conn, "EX-owner", 1)
        assert row["status"] == "ready" and row["owner_id"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# H. Earlier-starting build cannot supersede a later upload
# ---------------------------------------------------------------------------
def test_stale_build_cannot_override_newer_upload(client, monkeypatch):
    upload(client, "EX-stale", make_image(256, 256))  # rev 1 active

    release = threading.Event()
    inside = threading.Event()

    def blocking_hook(exhibit_id, revision, staging_path):
        if revision == 2:
            inside.set()
            release.wait(timeout=10)

    monkeypatch.setattr(tiling, "BUILD_HOOK", blocking_hook)

    def slow_build() -> None:
        with TestClient(app) as local:
            resp = upload(local, "EX-stale", make_image(400, 400))
            # Rev 2 finishes after rev 3 is already live -> conflict, not
            # silent override.
            assert resp.status_code == 409

    thread = threading.Thread(target=slow_build)
    thread.start()
    assert inside.wait(timeout=5)

    # A second upload fully completes while the first is still rendering.
    later = upload(client, "EX-stale", make_image(900, 150))
    assert later.status_code == 201
    assert later.json()["manifest"]["revision"] == 3

    release.set()
    thread.join(timeout=15)
    assert not thread.is_alive()

    active = client.get("/exhibits/EX-stale/manifest").json()
    assert active["revision"] == 3
    assert (active["width"], active["height"]) == (900, 150)

    # Rev 2 is still a complete, explicitly readable historical revision.
    rev2 = client.get(
        "/exhibits/EX-stale/manifest", params={"revision": 2}).json()
    assert (rev2["width"], rev2["height"]) == (400, 400)
    assert client.get(rev2["tiles"][0]["url"]).status_code == 200

    revisions = client.get("/exhibits/EX-stale/revisions").json()
    assert [(r["revision"], r["status"], r["active"]) for r in revisions] == [
        (1, "ready", False),
        (2, "ready", False),
        (3, "ready", True),
    ]


# ---------------------------------------------------------------------------
# Persistence: manifest JSON on disk mirrors the DB row
# ---------------------------------------------------------------------------
def test_manifest_file_on_disk_matches_database(client):
    upload(client, "EX-disk", make_image(333, 277))
    db_manifest = client.get("/exhibits/EX-disk/manifest").json()
    with open(
        os.path.join(
            tiling.revision_dir(config.TILE_ROOT, "EX-disk", 1),
            "manifest.json",
        ),
        encoding="utf-8",
    ) as fh:
        disk_manifest = json.load(fh)
    assert disk_manifest == db_manifest
