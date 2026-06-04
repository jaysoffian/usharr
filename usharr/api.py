"""JSON API: file media-info, prober status stream, Plex webhook, task triggers."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any, overload

from fastapi import APIRouter, Form, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from usharr import models, plex, probers, queries
from usharr.config import get_config
from usharr.scanner import ScanRequest, scanner

api = APIRouter(prefix="/api")


# --- /api/info response models --------------------------------------------


class VideoInfo(BaseModel):
    # validation_alias lets this validate straight off a Mediainfo row
    # (`video_*` columns) while serializing under the public, unprefixed names.
    codec: str | None = Field(default=None, validation_alias="video_codec")
    profile: str | None = Field(default=None, validation_alias="video_profile")
    width: int | None = Field(default=None, validation_alias="video_width")
    height: int | None = Field(default=None, validation_alias="video_height")
    bit_depth: int | None = Field(default=None, validation_alias="video_bit_depth")
    hdr: str | None = Field(default=None, validation_alias="video_hdr")
    hdr_format: str | None = Field(default=None, validation_alias="video_hdr_format")
    frame_rate: float | None = Field(default=None, validation_alias="video_frame_rate")
    bit_rate: int | None = Field(default=None, validation_alias="video_bit_rate")
    max_bit_rate: int | None = Field(
        default=None, validation_alias="video_max_bit_rate"
    )


class AspectSample(BaseModel):
    aspect: float
    percentage: float


class AspectInfo(BaseModel):
    primary: float | None = None
    widest: float | None = None
    samples: list[AspectSample] | None = None


class InfoResponse(BaseModel):
    path: str
    mediainfo_error: str | None = None
    ardetector_error: str | None = None
    container: str | None = None
    duration: float | None = None
    video: VideoInfo
    aspect: AspectInfo
    audio: list[models.AudioTrack]
    subtitles: list[models.SubtitleTrackInternal | models.SubtitleTrackExternal]


class InfoByContentIdResponse(InfoResponse):
    plex_content_id: str
    plex_files: list[str]


@overload
async def build_info(
    mf: models.VideoFile,
    response_cls: type[InfoByContentIdResponse],
    *,
    plex_content_id: str,
    plex_files: list[str],
) -> InfoByContentIdResponse: ...
@overload
async def build_info(
    mf: models.VideoFile, response_cls: type[InfoResponse]
) -> InfoResponse: ...
async def build_info(
    mf: models.VideoFile, response_cls: type[InfoResponse], **extra: Any
) -> InfoResponse:
    path = mf.path
    mi = await queries.get_mediainfo(path)
    ar = await queries.get_ardetector(path)
    internal_subs, external_subs = await queries.get_subtitle_tracks(path)
    samples_raw = ar.aspect_samples_parsed if ar else None
    samples = [AspectSample(**s) for s in samples_raw] if samples_raw else None
    return response_cls(
        path=path,
        mediainfo_error=mi.error if mi else None,
        ardetector_error=ar.error if ar else None,
        container=mi.container if mi else None,
        duration=mi.duration if mi else None,
        video=VideoInfo.model_validate(mi, from_attributes=True) if mi else VideoInfo(),
        aspect=AspectInfo(
            primary=ar.aspect_primary if ar else None,
            widest=ar.aspect_widest if ar else None,
            samples=samples,
        ),
        audio=await queries.get_audio_tracks(path),
        subtitles=[*internal_subs, *external_subs],
        **extra,
    )


def status_snapshot() -> dict:
    """Live state of both probers, shaped for the topbar status UI."""
    mi = scanner.mediainfo
    ar = scanner.ardetector
    return {
        "mediainfo": {
            "probing": str(mi.probing) if mi.probing else None,
            "pending": len(mi),
        },
        "ardetect": {
            "probing": str(ar.probing) if ar.probing else None,
            "pending": len(ar),
        },
    }


@api.get("/status")
async def status_stream() -> StreamingResponse:
    """SSE stream of prober activity. One event per state change, plus a
    15s keepalive so idle proxies don't drop the connection."""

    async def gen() -> AsyncIterator[str]:
        ev = probers.events.subscribe()
        try:
            yield f"data: {json.dumps(status_snapshot())}\n\n"
            while True:
                try:
                    await asyncio.wait_for(ev.wait(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                ev.clear()
                yield f"data: {json.dumps(status_snapshot())}\n\n"
        finally:
            probers.events.unsubscribe(ev)

    return StreamingResponse(gen(), media_type="text/event-stream")


@api.get("/info/by-content-id/{content_id}")
async def get_info_by_content_id(content_id: str) -> InfoByContentIdResponse:
    try:
        plex_files = await plex.resolve_rating_key(content_id)
    except plex.PlexNotLinkedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except plex.PlexError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not plex_files:
        raise HTTPException(
            status_code=404,
            detail=f"no Media/Part for content_id={content_id}",
        )
    path_map = get_config().plex.path_map
    for f in plex_files:
        row = await queries.get_by_remote_path(f, path_map)
        if row is not None:
            return await build_info(
                row,
                InfoByContentIdResponse,
                plex_content_id=content_id,
                plex_files=plex_files,
            )
    raise HTTPException(
        status_code=404,
        detail=f"no DB path matches any of {plex_files}",
    )


@api.get("/info/{file_path:path}")
async def get_info(file_path: str) -> InfoResponse:
    path = "/" + file_path
    row = await queries.get(path)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no record for {path}")
    return await build_info(row, InfoResponse)


@api.post("/webhook")
async def webhook(form: Annotated[plex.PlexWebhookForm, Form()]) -> Response:
    """Handle Plex webhook"""
    payload = form.payload

    if payload.event == "library.new":
        await scanner.enqueue(ScanRequest())

    return Response(status_code=204)


def lookup_path(file_path: str) -> Path:
    path = Path("/" + file_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"not a file: {path}")
    return path


@api.post("/task/scan")
async def task_scan() -> Response:
    """Incremental library sweep: pick up new files, reprobe changed ones."""
    await scanner.enqueue(ScanRequest())
    return Response(status_code=202)


@api.post("/task/refresh")
async def task_refresh() -> Response:
    """Force-refresh mediainfo on every file. AR cache preserved."""
    await scanner.enqueue(ScanRequest(refresh=True))
    return Response(status_code=202)


@api.post("/task/refresh/{file_path:path}")
async def task_refresh_path(file_path: str) -> Response:
    await scanner.enqueue(ScanRequest(lookup_path(file_path), refresh=True))
    return Response(status_code=202)


@api.post("/task/analyze")
async def task_analyze() -> Response:
    """Force-reprobe AR and mediainfo on every file. Slow."""
    await scanner.enqueue(ScanRequest(analyze=True))
    return Response(status_code=202)


@api.post("/task/analyze/{file_path:path}")
async def task_analyze_path(file_path: str) -> Response:
    await scanner.enqueue(ScanRequest(lookup_path(file_path), analyze=True))
    return Response(status_code=202)
