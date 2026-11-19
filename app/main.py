"""FastAPI application: event ingestion, visit paths, image tile publishing."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse

from . import config, crud, image_crud, tiling
from .db import get_connection, init_db, connect, write_transaction
from .image_models import ImageManifest, RevisionInfo, UploadResult
from .models import (
    ErrorResponse,
    EventBatchIn,
    EventIn,
    IngestBatchResult,
    IngestResult,
    VisitPath,
    WithdrawRequest,
    WithdrawResult,
)
from .tiling import TilingError, UnsupportedImage


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Ensure schema and storage exist, then reconcile any build interrupted
    # by a previous shutdown, before serving the first request.
    init_db()
    image_crud.start_heartbeat()
    conn = connect()
    try:
        image_crud.recover_interrupted_builds(conn)
    finally:
        conn.close()
    try:
        yield
    finally:
        image_crud.stop_heartbeat()


app = FastAPI(
    title="线上展馆 API",
    description=(
        "从浏览器 open/close 事件还原观众实际参观路径，并提供展品原图上传、"
        "256 像素瓦片切片发布与按修订号取图。支持事件重传去重、乱序补报、"
        "缺关闭标记停留未知、数据同意撤回，以及切片构建的原子发布与故障恢复。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(crud.BadEventTime)
def _bad_event_time_handler(_request, exc: crud.BadEventTime) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


@app.exception_handler(UnsupportedImage)
def _unsupported_image_handler(_request, exc: UnsupportedImage) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        content={"detail": str(exc)},
    )


@app.exception_handler(TilingError)
def _tiling_error_handler(_request, exc: TilingError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


# ---------------------------------------------------------------------------
# Event / visit-path API (unchanged behaviour)
# ---------------------------------------------------------------------------
@app.post(
    "/events",
    response_model=IngestResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="上报单个浏览事件（打开/关闭）",
)
def post_event(
    event: EventIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> IngestResult:
    """同一 ``event_id`` 重传返回 duplicate，不重复计数。

    整个请求在一个 ``BEGIN IMMEDIATE`` 事务内：校验、撤回检查与插入要么
    全部生效，要么全部回滚，不会出现半份写入。
    """
    with write_transaction(conn):
        return crud.ingest_event(conn, event)


@app.post(
    "/events/batch",
    response_model=IngestBatchResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="批量上报浏览事件（同一事务，原子提交）",
)
def post_events(
    batch: EventBatchIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> IngestBatchResult:
    """批量事件作为一个事务提交：任一事件非法则整批回滚。

    已撤回观众的事件记为 ``rejected_withdrawn``，其余事件仍可正常入库，
    二者在同一已提交状态上串行判定。
    """
    with write_transaction(conn):
        results = [crud.ingest_event(conn, event) for event in batch.events]
    accepted = sum(r.status == "stored" for r in results)
    duplicates = sum(r.status == "duplicate" for r in results)
    rejected = sum(r.status == "rejected_withdrawn" for r in results)
    return IngestBatchResult(
        accepted=accepted,
        duplicates=duplicates,
        rejected_withdrawn=rejected,
        results=results,
    )


@app.get(
    "/visitors/{visitor_id}/path",
    response_model=VisitPath,
    summary="查询观众参观路径（按打开时间排序）",
)
def get_path(
    visitor_id: str,
    conn: sqlite3.Connection = Depends(get_connection),
) -> VisitPath:
    """按 ``page_view_id`` 配对开关；缺关闭的展品 ``dwell_known=false``。

    路径在查询时从已提交事件实时推导，因此较早事件的补报会自动改变
    路径顺序与停留时长。撤回后返回空路径（``consent_withdrawn=true``）。
    """
    return crud.get_visitor_path(conn, visitor_id)


@app.post(
    "/visitors/{visitor_id}/withdraw",
    response_model=WithdrawResult,
    summary="撤回数据使用同意（删除已存事件，之后拒绝该观众的任何事件）",
)
def post_withdraw(
    visitor_id: str,
    payload: WithdrawRequest | None = None,
    conn: sqlite3.Connection = Depends(get_connection),
) -> WithdrawResult:
    """撤回标记与事件删除在同一事务内完成，重复撤回幂等。

    若请求带 body，其中的 visitor_id 必须与路径参数一致。
    """
    if payload is not None and payload.visitor_id != visitor_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径中的 visitor_id 与请求体不一致",
        )
    with write_transaction(conn):
        return crud.withdraw_consent(conn, visitor_id)


# ---------------------------------------------------------------------------
# Deep-zoom image upload / manifest / tile API
# ---------------------------------------------------------------------------
def _manifest_from_row(row: sqlite3.Row) -> ImageManifest:
    return ImageManifest(**json.loads(row["manifest"]))


@app.post(
    "/exhibits/{exhibit_id}/images",
    response_model=UploadResult,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse},
        415: {"model": ErrorResponse},
    },
    summary="上传展品原图并发布切片（PNG/JPEG，原子切换现役清单）",
)
def upload_exhibit_image(
    exhibit_id: str,
    file: UploadFile = File(..., description="PNG 或 JPEG 原图"),
) -> UploadResult:
    """为展品生成逐层缩小至单块的 256 px 瓦片并发布清单。

    - 旧修订在新修订全部瓦片校验通过、清单与现役指针同事务切换后仍可读取；
    - 构建失败只产生 ``failed`` 修订，现役清单保持不动；
    - 并发上传按到达顺序获得递增修订号，较早开始的构建不能覆盖后来上传。
    """
    image_bytes = file.file.read()
    if not image_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空"
        )

    conn = connect()
    try:
        manifest, previous, became_active = image_crud.publish_upload(
            conn, exhibit_id, image_bytes
        )
    finally:
        conn.close()

    if not became_active:
        # The build succeeded but a newer upload had already gone live.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"修订 {manifest['revision']} 已完整构建，但已有更新的修订成为现役，"
                "本次构建保留为非现役历史修订"
            ),
        )
    return UploadResult(manifest=ImageManifest(**manifest), previous_revision=previous)


@app.get(
    "/exhibits/{exhibit_id}/manifest",
    response_model=ImageManifest,
    summary="查询展品现役切片清单（原图尺寸、层级、瓦片坐标）",
)
def get_exhibit_manifest(
    exhibit_id: str,
    revision: int | None = None,
    conn: sqlite3.Connection = Depends(get_connection),
) -> ImageManifest:
    """默认返回现役清单；带 ``revision`` 时读取指定的已就绪历史修订。

    清单与其中每个瓦片 URL 携带同一来源修订号。
    """
    if revision is None:
        row = image_crud.get_active_manifest_row(conn, exhibit_id)
    else:
        row = image_crud.get_revision_row(conn, exhibit_id, revision)
        if row is not None and row["status"] != "ready":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"修订 {revision} 状态为 {row['status']}，尚无可用清单",
            )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该展品（或该修订）的已发布清单",
        )
    return _manifest_from_row(row)


@app.get(
    "/exhibits/{exhibit_id}/revisions",
    response_model=list[RevisionInfo],
    summary="查询展品修订历史（含构建状态与现役标记，运维用）",
)
def get_exhibit_revisions(
    exhibit_id: str,
    conn: sqlite3.Connection = Depends(get_connection),
) -> list[RevisionInfo]:
    rows = image_crud.list_revisions(conn, exhibit_id)
    return [
        RevisionInfo(
            exhibit_id=row["exhibit_id"],
            revision=row["revision"],
            status=row["status"],
            active=row["active"],
            source_format=row["source_format"],
            width=row["width"],
            height=row["height"],
            max_level=row["max_level"],
            error_message=row["error_message"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


@app.get(
    "/exhibits/{exhibit_id}/tiles/{revision}/{level}/{x}/{y}",
    summary="按修订号与瓦片坐标取图（边缘瓦片可能小于 256 px）",
)
def get_tile(
    exhibit_id: str,
    revision: int,
    level: int,
    x: int,
    y: int,
    conn: sqlite3.Connection = Depends(get_connection),
) -> FileResponse:
    """仅当修订已就绪、坐标出现在其清单中时返回瓦片文件。"""
    row = image_crud.get_revision_row(conn, exhibit_id, revision)
    if row is None or row["status"] != "ready":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该修订不存在或尚未就绪",
        )

    manifest = json.loads(row["manifest"])
    match = next(
        (
            t for t in manifest["tiles"]
            if t["level"] == level and t["x"] == x and t["y"] == y
        ),
        None,
    )
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该层级/坐标下没有瓦片",
        )

    path = tiling.tile_abspath(
        config.TILE_ROOT, exhibit_id, revision,
        level, x, y, row["source_format"],
    )
    if not os.path.isfile(path):
        # Should be impossible for a ready revision (startup verification
        # demotes such rows); reported honestly rather than crashing.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="瓦片文件缺失（修订可能需要重新构建）",
        )
    return FileResponse(path, media_type=tiling.media_type(row["source_format"]))


@app.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}
