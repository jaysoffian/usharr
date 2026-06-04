"""FastAPI app: endpoints + background scan loop."""

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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


def slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "library"


def libraries() -> list[dict]:
    return [
        {"slug": slug(name), "label": name, "paths": list(paths)}
        for name, paths in get_config().library.items()
    ]


def library_by_slug(slug: str) -> dict | None:
    for lib in libraries():
        if lib["slug"] == slug:
            return lib
    return None


async def library_rows_for(lib: dict) -> list[queries.LibraryRow]:
    rows: list[queries.LibraryRow] = []
    for p in lib["paths"]:
        rows.extend(await queries.LibraryRow.for_prefix(p))
    return rows


async def library_tracks_for(
    lib: dict,
) -> tuple[dict[str, list], dict[str, list]]:
    audio: dict[str, list] = {}
    subs: dict[str, list] = {}
    for p in lib["paths"]:
        a, s = await queries.LibraryRow.tracks_for_prefix(p)
        audio.update(a)
        subs.update(s)
    return audio, subs


# Plex's standard "extras" subdirectory names. A file under any of these
# (at any depth) is hidden from the library view — it's a bonus feature
# of the parent movie, not its own title. Probing + detail pages still
# work via /item?path=...
EXTRAS_DIRS = frozenset(
    {
        "behindthescenes",
        "behind the scenes",
        "deleted",
        "deleted scenes",
        "extra",
        "extras",
        "featurette",
        "featurettes",
        "interview",
        "interviews",
        "other",
        "sample",
        "samples",
        "scenes",
        "short",
        "shorts",
        "trailer",
        "trailers",
    },
)


def is_extra(path: str) -> bool:
    """True if any ancestor directory matches a Plex 'extras' subdir name."""
    return any(part.lower() in EXTRAS_DIRS for part in Path(path).parent.parts)


JUMP_LETTERS: tuple[str, ...] = ("#", *(chr(c) for c in range(ord("A"), ord("Z") + 1)))


def jump_letter(display_title: str) -> str:
    norm = fmt.sort_normalize(display_title)
    if not norm:
        return "#"
    c = norm[0]
    return c.upper() if c.isalpha() else "#"


def library_sort_key(r: queries.LibraryRow) -> tuple:
    """Library sort: show/title normalized + article-stripped, then S/E.

    Episodes group by show (via plex_show_title) and sort by season then
    episode within. Movies and standalone titles sort by title.
    """
    primary = (
        r.plex_show_title
        or r.plex_title
        or fmt.format_display_title(r.path, None, None)
    )
    return (
        fmt.natural_sort_key(primary),
        r.plex_season_number or 0,
        r.plex_episode_number or 0,
        r.path,
    )


async def bazarr_url_for(local_path: str) -> str | None:
    """Bazarr deep-link for the detail route. Derived from the Radarr movie id
    / Sonarr series id we already hold, gated by config flags — no Bazarr API."""
    cfg = get_config().bazarr
    if not cfg.url:
        return None
    if cfg.link_movies:
        movie_id = await queries.radarr_id_for_local_path(local_path)
        if movie_id is not None:
            return fmt.bazarr_movie_deeplink(cfg.url, movie_id)
    if cfg.link_series:
        series = await queries.series_for_local_path(local_path)
        if series is not None:
            return fmt.bazarr_series_deeplink(cfg.url, series.id)
    return None


async def radarr_url_for(radarr_base: str | None, local_path: str) -> str | None:
    if not radarr_base:
        return None
    return fmt.radarr_deeplink(
        radarr_base, await queries.radarr_tmdb_for_local_path(local_path)
    )


async def sonarr_url_for(sonarr_base: str | None, local_path: str) -> str | None:
    if not sonarr_base:
        return None
    series = await queries.series_for_local_path(local_path)
    return fmt.sonarr_deeplink(sonarr_base, series.title_slug if series else None)


