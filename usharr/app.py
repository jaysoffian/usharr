"""FastAPI app: endpoints + background scan loop."""

import asyncio
import contextlib
import dataclasses
import datetime as dt
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from usharr import (
    bazarr_sync,
    db,
    plex,
    plex_sync,
    radarr_sync,
    scanner,
    sonarr_sync,
)
from usharr import format as fmt
from usharr.config import Config, load_config


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
    config = load_config()
    db.init_db()
    app.state.config = config
    # USHARR_DB_RO: serve the DB read-only without spawning the scan /
    # probe / sync loops. Used by `make dev` to test UI changes against
    # a copied prod DB without trashing it (scan_loop deletes any rows
    # whose paths it can't find on disk).
    if os.environ.get("USHARR_DB_RO"):
        tasks: tuple[asyncio.Task, ...] = ()
        logger.info("usharr started in no-bg mode; db=%s", db.DB_PATH)
    else:
        tasks = (
            asyncio.create_task(scanner.scan_loop(config)),
            asyncio.create_task(scanner.probe_worker(config)),
            asyncio.create_task(plex_sync.plex_sync_loop(config)),
            asyncio.create_task(bazarr_sync.bazarr_sync_loop(config)),
            asyncio.create_task(radarr_sync.radarr_sync_loop(config)),
            asyncio.create_task(sonarr_sync.sonarr_sync_loop(config)),
        )
        logger.info("usharr started; library=%s", config.library)
    yield
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t
    db.close_db()
    logger.info("usharr shutdown complete")


app = FastAPI(title="usharr", lifespan=lifespan)
api = APIRouter(prefix="/api")

here = Path(__file__).parent
app.mount("/static", StaticFiles(directory=here / "static"), name="static")
templates = Jinja2Templates(directory=here / "templates")
templates.env.filters["pathencode"] = lambda s: quote(s or "", safe="/")
templates.env.globals["resolution_bucket"] = fmt.resolution_bucket

bg_tasks: set[asyncio.Task] = set()


def spawn(coro: object) -> asyncio.Task:
    t = asyncio.create_task(coro)  # type: ignore[arg-type]
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)
    return t


def slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "library"


def libraries(config: Config) -> list[dict]:
    return [
        {"slug": slug(name), "label": name, "paths": list(paths)}
        for name, paths in config.library.items()
    ]


def library_by_slug(config: Config, slug: str) -> dict | None:
    for lib in libraries(config):
        if lib["slug"] == slug:
            return lib
    return None


def library_rows_for(lib: dict) -> list[db.LibraryRow]:
    return [r for p in lib["paths"] for r in db.library_rows(p)]


def library_tracks_for(
    lib: dict,
) -> tuple[dict[str, list], dict[str, list]]:
    audio: dict[str, list] = {}
    subs: dict[str, list] = {}
    for p in lib["paths"]:
        a, s = db.library_tracks(p)
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


def library_sort_key(r: db.LibraryRow) -> tuple:
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
        fmt.sort_normalize(primary),
        r.plex_season_number or 0,
        r.plex_episode_number or 0,
        r.path,
    )


def bazarr_url_for(bazarr_base: str | None, local_path: str) -> str | None:
    """Single-path lookup used by the detail route."""
    if not bazarr_base:
        return None
    movie_id = db.bazarr_movie_for_local_path(local_path)
    if movie_id is not None:
        return fmt.bazarr_movie_deeplink(bazarr_base, movie_id)
    series_id = db.bazarr_series_for_local_path(local_path)
    if series_id is not None:
        return fmt.bazarr_series_deeplink(bazarr_base, series_id)
    return None


def radarr_url_for(radarr_base: str | None, local_path: str) -> str | None:
    if not radarr_base:
        return None
    return fmt.radarr_deeplink(radarr_base, db.radarr_tmdb_for_local_path(local_path))


def sonarr_url_for(sonarr_base: str | None, local_path: str) -> str | None:
    if not sonarr_base:
        return None
    return fmt.sonarr_deeplink(sonarr_base, db.sonarr_slug_for_local_path(local_path))


def ancestor_match[T](path: str, folder_map: dict[str, T]) -> T | None:
    """Walk up from `path`'s parent, return the value of the first matching
    folder in `folder_map`. Fast O(path-depth) ancestor-lookup — replaces
    the SQL prefix-match for series resolution on the library page.
    """
    p = Path(path).parent
    while True:
        s = str(p)
        if not s or s == p.anchor:
            return None
        v = folder_map.get(s)
        if v is not None:
            return v
        new_p = p.parent
        if new_p == p:
            return None
        p = new_p


