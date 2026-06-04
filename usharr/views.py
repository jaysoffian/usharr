"""Library-grid presentation: turn LibraryRow records into the typed view rows
the library template renders. Pure view logic — no DB access and no request
handling (those live in usharr.queries / usharr.app).

The grid is a tagged union: file rows (movies/episodes) plus the show/season
header rows ``group_tv_rows`` inserts. The template branches on ``kind``.
"""

import json
from collections import Counter
from dataclasses import dataclass

from usharr import format as fmt
from usharr import models, queries
from usharr.config import BazarrConfig, Config


@dataclass
class FileRow:
    """A movie or episode row: identity + plex metadata + formatted columns."""

    kind: str  # "movie" | "episode"
    path: str
    plex_title: str | None
    plex_show_title: str | None
    plex_season_number: int | None
    plex_episode_number: int | None
    display_title: str
    plex_year: int | None
    edition: str | None
    video_summary: str
    audio_summary: str
    sub_chip: dict | None
    has_error: bool
    aspects: list[dict]
    aspects_truncated: bool
    plex_url: str | None
    tautulli_url: str | None
    bazarr_url: str | None
    radarr_url: str | None
    sonarr_url: str | None
    jump_letter: str | None = None


@dataclass
class ShowHeader:
    """A show banner spanning its seasons; counts filled after the episodes."""

    display_title: str
    show_title: str
    plex_year: int | None
    season_count: int = 0
    episode_count: int = 0
    jump_letter: str | None = None
    kind: str = "show"


@dataclass
class SeasonHeader:
    display_title: str
    show_title: str
    season_number: int
    jump_letter: str | None = None
    kind: str = "season"


GridRow = FileRow | ShowHeader | SeasonHeader


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


def render_row(
    r: queries.LibraryRow,
    *,
    audio: list,
    subs: list,
    movie: models.Movie | None,
    config: Config,
    server_url: str | None,
    machine_id: str | None,
) -> FileRow:
    """Flatten one LibraryRow into a FileRow for the template. Absent overlays
    surface as None."""
    p, mi, ar, s = r.plex, r.mediainfo, r.ardetector, r.series
    path = r.video.path
    rating_key = p.rating_key if p else None
    aspect_set = json.loads(ar.aspect_samples) if ar and ar.aspect_samples else None
    aspects, aspects_truncated = fmt.format_aspects_for_row(
        aspect_set, ar.aspect_primary if ar else None
    )
    season = p.season_number if p else None
    show_title = p.show_title if p else None
    title = p.title if p else None
    return FileRow(
        kind="episode" if season is not None else "movie",
        path=path,
        plex_title=title,
        plex_show_title=show_title,
        plex_season_number=season,
        plex_episode_number=p.episode_number if p else None,
        display_title=fmt.format_display_title(path, title, show_title),
        plex_year=(p.year if p else None) or fmt.year_from_path(path),
        edition=fmt.edition_from_path(path),
        video_summary=fmt.format_video(
            mi.video_width if mi else None,
            mi.video_height if mi else None,
            mi.video_hdr if mi else None,
        ),
        audio_summary=fmt.format_audio(audio),
        sub_chip=fmt.format_sub_chip(subs),
        has_error=bool((mi.error if mi else None) or (ar.error if ar else None)),
        aspects=aspects,
        aspects_truncated=aspects_truncated,
        plex_url=fmt.plex_deeplink(server_url, machine_id, rating_key),
        tautulli_url=fmt.tautulli_deeplink(config.tautulli.url, rating_key),
        bazarr_url=bazarr_link(r, movie, config.bazarr),
        radarr_url=fmt.radarr_deeplink(
            config.radarr.url, movie.tmdb_id if movie else None
        ),
        sonarr_url=fmt.sonarr_deeplink(config.sonarr.url, s.title_slug if s else None),
    )


def group_tv_rows(episode_rows: list[FileRow]) -> list[GridRow]:
    """Insert show + season header rows into a sorted episode list.

    Input rows are already ordered by show title → season → episode
    (see ``library_sort_key``). A show header is emitted when the show changes;
    a season header when the season changes within the show. Episodes are
    retitled ``"<N>. <Episode Title>"`` so the season header carries the season
    context. Non-episode rows (mixed-in movies) pass through untouched.
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
                display_title=show, show_title=show, plex_year=r.plex_year
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
        # Drop the show prefix (the header already names it) and prefix the
        # episode number so "1. Pilot" reads naturally under "Season 1".
        ep_num = r.plex_episode_number
        ep_title = r.plex_title or fmt.format_display_title(r.path, None, None)
        r.display_title = f"{ep_num}. {ep_title}" if ep_num is not None else ep_title
        out.append(r)
        if show:
            show_ep_count[show] += 1
    for show, header in show_headers.items():
        header.season_count = len(show_seasons[show])
        header.episode_count = show_ep_count[show]
    return out
