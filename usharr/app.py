"""FastAPI app: endpoints + background scan loop."""

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from oxyde_admin import FastAPIAdmin
from pydantic import BaseModel, Field

from usharr import database, models, plex, probers, queries, views
from usharr import format as fmt
from usharr.config import get_config
from usharr.scanner import ScanRequest, scanner


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s %(levelname)s] %(message)s"),
    )
    for name, level in (
        ("usharr", logging.DEBUG),
        ("uvicorn", logging.INFO),
        ("uvicorn.access", logging.INFO),
        ("uvicorn.error", logging.INFO),
    ):
        log = logging.getLogger(name)
        log.setLevel(level)
        log.handlers = [handler]
        log.propagate = False


configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await database.connect()
    get_config()
    # USHARR_DB_RO: serve the DB read-only without spawning the reconcile
    # / probe loops. Used by `make dev` to test UI changes against a
    # copied prod DB without trashing it (scan deletes any rows
    # whose paths it can't find on disk).
    if os.environ.get("USHARR_DB_RO"):
        logger.info("usharr started in no-bg mode; db=%s", database.DB_PATH)
    else:
        scanner.start()
        logger.info("usharr started; library=%s", get_config().library)
    yield
    await scanner.stop()
    await database.close()
    logger.info("usharr shutdown complete")


app = FastAPI(title="usharr", lifespan=lifespan)
api = APIRouter(prefix="/api")

# Oxyde admin panel
admin = FastAPIAdmin(title="usharr admin")
admin.register_all()
assert admin.app is not None
app.mount("/admin", admin.app)

here = Path(__file__).parent
app.mount("/static", StaticFiles(directory=here / "static"), name="static")
templates = Jinja2Templates(directory=here / "templates")
templates.env.filters["pathencode"] = lambda s: quote(s or "", safe="/")
# jinja2 leaves Environment.globals' value type inferred from its defaults, so
# assign our helpers through a widened reference.
jinja_globals: dict[str, Any] = templates.env.globals
jinja_globals["resolution_bucket"] = fmt.resolution_bucket


def static_url(filename: str) -> str:
    """Cache-busting URL for a /static asset. Appends the file's mtime
    so an edit invalidates browser caches without a hard reload."""
    p = here / "static" / filename
    try:
        v = int(p.stat().st_mtime)
    except OSError:
        return f"/static/{filename}"
    return f"/static/{filename}?v={v}"


jinja_globals["static_url"] = static_url

# Grid + detail view helpers (the template calls these on the data models,
# the same pattern as resolution_bucket above).
jinja_globals["grid_title"] = views.grid_title
jinja_globals["grid_year"] = views.grid_year
jinja_globals["grid_edition"] = views.grid_edition
jinja_globals["video_summary"] = views.video_summary
jinja_globals["audio_summary"] = views.audio_summary
jinja_globals["sub_chip"] = views.sub_chip
jinja_globals["has_error"] = views.has_error
jinja_globals["aspects"] = views.aspects
jinja_globals["plex_url"] = views.plex_url
jinja_globals["tautulli_url"] = views.tautulli_url
jinja_globals["bazarr_url"] = views.bazarr_url
jinja_globals["radarr_url"] = views.radarr_url
jinja_globals["sonarr_url"] = views.sonarr_url
jinja_globals["color"] = views.color
jinja_globals["detail_error"] = views.detail_error
jinja_globals["audio_lang"] = views.audio_lang
jinja_globals["audio_title"] = views.audio_title
jinja_globals["audio_details"] = views.audio_details
jinja_globals["sub_lang"] = views.sub_lang
templates.env.filters["dash"] = views.dash


def slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "library"


@dataclass(frozen=True, slots=True)
class Library:
    slug: str
    label: str
    paths: list[str]


def libraries() -> list[Library]:
    return [
        Library(slug=slug(name), label=name, paths=list(paths))
        for name, paths in get_config().library.items()
    ]


def library_by_slug(slug: str) -> Library | None:
    for lib in libraries():
        if lib.slug == slug:
            return lib
    return None


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


async def build_info(mf: models.VideoFile) -> InfoResponse:
    path = mf.path
    mi = await queries.get_mediainfo(path)
    ar = await queries.get_ardetector(path)
    internal_subs, external_subs = await queries.get_subtitle_tracks(path)
    samples_raw = ar.aspect_samples_parsed if ar else None
    samples = [AspectSample(**s) for s in samples_raw] if samples_raw else None
    return InfoResponse(
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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=RedirectResponse, include_in_schema=False)
async def index() -> RedirectResponse:
    libs = libraries()
    if not libs:
        raise HTTPException(status_code=500, detail="no library configured")
    return RedirectResponse(url=f"/library/{libs[0].slug}", status_code=302)


@app.get("/library/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def library_page(request: Request, slug: str) -> HTMLResponse:
    lib = library_by_slug(slug)
    if lib is None:
        raise HTTPException(status_code=404, detail=f"no library {slug!r}")

    rows_data = await queries.library_rows(lib.paths, key=views.library_sort_key)
    grid = views.build_grid(rows_data)

    config = get_config()
    machine_id = await plex.get_machine_identifier()
    server_url = config.plex.url or await plex.get_server_url()

    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "label": lib.label,
            "rows": grid.rows,
            "jump_letters": grid.jump_letters,
            "titles": grid.titles,
            "episodes": grid.episodes,
            "is_tv": grid.is_tv,
            "libraries": libraries(),
            "current_slug": slug,
            "server_url": server_url,
            "machine_id": machine_id,
            "config": config,
            "jump_letters_alphabet": views.JUMP_LETTERS,
            "available_letters": grid.available_letters,
        },
    )