def find_prev_season_path(ordered: list[queries.LibraryRow], i: int) -> str | None:
    """First episode of the previous season within the same show."""
    cur_show = ordered[i].plex_show_title
    cur_season = ordered[i].plex_season_number
    if cur_show is None or cur_season is None:
        return None
    j = i - 1
    while j >= 0 and ordered[j].plex_show_title == cur_show:
        if ordered[j].plex_season_number != cur_season:
            # j is the last episode of some earlier season; walk back
            # to that season's first episode (same sort order as library).
            prev_season = ordered[j].plex_season_number
            while j > 0 and (
                ordered[j - 1].plex_show_title == cur_show
                and ordered[j - 1].plex_season_number == prev_season
            ):
                j -= 1
            return ordered[j].path
        j -= 1
    return None


def find_next_season_path(ordered: list[queries.LibraryRow], i: int) -> str | None:
    """First episode of the next season within the same show."""
    cur_show = ordered[i].plex_show_title
    cur_season = ordered[i].plex_season_number
    if cur_show is None or cur_season is None:
        return None
    j = i + 1
    while j < len(ordered) and ordered[j].plex_show_title == cur_show:
        if ordered[j].plex_season_number != cur_season:
            return ordered[j].path
        j += 1
    return None


def find_prev_show_path(ordered: list[queries.LibraryRow], i: int) -> str | None:
    """First episode of the previous show in the library."""
    cur_show = ordered[i].plex_show_title
    if cur_show is None:
        return None
    j = i - 1
    while j >= 0 and ordered[j].plex_show_title == cur_show:
        j -= 1
    if j < 0:
        return None
    prev_show = ordered[j].plex_show_title
    while j > 0 and ordered[j - 1].plex_show_title == prev_show:
        j -= 1
    return ordered[j].path


def find_next_show_path(ordered: list[queries.LibraryRow], i: int) -> str | None:
    """First episode of the next show in the library."""
    cur_show = ordered[i].plex_show_title
    if cur_show is None:
        return None
    j = i + 1
    while j < len(ordered) and ordered[j].plex_show_title == cur_show:
        j += 1
    if j >= len(ordered):
        return None
    return ordered[j].path


def annotate_tracks(
    media_path: str,
    audio: list[models.AudioTrack],
    subtitle: list[fmt.SubtitleTrack],
) -> tuple[list[dict], list[dict]]:
    """Build template-facing audio/subtitle dicts with display fields attached."""
    audio_view = [
        {
            **t.model_dump(),
            "details": fmt.format_audio_details(t),
            "lang_display": fmt.lang_name(t.language),
            "title_display": fmt.clean_audio_title(t.title, t.language),
        }
        for t in audio
    ]
    file_exts = fmt.subtitle_file_exts(media_path, subtitle)
    subtitle_view = [
        {
            **t.model_dump(),
            "subtitle_path": getattr(t, "subtitle_path", None),
            "lang_display": fmt.lang_name(t.language),
            "file_ext": ext,
        }
        for t, ext in zip(subtitle, file_exts, strict=True)
    ]
    return audio_view, subtitle_view


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
    samples_raw = json.loads(ar.aspect_samples) if ar and ar.aspect_samples else None
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
    return RedirectResponse(url=f"/library/{libs[0]['slug']}", status_code=302)


