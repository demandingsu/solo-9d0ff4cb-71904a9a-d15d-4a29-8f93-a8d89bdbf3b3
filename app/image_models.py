"""Pydantic models for the deep-zoom image publish/query API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ImageFormat = Literal["png", "jpeg"]
RevisionStatus = Literal["building", "ready", "failed"]


class TileEntry(BaseModel):
    """One tile file inside a revision.

    ``width``/``height`` are the actual encoded pixel dimensions: edge tiles
    are smaller than ``tile_size`` rather than padded or cropped, which is how
    the requirement "edge tiles must not lose image content" is made visible
    to clients.
    """

    level: int = Field(..., description="层级号，0 = 缩至单块的最小层")
    x: int = Field(..., description="层内横向瓦片序号（从 0 开始）")
    y: int = Field(..., description="层内纵向瓦片序号（从 0 开始）")
    width: int = Field(..., description="该瓦片实际像素宽（边缘瓦片可能小于 tile_size）")
    height: int = Field(..., description="该瓦片实际像素高（边缘瓦片可能小于 tile_size）")
    # The URL already carries the same revision as the manifest.
    url: str = Field(..., description="取图地址，内含同一来源修订号")


class LevelEntry(BaseModel):
    """One pyramid level: its full (scaled) dimensions and tile grid shape."""

    level: int
    width: int = Field(..., description="该层级整图像素宽")
    height: int = Field(..., description="该层级整图像素高")
    cols: int = Field(..., description="横向瓦片数")
    rows: int = Field(..., description="纵向瓦片数")


class ImageManifest(BaseModel):
    """Published manifest: original size, levels and tile coordinates.

    Returned both as the body of an upload and from the manifest endpoint.
    Every embedded URL carries ``revision`` so tiles and manifest share the
    same source revision number.
    """

    exhibit_id: str
    revision: int = Field(..., description="来源修订号，瓦片 URL 与清单同号")
    source_format: ImageFormat
    width: int = Field(..., description="原图像素宽")
    height: int = Field(..., description="原图像素高")
    tile_size: int = Field(..., description="方瓦片边长（像素），边缘瓦片除外")
    max_level: int
    levels: list[LevelEntry]
    tiles: list[TileEntry]


class RevisionInfo(BaseModel):
    """One row of an exhibit's revision history (debug/ops endpoint)."""

    exhibit_id: str
    revision: int
    status: RevisionStatus
    active: bool = Field(..., description="是否为现役清单指向的修订")
    source_format: ImageFormat
    width: int
    height: int
    max_level: int
    error_message: str | None = None
    created_at: str


class UploadResult(BaseModel):
    manifest: ImageManifest
    # Previously active revision, if any — it remains fully readable afterwards.
    previous_revision: int | None = None