def group_tv_rows(episode_rows: list[dict]) -> list[dict]:
    """Insert show + season header rows into a sorted episode list.

    Input rows are already ordered by show title → season → episode
    (see ``library_sort_key``). A show header is emitted when
    ``plex_show_title`` changes; a season header is emitted when
    ``plex_season_number`` changes within the show. Episodes are
    retitled ``"<N>. <Episode Title>"`` so the season header carries
    the season context.

    Non-episode rows (mixed-in movies) pass through untouched.
    """
    out: list[dict] = []
    current_show: str | None = None
    current_season: int | None = None
    show_header_idx: dict[str, int] = {}
    show_seasons: dict[str, set] = {}
    show_ep_count: dict[str, int] = {}
    for r in episode_rows:
        if r.get("kind") != "episode":
            out.append(r)
            continue
        show = r.get("plex_show_title")
        season = r.get("plex_season_number")
        if show and show != current_show:
            current_show = show
            current_season = None
            out.append(
                {
                    "kind": "show",
                    "display_title": show,
                    "show_title": show,
                    "plex_year": r.get("plex_year"),
                },
            )
            show_header_idx[show] = len(out) - 1
            show_seasons[show] = set()
            show_ep_count[show] = 0
        if show and season != current_season:
            current_season = season
            label = "Specials" if season in (None, 0) else f"Season {season}"
            out.append(
                {
                    "kind": "season",
                    "display_title": label,
                    "show_title": show,
                    "season_number": season if season is not None else 0,
                },
            )
            show_seasons[show].add(season)
        # Rewrite episode title to drop the show prefix (the header
        # above already says the show name) and prefix the episode
        # number so "1. Pilot" reads naturally under "Season 1".
        ep_num = r.get("plex_episode_number")
        ep_title = r.get("plex_title") or fmt.format_display_title(
            r["path"], None, None
        )
        r["display_title"] = f"{ep_num}. {ep_title}" if ep_num is not None else ep_title
        out.append(r)
        if show:
            show_ep_count[show] += 1
    # Second pass: fill season/episode counts into the show headers.
    for show, idx in show_header_idx.items():
        out[idx]["season_count"] = len(show_seasons[show])
        out[idx]["episode_count"] = show_ep_count[show]
    return out


def find_prev_season_path(ordered: list[db.LibraryRow], i: int) -> str | None:
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


def find_next_season_path(ordered: list[db.LibraryRow], i: int) -> str | None:
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


def find_prev_show_path(ordered: list[db.LibraryRow], i: int) -> str | None:
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


def find_next_show_path(ordered: list[db.LibraryRow], i: int) -> str | None:
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
    audio: list[db.AudioTrackRow],
    subtitle: list[db.SubtitleTrackRow],
) -> tuple[list[dict], list[dict]]:
    """Build template-facing audio/subtitle dicts with display fields attached."""
    audio_view = [
        {
            **dataclasses.asdict(t),
            "details": fmt.format_audio_details(t),
            "lang_display": fmt.lang_name(t.language),
            "title_display": fmt.clean_audio_title(t.title, t.language),
        }
        for t in audio
    ]
    file_exts = fmt.subtitle_file_exts(media_path, subtitle)
    subtitle_view = [
        {
            **dataclasses.asdict(t),
            "lang_display": fmt.lang_name(t.language),
            "file_ext": ext,
        }
        for t, ext in zip(subtitle, file_exts, strict=True)
    ]
    return audio_view, subtitle_view


# --- /api/info response models --------------------------------------------


class VideoInfo(BaseModel):
    codec: str | None = None
    profile: str | None = None
    width: int | None = None
    height: int | None = None
    bit_depth: int | None = None
    hdr: str | None = None
    hdr_format: str | None = None
    frame_rate: float | None = None
    bit_rate: int | None = None
    max_bit_rate: int | None = None


class AspectSample(BaseModel):
    aspect: float
    percentage: float


class AspectInfo(BaseModel):
    primary: float | None = None
    widest: float | None = None
    samples: list[AspectSample] | None = None


class InfoResponse(BaseModel):
    path: str
    discovered_at: dt.datetime
    mediainfo_probed_at: dt.datetime | None = None
    ardetector_probed_at: dt.datetime | None = None
    mediainfo_error: str | None = None
    ardetector_error: str | None = None
    container: str | None = None
    duration: float | None = None
    video: VideoInfo
    aspect: AspectInfo
    audio: list[db.AudioTrackRow]
    subtitles: list[db.SubtitleTrackRow]