@app.get("/library/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def library_page(request: Request, slug: str) -> HTMLResponse:
    lib = library_by_slug(slug)
    if lib is None:
        raise HTTPException(status_code=404, detail=f"no library {slug!r}")

    all_rows = await library_rows_for(lib)
    raw_rows = sorted(
        (r for r in all_rows if not is_extra(r.path)),
        key=library_sort_key,
    )
    audio_by_path, sub_by_path = await library_tracks_for(lib)

    config = get_config()
    machine_id = await plex.get_machine_identifier()
    server_url = config.plex.url or await plex.get_server_url()

    # Radarr movie overlay (tmdb + radarr id) keyed by local path; one query
    # per library root. Series + Sonarr slug come pre-joined on each row.
    movies: dict[str, models.Movie] = {}
    for p in lib["paths"]:
        movies.update(await queries.LibraryRow.movies_for_prefix(p))

    file_rows = [
        views.render_row(
            r,
            audio=audio_by_path.get(r.path, []),
            subs=sub_by_path.get(r.path, []),
            movie=movies.get(r.path),
            config=config,
            server_url=server_url,
            machine_id=machine_id,
        )
        for r in raw_rows
    ]
    is_tv = any(row.kind == "episode" for row in file_rows)
    # TV libraries: roll episodes up under show + season header rows.
    # Avoids a 1700-row wall for shows with many seasons. Grouping is
    # purely visual — no click-to-expand, no nesting — so Cmd-F still
    # works and the rail still jumps by show-title letter.
    rows = views.group_tv_rows(file_rows) if is_tv else file_rows
    if is_tv:
        titles = sum(1 for r in rows if r.kind == "show")
        episodes = sum(1 for r in rows if r.kind == "episode")
    else:
        titles = sum(1 for r in rows if r.kind == "movie")
        episodes = None

    # Letter-jump rail: anchor on show headers (TV) / movie rows (films);
    # skip season + episode rows so the alphabet tracks shows, not
    # whatever happens to be the first-letter of an episode title.
    jump_anchor_kinds = {"show", "movie"}
    last_letter: str | None = None
    available: set[str] = set()
    for r in rows:
        if r.kind in jump_anchor_kinds:
            letter = jump_letter(r.display_title)
            available.add(letter)
            r.jump_letter = letter if letter != last_letter else None
            last_letter = letter
        else:
            r.jump_letter = None

    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "label": lib["label"],
            "rows": rows,
            "titles": titles,
            "episodes": episodes,
            "is_tv": is_tv,
            "libraries": libraries(),
            "current_slug": slug,
            "jump_letters": JUMP_LETTERS,
            "available_letters": available,
        },
    )


