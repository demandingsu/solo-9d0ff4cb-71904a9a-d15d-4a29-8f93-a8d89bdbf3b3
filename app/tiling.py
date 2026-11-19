"""Image pyramid tiling: planning, rendering and on-disk build layout.

The build is fully staged: everything for one revision is written under
``TILE_ROOT/.staging/<exhibit>/<revision>/`` and atomically renamed into the
published tree only after every tile and the manifest have been verified.
The database therefore never has to reference a half-written directory.

Pyramid geometry
----------------
The original image keeps its full resolution at ``max_level``; each lower
level halves both dimensions (rounding *up*, so no row/column is ever
dropped), until level 0 fits inside a single 256 px tile. Edge tiles are
cropped at the image boundary instead of being padded, so their recorded
width/height may be smaller than ``tile_size`` and no picture content is
lost.
"""
from __future__ import annotations

import io
import json
import os
import re
import uuid
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from . import config

EXHIBIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

SUPPORTED_FORMATS = {"PNG": "png", "JPEG": "jpeg"}
_EXTENSIONS = {"png": ".png", "jpeg": ".jpg"}
_MEDIA_TYPES = {"png": "image/png", "jpeg": "image/jpeg"}


class TilingError(Exception):
    """Raised when an upload cannot be parsed or tiled."""


class UnsupportedImage(TilingError):
    """Raised when the uploaded bytes are not a PNG or JPEG image."""


def validate_exhibit_id(exhibit_id: str) -> None:
    if not EXHIBIT_ID_RE.fullmatch(exhibit_id):
        raise TilingError(
            "展品号只能含字母、数字、点、下划线或短横线，且以字母数字开头（最长 128）"
        )


def media_type(source_format: str) -> str:
    return _MEDIA_TYPES[source_format]


def extension(source_format: str) -> str:
    return _EXTENSIONS[source_format]


def staging_dir(tile_root: str, exhibit_id: str, revision: int,
                token: str | None = None) -> str:
    # Globally unique name (revision + pid + uuid): concurrent builds never
    # share a directory, and no build can ever mistake another build's
    # staging directory for its own when cleaning up after a failure.
    token = token or uuid.uuid4().hex
    return os.path.join(
        tile_root, ".staging",
        f"{exhibit_id}__rev{revision}__{os.getpid()}__{token}",
    )


def revision_dir(tile_root: str, exhibit_id: str, revision: int) -> str:
    # Flat published layout as well: one uniquely named directory per
    # revision directly under the tile root. There is no shared per-exhibit
    # parent whose mkdir/rmdir could interleave between concurrent builds;
    # publishing is a single atomic os.replace between two flat directories.
    return os.path.join(tile_root, f"{exhibit_id}__rev{revision}")


def tile_relpath(level: int, x: int, y: int, source_format: str) -> str:
    return os.path.join(
        "tiles", str(level), f"{x}_{y}{extension(source_format)}"
    )


def tile_url(exhibit_id: str, revision: int, level: int, x: int, y: int) -> str:
    return (
        f"/exhibits/{exhibit_id}/tiles/{revision}/{level}/{x}/{y}"
    )


@dataclass(frozen=True)
class LevelGeometry:
    level: int
    width: int
    height: int
    cols: int
    rows: int


def plan_pyramid(width: int, height: int, tile_size: int) -> list[LevelGeometry]:
    """Compute every level from the single-tile top (0) to the full image.

    Level 0 is the smallest downscale (whole image in one tile); the last
    level keeps the original resolution. Levels are returned ascending, so
    ``levels[-1].level`` is ``max_level``.
    """
    if width <= 0 or height <= 0:
        raise TilingError("图像尺寸必须为正数")

    max_level = 0
    while (
        (width + (1 << max_level) - 1) >> max_level > tile_size
        or (height + (1 << max_level) - 1) >> max_level > tile_size
    ):
        max_level += 1

    levels: list[LevelGeometry] = []
    for level in range(max_level + 1):
        # Level 0 divides the original by 2**max_level; max_level by 1.
        divisor = 1 << (max_level - level)
        level_width = (width + divisor - 1) // divisor
        level_height = (height + divisor - 1) // divisor
        cols = (level_width + tile_size - 1) // tile_size
        rows = (level_height + tile_size - 1) // tile_size
        levels.append(
            LevelGeometry(level, level_width, level_height, cols, rows)
        )
    return levels


def _decode(image_bytes: bytes) -> tuple[Image.Image, str]:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UnsupportedImage("无法识别的图像文件，仅支持 PNG 或 JPEG") from exc
    if img.format not in SUPPORTED_FORMATS:
        raise UnsupportedImage(
            f"仅支持 PNG 或 JPEG，收到 {img.format or '未知格式'}"
        )
    return img, SUPPORTED_FORMATS[img.format]