class InfoByContentIdResponse(InfoResponse):
    plex_content_id: str
    plex_files: list[str]


def build_info(mf: db.MediaFileRow) -> InfoResponse:
    path = mf.path
    mi = db.get_mediainfo(path)
    ar = db.get_ardetector(path)
    samples_raw = json.loads(ar.aspect_samples) if ar and ar.aspect_samples else None
    samples = [AspectSample(**s) for s in samples_raw] if samples_raw else None
    return InfoResponse(
        path=path,
        discovered_at=dt.datetime.fromtimestamp(mf.discovered_at, dt.UTC),
        mediainfo_probed_at=(
            dt.datetime.fromtimestamp(mi.probed_at, dt.UTC) if mi else None
        ),
        ardetector_probed_at=(
            dt.datetime.fromtimestamp(ar.probed_at, dt.UTC) if ar else None
        ),
        mediainfo_error=mi.error if mi else None,
        ardetector_error=ar.error if ar else None,
        container=mi.container if mi else None,
        duration=mi.duration if mi else None,
        video=VideoInfo(
            codec=mi.video_codec if mi else None,
            profile=mi.video_profile if mi else None,
            width=mi.video_width if mi else None,
            height=mi.video_height if mi else None,
            bit_depth=mi.video_bit_depth if mi else None,
            hdr=mi.video_hdr if mi else None,
            hdr_format=mi.video_hdr_format if mi else None,
            frame_rate=mi.video_frame_rate if mi else None,
            bit_rate=mi.video_bit_rate if mi else None,
            max_bit_rate=mi.video_max_bit_rate if mi else None,
        ),
        aspect=AspectInfo(
            primary=ar.aspect_primary if ar else None,
            widest=ar.aspect_widest if ar else None,
            samples=samples,
        ),
        audio=db.get_audio_tracks(path),
        subtitles=db.get_subtitle_tracks(path),
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=RedirectResponse, include_in_schema=False)
async def index() -> RedirectResponse:
    libs = libraries(app.state.config)
    if not libs:
        raise HTTPException(status_code=500, detail="no library configured")
    return RedirectResponse(url=f"/library/{libs[0]['slug']}", status_code=302)


@app.get("/library/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def library_page(request: Request, slug: str) -> HTMLResponse:
    config: Config = app.state.config
    lib = library_by_slug(config, slug)
    if lib is None:
        raise HTTPException(status_code=404, detail=f"no library {slug!r}")

    raw_rows = sorted(
        (r for r in library_rows_for(lib) if not is_extra(r.path)),
        key=library_sort_key,
    )
    audio_by_path, sub_by_path = library_tracks_for(lib)

    machine_id = await plex.get_machine_identifier()
    server_url = config.plex.url or plex.get_server_url()
    tautulli_url = config.tautulli.url
    bazarr_url = config.bazarr.url
    radarr_url = config.radarr.url
    sonarr_url = config.sonarr.url

    # Batch the *arr lookups into one query each instead of 4 per row.
    bazarr_movies = db.all_bazarr_movies_by_local_path() if bazarr_url else {}
    bazarr_series = db.all_bazarr_series_by_local_folder() if bazarr_url else {}
    radarr_movies = db.all_radarr_movies_by_local_path() if radarr_url else {}
    sonarr_series = db.all_sonarr_series_by_local_folder() if sonarr_url else {}

    rows: list[dict] = []
    is_tv = False
    for r in raw_rows:
        path = r.path
        audio = audio_by_path.get(path, [])
        subs = sub_by_path.get(path, [])
        aspect_set = json.loads(r.aspect_samples) if r.aspect_samples else None
        aspects, aspects_truncated = fmt.format_aspects_for_row(
            aspect_set,
            r.aspect_primary,
        )
        is_episode = r.plex_season_number is not None
        if is_episode:
            is_tv = True
        rows.append(
            {
                "kind": "episode" if is_episode else "movie",
                "path": path,
                "plex_title": r.plex_title,
                "plex_show_title": r.plex_show_title,
                "plex_season_number": r.plex_season_number,
                "plex_episode_number": r.plex_episode_number,
                "display_title": fmt.format_display_title(
                    path,
                    r.plex_title,
                    r.plex_show_title,
                ),
                "plex_year": r.plex_year or fmt.year_from_path(path),
                "edition": fmt.edition_from_path(path),
                "video_summary": fmt.format_video(
                    r.video_width,
                    r.video_height,
                    r.video_hdr,
                ),
                "audio_summary": fmt.format_audio(audio),
                "sub_chip": fmt.format_sub_chip(subs),
                "has_error": bool(r.mediainfo_error or r.ardetector_error),
                "aspects": aspects,
                "aspects_truncated": aspects_truncated,
                "plex_url": fmt.plex_deeplink(
                    server_url,
                    machine_id,
                    r.plex_rating_key,
                ),
                "tautulli_url": fmt.tautulli_deeplink(
                    tautulli_url,
                    r.plex_rating_key,
                ),
                "bazarr_url": (
                    fmt.bazarr_movie_deeplink(bazarr_url, bazarr_movies[path])
                    if path in bazarr_movies
                    else fmt.bazarr_series_deeplink(
                        bazarr_url,
                        ancestor_match(path, bazarr_series),
                    )
                    if bazarr_url
                    else None
                ),
                "radarr_url": fmt.radarr_deeplink(
                    radarr_url,
                    radarr_movies.get(path),
                ),
                "sonarr_url": fmt.sonarr_deeplink(
                    sonarr_url,
                    ancestor_match(path, sonarr_series),
                ),
            },
        )
    # TV libraries: roll episodes up under show + season header rows.
    # Avoids a 1700-row wall for shows with many seasons. Grouping is
    # purely visual — no click-to-expand, no nesting — so Cmd-F still
    # works and the rail still jumps by show-title letter.
    if is_tv:
        rows = group_tv_rows(rows)
        titles = sum(1 for r in rows if r.get("kind") == "show")
        episodes = sum(1 for r in rows if r.get("kind") == "episode")
    else:
        titles = sum(1 for r in rows if r.get("kind") == "movie")
        episodes = None

    # Letter-jump rail: anchor on show headers (TV) / movie rows (films);
    # skip season + episode rows so the alphabet tracks shows, not
    # whatever happens to be the first-letter of an episode title.
    jump_anchor_kinds = {"show", "movie"}
    last_letter: str | None = None
    available: set[str] = set()
    for r in rows:
        if r.get("kind") in jump_anchor_kinds:
            letter = jump_letter(r["display_title"])
            available.add(letter)
            r["jump_letter"] = letter if letter != last_letter else None
            last_letter = letter
        else:
            r["jump_letter"] = None

    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "label": lib["label"],
            "rows": rows,
            "titles": titles,
            "episodes": episodes,
            "is_tv": is_tv,
            "libraries": libraries(config),
            "current_slug": slug,
            "jump_letters": JUMP_LETTERS,
            "available_letters": available,
        },
    )