@app.get("/item", response_class=HTMLResponse, include_in_schema=False)
async def item_detail(request: Request, path: str) -> HTMLResponse:
    mf = await queries.get(path)
    if mf is None:
        raise HTTPException(status_code=404, detail=f"no record for {path}")
    lib = next(
        (lib for lib in libraries() if any(path.startswith(p) for p in lib["paths"])),
        None,
    )
    prev_path: str | None = None
    next_path: str | None = None
    prev_season_path: str | None = None
    next_season_path: str | None = None
    prev_show_path: str | None = None
    next_show_path: str | None = None
    is_episode = False
    if lib is not None:
        # Use the same sort as the library page so Prev/Next feels
        # consistent. Hops over bonus features (extras).
        ordered = sorted(
            (r for r in await library_rows_for(lib) if not is_extra(r.path)),
            key=library_sort_key,
        )
        paths = [r.path for r in ordered]
        try:
            i = paths.index(path)
        except ValueError:
            i = -1
        if i > 0:
            prev_path = paths[i - 1]
        if 0 <= i < len(paths) - 1:
            next_path = paths[i + 1]
        if 0 <= i < len(paths) and ordered[i].plex_season_number is not None:
            is_episode = True
            prev_season_path = find_prev_season_path(ordered, i)
            next_season_path = find_next_season_path(ordered, i)
            prev_show_path = find_prev_show_path(ordered, i)
            next_show_path = find_next_show_path(ordered, i)

    audio_rows = await queries.get_audio_tracks(path)
    internal_subs, external_subs = await queries.get_subtitle_tracks(path)
    subtitle_rows = [*internal_subs, *external_subs]
    audio_view, subtitle_view = annotate_tracks(path, audio_rows, subtitle_rows)
    plex_item = await queries.get_plex_item_by_local_path(path)
    mi = await queries.get_mediainfo(path)
    ar = await queries.get_ardetector(path)
    aspect_set = json.loads(ar.aspect_samples) if ar and ar.aspect_samples else None
    config = get_config()
    machine_id = await plex.get_machine_identifier()
    server_url = config.plex.url or await plex.get_server_url()
    rating_key = plex_item.rating_key if plex_item else None

    # Bonus features: siblings of the main movie that live in an Extras/
    # Interviews/Featurettes/... subfolder. Skip when viewing an extra
    # directly — no nested grouping.
    extras: list[dict] = []
    if not is_extra(path):
        parent_prefix = str(Path(path).parent) + "/"
        extra_paths = sorted(
            p
            for p in await queries.list_paths()
            if p.startswith(parent_prefix) and is_extra(p)
        )
        for ep in extra_paths:
            emf = await queries.get(ep)
            if emf is None:
                continue
            emi = await queries.get_mediainfo(ep)
            ear = await queries.get_ardetector(ep)
            ea = await queries.get_audio_tracks(ep)
            ei, ex = await queries.get_subtitle_tracks(ep)
            ea_view, es_view = annotate_tracks(ep, ea, [*ei, *ex])
            eset = (
                json.loads(ear.aspect_samples) if ear and ear.aspect_samples else None
            )
            extras.append(
                {
                    "mf": detail_view(emf, emi, ear),
                    "audio": ea_view,
                    "subtitle": es_view,
                    "aspect_set": eset,
                    "duration_str": fmt.format_duration(emi.duration if emi else None),
                    "display_title": fmt.format_display_title(ep, None, None),
                    "year": fmt.year_from_path(ep),
                    "edition": fmt.edition_from_path(ep),
                },
            )
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "mf": detail_view(mf, mi, ar),
            "audio": audio_view,
            "subtitle": subtitle_view,
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
            "plex_url": fmt.plex_deeplink(server_url, machine_id, rating_key),
            "tautulli_url": fmt.tautulli_deeplink(config.tautulli.url, rating_key),
            "bazarr_url": await bazarr_url_for(path),
            "radarr_url": await radarr_url_for(config.radarr.url, path),
            "sonarr_url": await sonarr_url_for(config.sonarr.url, path),
            "extras": extras,
            "libraries": libraries(),
            "current_slug": lib["slug"] if lib else "",
            "library_label": lib["label"] if lib else "Library",
            "prev_path": prev_path,
            "next_path": next_path,
            "prev_season_path": prev_season_path,
            "next_season_path": next_season_path,
            "prev_show_path": prev_show_path,
            "next_show_path": next_show_path,
            "is_episode": is_episode,
            "mi_badges": fmt.mediainfo_badges(mi, audio_rows),
        },
    )


def detail_view(
    mf: models.VideoFile,
    mi: models.Mediainfo | None,
    ar: models.Ardetector | None,
) -> dict:
    """Flatten media_file + mediainfo + ardetector into one dict the
    detail template treats as the legacy `mf` object. NULL probe rows
    surface as None values, which the template already renders as '—'.
    """
    errors: list[str] = []
    if mi and mi.error:
        errors.append(f"mediainfo: {mi.error}")
    if ar and ar.error:
        errors.append(f"ardetector: {ar.error}")
    return {
        "path": mf.path,
        "container": mi.container if mi else None,
        "duration": mi.duration if mi else None,
        "video_codec": mi.video_codec if mi else None,
        "video_profile": mi.video_profile if mi else None,
        "video_width": mi.video_width if mi else None,
        "video_height": mi.video_height if mi else None,
        "video_bit_depth": mi.video_bit_depth if mi else None,
        "video_hdr": mi.video_hdr if mi else None,
        "video_hdr_format": mi.video_hdr_format if mi else None,
        "video_frame_rate": mi.video_frame_rate if mi else None,
        "video_bit_rate": mi.video_bit_rate if mi else None,
        "video_max_bit_rate": mi.video_max_bit_rate if mi else None,
        "aspect_primary": ar.aspect_primary if ar else None,
        "color": fmt.format_color(ar.color_pct if ar else None),
        "error": "; ".join(errors) if errors else None,
    }


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