@dataclass(frozen=True, slots=True)
class DetailNav:
    prev_path: str | None
    next_path: str | None
    prev_season_path: str | None
    next_season_path: str | None
    prev_show_path: str | None
    next_show_path: str | None
    is_episode: bool


async def detail_nav(lib: Library | None, path: str) -> DetailNav:
    """Prev/Next navigation for the detail page: file-order siblings plus, for
    episodes, the prev/next season and show jump targets."""
    if lib is None:
        return DetailNav(None, None, None, None, None, None, False)
    # Use the same sort as the library page so Prev/Next feels
    # consistent. Hops over bonus features (extras).
    ordered = await queries.library_rows(lib.paths, key=views.library_sort_key)
    paths = [r.path for r in ordered]
    try:
        i = paths.index(path)
    except ValueError:
        i = -1
    is_episode = 0 <= i < len(paths) and ordered[i].plex_season_number is not None
    return DetailNav(
        prev_path=paths[i - 1] if i > 0 else None,
        next_path=paths[i + 1] if 0 <= i < len(paths) - 1 else None,
        prev_season_path=views.find_prev_season_path(ordered, i)
        if is_episode
        else None,
        next_season_path=views.find_next_season_path(ordered, i)
        if is_episode
        else None,
        prev_show_path=views.find_prev_show_path(ordered, i) if is_episode else None,
        next_show_path=views.find_next_show_path(ordered, i) if is_episode else None,
        is_episode=is_episode,
    )


async def gather_extras(path: str) -> list[dict]:
    """Bonus features: siblings of the main movie that live in an Extras/
    Interviews/Featurettes/... subfolder. Skip when viewing an extra
    directly — no nested grouping."""
    extras: list[dict] = []
    if not queries.is_extra(path):
        parent_prefix = str(Path(path).parent) + "/"
        extra_paths = sorted(
            p
            for p in await queries.list_paths()
            if p.startswith(parent_prefix) and queries.is_extra(p)
        )
        for ep in extra_paths:
            pm = await queries.load_path_media(ep)
            if pm is None:
                continue
            extras.append(
                {
                    "mf": pm.mf,
                    "mi": pm.mediainfo,
                    "ar": pm.ardetector,
                    "audio": pm.audio,
                    "subtitle": pm.subtitles,
                    "sub_exts": fmt.subtitle_file_exts(ep, pm.subtitles),
                    "aspect_set": pm.ardetector.aspect_samples_parsed
                    if pm.ardetector
                    else None,
                    "duration_str": fmt.format_duration(
                        pm.mediainfo.duration if pm.mediainfo else None
                    ),
                    "display_title": fmt.format_display_title(ep, None, None),
                    "year": fmt.year_from_path(ep),
                    "edition": fmt.edition_from_path(ep),
                },
            )
    return extras


@app.get("/item", response_class=HTMLResponse, include_in_schema=False)
async def item_detail(request: Request, path: str) -> HTMLResponse:
    pm = await queries.load_path_media(path)
    if pm is None:
        raise HTTPException(status_code=404, detail=f"no record for {path}")
    lib = next(
        (lib for lib in libraries() if any(path.startswith(p) for p in lib.paths)),
        None,
    )
    nav = await detail_nav(lib, path)

    mf = pm.mf
    mi = pm.mediainfo
    ar = pm.ardetector
    audio_rows = pm.audio
    subtitle_rows = pm.subtitles
    sub_exts = fmt.subtitle_file_exts(path, subtitle_rows)
    plex_item = await queries.get_plex_item_by_local_path(path)
    movie = await queries.movie_for_local_path(path)
    series = await queries.series_for_local_path(path)
    aspect_set = ar.aspect_samples_parsed if ar else None
    extras = await gather_extras(path)
    config = get_config()
    machine_id = await plex.get_machine_identifier()
    server_url = config.plex.url or await plex.get_server_url()

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "mf": mf,
            "mi": mi,
            "ar": ar,
            "audio": audio_rows,
            "subtitle": subtitle_rows,
            "sub_exts": sub_exts,
            "plex_item": plex_item,
            "aspect_set": aspect_set,
            "duration_str": fmt.format_duration(mi.duration if mi else None),
            "year": (plex_item.year if plex_item else None) or fmt.year_from_path(path),
            "edition": fmt.edition_from_path(path),
            "display_title": fmt.format_display_title(
                path,
                plex_item.title if plex_item else None,
                plex_item.show_title if plex_item else None,
            ),
            "plex_url": views.plex_url(plex_item, server_url, machine_id),
            "tautulli_url": views.tautulli_url(plex_item, config.tautulli.url),
            "bazarr_url": views.bazarr_url(movie, series, config),
            "radarr_url": views.radarr_url(movie, config.radarr.url),
            "sonarr_url": views.sonarr_url(series, config.sonarr.url),
            "extras": extras,
            "libraries": libraries(),
            "current_slug": lib.slug if lib else "",
            "library_label": lib.label if lib else "Library",
            **asdict(nav),
            "mi_badges": fmt.mediainfo_badges(mi, audio_rows),
        },
    )


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
            return InfoByContentIdResponse(
                **(await build_info(row)).model_dump(),
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
    return await build_info(row)


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


app.include_router(api)