@app.get("/item", response_class=HTMLResponse, include_in_schema=False)
async def item_detail(request: Request, path: str) -> HTMLResponse:
    mf = db.get(path)
    if mf is None:
        raise HTTPException(status_code=404, detail=f"no record for {path}")
    config: Config = app.state.config
    lib = next(
        (
            lib
            for lib in libraries(config)
            if any(path.startswith(p) for p in lib["paths"])
        ),
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
            (r for r in library_rows_for(lib) if not is_extra(r.path)),
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

    audio_rows = db.get_audio_tracks(path)
    subtitle_rows = db.get_subtitle_tracks(path)
    audio_view, subtitle_view = annotate_tracks(path, audio_rows, subtitle_rows)
    plex_item = db.get_plex_item_by_local_path(path)
    mi = db.get_mediainfo(path)
    ar = db.get_ardetector(path)
    aspect_set = json.loads(ar.aspect_samples) if ar and ar.aspect_samples else None
    machine_id = await plex.get_machine_identifier()
    server_url = config.plex.url or plex.get_server_url()
    rating_key = plex_item.rating_key if plex_item else None

    # Bonus features: siblings of the main movie that live in an Extras/
    # Interviews/Featurettes/... subfolder. Skip when viewing an extra
    # directly — no nested grouping.
    extras: list[dict] = []
    if not is_extra(path):
        parent_prefix = str(Path(path).parent) + "/"
        extra_paths = sorted(
            p for p in db.list_paths() if p.startswith(parent_prefix) and is_extra(p)
        )
        for ep in extra_paths:
            emf = db.get(ep)
            if emf is None:
                continue
            emi = db.get_mediainfo(ep)
            ear = db.get_ardetector(ep)
            ea = db.get_audio_tracks(ep)
            es = db.get_subtitle_tracks(ep)
            ea_view, es_view = annotate_tracks(ep, ea, es)
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
            "bazarr_url": bazarr_url_for(config.bazarr.url, path),
            "radarr_url": radarr_url_for(config.radarr.url, path),
            "sonarr_url": sonarr_url_for(config.sonarr.url, path),
            "extras": extras,
            "libraries": libraries(config),
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
    mf: db.MediaFileRow,
    mi: db.MediainfoRow | None,
    ar: db.ArdetectorRow | None,
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
    config: Config = app.state.config
    for f in plex_files:
        row = db.get_by_remote_path(f, config.plex.path_map)
        if row is not None:
            return InfoByContentIdResponse(
                **build_info(row).model_dump(),
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
    row = db.get(path)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no record for {path}")
    return build_info(row)


@api.post("/webhook")
async def webhook(request: Request) -> dict:
    """Accept webhook posts from any upstream (Plex / Sonarr / Radarr).

    Dispatches by sniffing the payload shape:
      - Plex sends ``multipart/form-data`` with a JSON ``payload`` field.
      - Sonarr/Radarr send application/json with ``eventType`` + a
        movie/series envelope. (TODO: wire those handlers when we have
        concrete payload samples.)
    """
    ct = request.headers.get("content-type", "")
    if "multipart/form-data" in ct:
        form = await request.form()
        raw = form.get("payload")
        if raw is None:
            raise HTTPException(status_code=400, detail="missing 'payload' field")
        try:
            payload = plex_sync.parse_webhook_payload(
                raw if isinstance(raw, str) else await raw.read(),
            )
        except plex.PlexError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = await plex_sync.handle_webhook(payload, app.state.config)
        local_path = result.get("local_path")
        if local_path:
            scanner.enqueue_probe(Path(local_path))
        return result
    # JSON — Sonarr/Radarr shapes land here. Not yet implemented.
    raise HTTPException(
        status_code=415,
        detail="only Plex (multipart/form-data) webhooks handled right now",
    )


@api.post("/task/scan")
async def task_scan() -> dict:
    """Incremental library sweep: pick up new files, reprobe changed ones."""
    spawn(scanner.full_scan(app.state.config))
    return {"triggered": True, "task": "scan"}


@api.post("/task/scan/{file_path:path}")
async def task_scan_one(file_path: str) -> dict:
    """Incremental scan of a single file: probe iff not already fresh.

    The typical caller is a webhook — "Sonarr just imported this,
    please probe if you haven't already". Idempotent: returning
    ``scanned: true`` means the DB row already matches the file's
    size+mtime, so nothing was enqueued.
    """
    p = Path("/" + file_path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"not a file: {p}")
    scanned = scanner.is_scanned(p)
    if not scanned:
        scanner.enqueue_probe(p)
    return {
        "triggered": not scanned,
        "task": "scan",
        "path": str(p),
        "scanned": scanned,
    }


@api.post("/task/refresh")
async def task_refresh_all() -> dict:
    """Force-refresh mediainfo on every file. AR cache preserved."""
    spawn(scanner.full_scan(app.state.config, force_mediainfo=True))
    return {"triggered": True, "task": "refresh"}


@api.post("/task/refresh/{file_path:path}")
async def task_refresh_one(file_path: str) -> dict:
    p = Path("/" + file_path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"not a file: {p}")
    enqueued = scanner.enqueue_probe(p, force_mediainfo=True)
    return {"triggered": True, "task": "refresh", "path": str(p), "enqueued": enqueued}


@api.post("/task/analyze")
async def task_analyze_all() -> dict:
    """Force-reprobe AR and mediainfo on every file. Slow."""
    spawn(scanner.full_scan(app.state.config, force=True))
    return {"triggered": True, "task": "analyze"}


@api.post("/task/analyze/{file_path:path}")
async def task_analyze_one(file_path: str) -> dict:
    p = Path("/" + file_path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"not a file: {p}")
    enqueued = scanner.enqueue_probe(p, force=True)
    return {"triggered": True, "task": "analyze", "path": str(p), "enqueued": enqueued}


SYNC_HANDLERS = {
    "plex": plex_sync.sync_once,
    "bazarr": bazarr_sync.sync_once,
    "radarr": radarr_sync.sync_once,
    "sonarr": sonarr_sync.sync_once,
}


@api.post("/task/sync")
async def task_sync_all() -> dict:
    """Kick off every external-service sync in parallel."""
    config = app.state.config
    for handler in SYNC_HANDLERS.values():
        spawn(handler(config))
    return {"triggered": True, "task": "sync", "services": list(SYNC_HANDLERS)}


@api.post("/task/sync/{service}")
async def task_sync_one(service: str) -> dict:
    handler = SYNC_HANDLERS.get(service)
    if handler is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown service: {service} "
            f"(expected one of {sorted(SYNC_HANDLERS)})",
        )
    spawn(handler(app.state.config))
    return {"triggered": True, "task": "sync", "service": service}


app.include_router(api)