def _prepare_mode(img: Image.Image, source_format: str) -> Image.Image:
    # JPEG cannot store alpha; flatten onto white rather than crash or drop
    # transparency silently to a black background.
    if source_format == "jpeg" and img.mode != "RGB":
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
        else:
            background.paste(img.convert("RGB"))
        return background
    return img


# Test seam: a test may replace this with a callable raising (or blocking)
# for a specific exhibit/revision to simulate broken/interleaved builds.
# Signature: (exhibit_id: str, revision: int, staging_path: str) -> None
BUILD_HOOK = None


def build_revision(
    exhibit_id: str,
    revision: int,
    image_bytes: bytes,
    tile_root: str | None = None,
    tile_size: int | None = None,
) -> tuple[str, dict]:
    """Render one full revision into a unique, process-private staging dir.

    The staging path embeds the pid, so concurrent builds — even across
    uvicorn workers — never share or nest directories. The caller promotes
    the directory to its final revision path with an atomic
    :func:`os.replace`. Returns ``(staging_path, manifest)``. Raises
    :class:`TilingError` (subclasses included) on any failure; the staging
    directory is removed before the exception propagates.
    """
    tile_root = tile_root or config.TILE_ROOT
    tile_size = tile_size or config.TILE_SIZE

    img, source_format = _decode(image_bytes)
    img = _prepare_mode(img, source_format)
    width, height = img.size
    levels = plan_pyramid(width, height, tile_size)
    max_level = levels[-1].level

    target = staging_dir(tile_root, exhibit_id, revision)
    if os.path.exists(target):
        raise TilingError(f"构建暂存目录已存在: {target}")
    os.makedirs(target)

    try:
        # Keep the exact bytes the client sent as the revision's original.
        original_path = os.path.join(target, f"original{extension(source_format)}")
        with open(original_path, "wb") as fh:
            fh.write(image_bytes)

        ext = extension(source_format)
        save_kwargs = {"png": {"compress_level": 6}, "jpeg": {"quality": 90}}[
            source_format
        ]
        level_entries: list[dict] = []
        tile_entries: list[dict] = []

        for geometry in levels:
            if geometry.level == max_level:
                # The deepest level is the original resolution, no resampling.
                scaled = img
            else:
                scaled = img.resize(
                    (geometry.width, geometry.height), Image.Resampling.LANCZOS
                )
            tiles_dir = os.path.join(target, "tiles", str(geometry.level))
            os.makedirs(tiles_dir)

            for ty in range(geometry.rows):
                for tx in range(geometry.cols):
                    left = tx * tile_size
                    upper = ty * tile_size
                    right = min(left + tile_size, geometry.width)
                    lower = min(upper + tile_size, geometry.height)
                    tile = scaled.crop((left, upper, right, lower))
                    tile_path = os.path.join(tiles_dir, f"{tx}_{ty}{ext}")
                    tile.save(tile_path, **save_kwargs)
                    tile_entries.append(
                        {
                            "level": geometry.level,
                            "x": tx,
                            "y": ty,
                            # Actual, un-padded size: edge tiles keep every
                            # remaining pixel and simply report smaller size.
                            "width": right - left,
                            "height": lower - upper,
                            "url": tile_url(
                                exhibit_id, revision, geometry.level, tx, ty
                            ),
                        }
                    )

            level_entries.append(
                {
                    "level": geometry.level,
                    "width": geometry.width,
                    "height": geometry.height,
                    "cols": geometry.cols,
                    "rows": geometry.rows,
                }
            )

        if BUILD_HOOK is not None:
            BUILD_HOOK(exhibit_id, revision, target)

        manifest = {
            "exhibit_id": exhibit_id,
            "revision": revision,
            "source_format": source_format,
            "width": width,
            "height": height,
            "tile_size": tile_size,
            "max_level": max_level,
            "levels": level_entries,
            "tiles": tile_entries,
        }
        with open(os.path.join(target, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False)
        return target, manifest
    except Exception:
        # A failed build leaves nothing behind for a future reader to find.
        _remove_path(target)
        raise


def _remove_path(path: str) -> None:
    if not os.path.exists(path):
        return
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(path)
    else:
        os.unlink(path)


def verify_revision_files(tile_root: str, exhibit_id: str, revision: int,
                          source_format: str, manifest: dict) -> bool:
    """Check that the manifest itself and every listed tile exist on disk."""
    base = revision_dir(tile_root, exhibit_id, revision)
    if not os.path.isfile(os.path.join(base, "manifest.json")):
        return False
    if not os.path.isfile(
        os.path.join(base, f"original{extension(source_format)}")
    ):
        return False
    for tile in manifest["tiles"]:
        rel = tile_relpath(tile["level"], tile["x"], tile["y"], source_format)
        if not os.path.isfile(os.path.join(base, rel)):
            return False
    return True


def tile_abspath(tile_root: str, exhibit_id: str, revision: int,
                 level: int, x: int, y: int, source_format: str) -> str:
    return os.path.join(
        revision_dir(tile_root, exhibit_id, revision),
        tile_relpath(level, x, y, source_format),
    )
