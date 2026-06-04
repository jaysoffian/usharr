"""View helpers for the library grid and detail templates, plus the library
ordering / navigation helpers. Pure view logic — no DB access and no request
handling (those live in usharr.queries / usharr.app).

The grid renders directly from the data models: ``LibraryRow`` file rows plus
the show/season header rows ``group_tv_rows`` inserts. The template branches on
``kind`` and calls these helpers (registered as Jinja globals) for the computed
display columns.
"""

from collections import Counter
from dataclasses import dataclass

from usharr import format as fmt
from usharr import models, queries
from usharr.audio_title import clean_audio_title
from usharr.config import Config

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
    aspect_set = ar.aspect_samples_parsed if ar else None
    return fmt.format_aspects_for_row(aspect_set, ar.aspect_primary if ar else None)


# --- grid link helpers (take the request context the page resolves) --------


def plex_url(
    plex: models.PlexItem | None, server_url: str | None, machine_id: str | None
) -> str | None:
    return fmt.plex_deeplink(server_url, machine_id, plex.rating_key if plex else None)


def tautulli_url(plex: models.PlexItem | None, base: str | None) -> str | None:
    return fmt.tautulli_deeplink(base, plex.rating_key if plex else None)


def bazarr_url(
    movie: models.Movie | None, series: models.Series | None, config: Config
) -> str | None:
    """Bazarr deep-link off the Radarr movie id when present, else the Sonarr
    series id — gated by config."""
    bazarr = config.bazarr
    if not bazarr.url:
        return None
    if bazarr.link_movies and movie is not None:
        return fmt.bazarr_movie_deeplink(bazarr.url, movie.id)
    if bazarr.link_series and series is not None:
        return fmt.bazarr_series_deeplink(bazarr.url, series.id)
    return None


def radarr_url(movie: models.Movie | None, base: str | None) -> str | None:
    return fmt.radarr_deeplink(base, movie.tmdb_id if movie else None)


def sonarr_url(series: models.Series | None, base: str | None) -> str | None:
    return fmt.sonarr_deeplink(base, series.title_slug if series else None)


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


# --- grid assembly ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Grid:
    rows: list[GridRow]
    jump_letters: list[str | None]
    titles: int
    episodes: int | None
    is_tv: bool
    available_letters: set[str]


def build_grid(rows_data: list[queries.LibraryRow]) -> Grid:
    """Turn fetched library rows into everything the grid template needs:
    the (optionally TV-grouped) rows, title/episode counts, and the jump rail."""
    is_tv = any(r.kind == "episode" for r in rows_data)
    # TV libraries: roll episodes up under show + season header rows.
    # Avoids a 1700-row wall for shows with many seasons. Grouping is
    # purely visual — no click-to-expand, no nesting — so Cmd-F still
    # works and the rail still jumps by show-title letter.
    rows: list[GridRow] = group_tv_rows(rows_data) if is_tv else list(rows_data)
    if is_tv:
        titles = sum(1 for r in rows if r.kind == "show")
        episodes: int | None = sum(1 for r in rows if r.kind == "episode")
    else:
        titles = sum(1 for r in rows if r.kind == "movie")
        episodes = None

    # Letter-jump rail: anchor on show headers (TV) / movie rows (films);
    # skip season + episode rows so the alphabet tracks shows, not
    # whatever happens to be the first-letter of an episode title. The
    # parallel jump_letters list carries the per-row anchor letter (or None).
    jump_letters: list[str | None] = []
    last_letter: str | None = None
    available: set[str] = set()
    for r in rows:
        if isinstance(r, ShowHeader):
            title = r.display_title
        elif isinstance(r, queries.LibraryRow) and r.kind == "movie":
            title = grid_title(r)
        else:
            jump_letters.append(None)
            continue
        letter = jump_letter(title)
        available.add(letter)
        jump_letters.append(letter if letter != last_letter else None)
        last_letter = letter

    return Grid(
        rows=rows,
        jump_letters=jump_letters,
        titles=titles,
        episodes=episodes,
        is_tv=is_tv,
        available_letters=available,
    )


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
    return clean_audio_title(t.title, t.language)


def audio_details(t: models.AudioTrack) -> str:
    return fmt.format_audio_details(t)


def sub_lang(t: fmt.SubtitleTrack) -> str:
    return fmt.lang_name(t.language)
