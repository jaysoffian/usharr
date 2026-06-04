"""View helpers for the library grid and detail templates, plus the library
ordering / navigation helpers. Pure view logic — no DB access and no request
handling (those live in usharr.queries / usharr.app).

The grid renders directly from the data models: ``LibraryRow`` file rows plus
the show/season header rows ``group_tv_rows`` inserts. The template branches on
``kind`` and calls these helpers (registered as Jinja globals) for the computed
display columns.
"""

import json
from collections import Counter
from dataclasses import dataclass

from usharr import format as fmt
from usharr import models, queries
from usharr.config import BazarrConfig, Config

JUMP_LETTERS: tuple[str, ...] = ("#", *(chr(c) for c in range(ord("A"), ord("Z") + 1)))


@dataclass
class ShowHeader:
    """A show banner spanning its seasons; counts filled after the episodes."""

    display_title: str
    show_title: str
    plex_year: int | None
    season_count: int = 0
    episode_count: int = 0
    kind: str = "show"


@dataclass
class SeasonHeader:
    display_title: str
    show_title: str
    season_number: int
    kind: str = "season"


GridRow = queries.LibraryRow | ShowHeader | SeasonHeader


# --- grid column helpers ---------------------------------------------------


def grid_title(r: queries.LibraryRow) -> str:
    """Episodes are shown grouped, so prefix the number and drop the show name
    (the season header carries it); movies/standalone use the full title."""
    if r.plex_season_number is not None:
        ep = r.plex_title or fmt.format_display_title(r.path, None, None)
        return f"{r.plex_episode_number}. {ep}"
    return fmt.format_display_title(r.path, r.plex_title, r.plex_show_title)


def grid_year(r: queries.LibraryRow) -> int | None:
    return (r.plex.year if r.plex else None) or fmt.year_from_path(r.path)


def grid_edition(r: queries.LibraryRow) -> str | None:
    return fmt.edition_from_path(r.path)


def video_summary(r: queries.LibraryRow) -> str:
    mi = r.mediainfo
    return fmt.format_video(
        mi.video_width if mi else None,
        mi.video_height if mi else None,
        mi.video_hdr if mi else None,
    )


def audio_summary(r: queries.LibraryRow) -> str:
    return fmt.format_audio(r.audio)


def sub_chip(r: queries.LibraryRow) -> dict | None:
    return fmt.format_sub_chip(r.subtitles)


def has_error(r: queries.LibraryRow) -> bool:
    mi, ar = r.mediainfo, r.ardetector
    return bool((mi.error if mi else None) or (ar.error if ar else None))


def aspects(r: queries.LibraryRow) -> tuple[list[dict], bool]:
    ar = r.ardetector
    aspect_set = json.loads(ar.aspect_samples) if ar and ar.aspect_samples else None
    return fmt.format_aspects_for_row(aspect_set, ar.aspect_primary if ar else None)


# --- grid link helpers (take the request context the page resolves) --------


def bazarr_link(
    r: queries.LibraryRow, movie: models.Movie | None, bazarr: BazarrConfig
) -> str | None:
    """Bazarr deep-link off the Radarr movie id when present, else the Sonarr
    series id — gated by config, from the overlays already loaded."""
    if not bazarr.url:
        return None
    if bazarr.link_movies and movie is not None:
        return fmt.bazarr_movie_deeplink(bazarr.url, movie.id)
    series_id = r.series.id if r.series else None
    if bazarr.link_series and series_id is not None:
        return fmt.bazarr_series_deeplink(bazarr.url, series_id)
    return None


def plex_url(
    r: queries.LibraryRow, server_url: str | None, machine_id: str | None
) -> str | None:
    return fmt.plex_deeplink(
        server_url, machine_id, r.plex.rating_key if r.plex else None
    )


def tautulli_url(r: queries.LibraryRow, base: str | None) -> str | None:
    return fmt.tautulli_deeplink(base, r.plex.rating_key if r.plex else None)


def bazarr_url(r: queries.LibraryRow, config: Config) -> str | None:
    return bazarr_link(r, r.movie, config.bazarr)


def radarr_url(r: queries.LibraryRow, base: str | None) -> str | None:
    return fmt.radarr_deeplink(base, r.movie.tmdb_id if r.movie else None)


def sonarr_url(r: queries.LibraryRow, base: str | None) -> str | None:
    return fmt.sonarr_deeplink(base, r.series.title_slug if r.series else None)


# --- TV grouping -----------------------------------------------------------


def group_tv_rows(episode_rows: list[queries.LibraryRow]) -> list[GridRow]:
    """Insert show + season header rows into a sorted episode list.

    Input rows are already ordered by show title → season → episode
    (see ``library_sort_key``). A show header is emitted when the show changes;
    a season header when the season changes within the show. Non-episode rows
    (mixed-in movies) pass through untouched.
    """
    out: list[GridRow] = []
    current_show: str | None = None
    current_season: int | None = None
    show_headers: dict[str, ShowHeader] = {}
    show_seasons: dict[str, set] = {}
    show_ep_count: Counter[str] = Counter()
    for r in episode_rows:
        if r.kind != "episode":
            out.append(r)
            continue
        show = r.plex_show_title
        season = r.plex_season_number
        if show and show != current_show:
            current_show = show
            current_season = None
            header = ShowHeader(
                display_title=show, show_title=show, plex_year=grid_year(r)
            )
            out.append(header)
            show_headers[show] = header
            show_seasons[show] = set()
        if show and season != current_season:
            current_season = season
            label = "Specials" if season in (None, 0) else f"Season {season}"
            out.append(
                SeasonHeader(
                    display_title=label,
                    show_title=show,
                    season_number=season if season is not None else 0,
                )
            )
            show_seasons[show].add(season)
        out.append(r)
        if show:
            show_ep_count[show] += 1
    for show, header in show_headers.items():
        header.season_count = len(show_seasons[show])
        header.episode_count = show_ep_count[show]
    return out


# --- library ordering + jump rail -----------------------------------------


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


def jump_letter(display_title: str) -> str:
    norm = fmt.sort_normalize(display_title)
    if not norm:
        return "#"
    c = norm[0]
    return c.upper() if c.isalpha() else "#"


# --- detail-page prev/next navigation -------------------------------------


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


# --- detail-page helpers --------------------------------------------------


def dash(v: object) -> object:
    return v if v not in (None, "") else "—"


def color(ar: models.Ardetector | None) -> str:
    return fmt.format_color(ar.color_pct if ar else None)


def detail_error(
    mi: models.Mediainfo | None, ar: models.Ardetector | None
) -> str | None:
    """The combined ``mediainfo: .. ; ardetector: ..`` string, or None."""
    errors: list[str] = []
    if mi and mi.error:
        errors.append(f"mediainfo: {mi.error}")
    if ar and ar.error:
        errors.append(f"ardetector: {ar.error}")
    return "; ".join(errors) if errors else None


def audio_lang(t: models.AudioTrack) -> str:
    return fmt.lang_name(t.language)


def audio_title(t: models.AudioTrack) -> str:
    return fmt.clean_audio_title(t.title, t.language)


def audio_details(t: models.AudioTrack) -> str:
    return fmt.format_audio_details(t)


def sub_lang(t: fmt.SubtitleTrack) -> str:
    return fmt.lang_name(t.language)
