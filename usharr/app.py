"""FastAPI app: HTML pages + background scan loop."""

import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from oxyde_admin import FastAPIAdmin

from usharr import database, plex, queries, views
from usharr import format as fmt
from usharr.api import api
from usharr.config import get_config
from usharr.scanner import scanner


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
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "library"


@dataclass(frozen=True, slots=True)
class Library:
    slug: str
    label: str
    paths: list[str]


@cache
def libraries() -> list[Library]:
    return [
        Library(slug=slug(name), label=name, paths=list(paths))
        for name, paths in get_config().library.items()
    ]


@cache
def libraries_by_slug() -> dict[str, Library]:
    return {lib.slug: lib for lib in libraries()}


def library_by_slug(slug: str) -> Library | None:
    return libraries_by_slug().get(slug)


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


app.include_router(api)
