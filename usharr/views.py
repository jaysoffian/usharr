"""Presentation layer: shape the data models into the typed view rows the
library and detail templates render, plus the library ordering / navigation
helpers. Pure view logic — no DB access and no request handling (those live in
usharr.queries / usharr.app).

The grid is a tagged union: file rows (movies/episodes) plus the show/season
header rows ``group_tv_rows`` inserts. The template branches on ``kind``.
"""

import json
from collections import Counter
from dataclasses import dataclass

from usharr import format as fmt
from usharr import models, queries
from usharr.config import BazarrConfig, Config

JUMP_LETTERS: tuple[str, ...] = ("#", *(chr(c) for c in range(ord("A"), ord("Z") + 1)))


@dataclass
class FileRow:
    """A movie or episode grid row: the source ``LibraryRow`` plus only the
    computed presentation the template adds (formatted summaries, deep-links).
    Source fields are read through ``data`` — never copied onto this row."""

    data: queries.LibraryRow
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

    @property
    def kind(self) -> str:
        return "episode" if self.data.plex_season_number is not None else "movie"


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
    config: Config,
    server_url: str | None,
    machine_id: str | None,
) -> FileRow:
    """Flatten one LibraryRow into a FileRow for the template. Absent overlays
    surface as None."""
    plex, mi, ar, series, movie = (
        r.plex,
        r.mediainfo,
        r.ardetector,
        r.series,
        r.movie,
    )
    rating_key = plex.rating_key if plex else None
    aspect_set = json.loads(ar.aspect_samples) if ar and ar.aspect_samples else None
    aspects, aspects_truncated = fmt.format_aspects_for_row(
        aspect_set, ar.aspect_primary if ar else None
    )
    return FileRow(
        data=r,
        display_title=fmt.format_display_title(r.path, r.plex_title, r.plex_show_title),
        plex_year=(plex.year if plex else None) or fmt.year_from_path(r.path),
        edition=fmt.edition_from_path(r.path),
        video_summary=fmt.format_video(
            mi.video_width if mi else None,
            mi.video_height if mi else None,
            mi.video_hdr if mi else None,
        ),
        audio_summary=fmt.format_audio(r.audio),
        sub_chip=fmt.format_sub_chip(r.subtitles),
        has_error=bool((mi.error if mi else None) or (ar.error if ar else None)),
        aspects=aspects,
        aspects_truncated=aspects_truncated,
        plex_url=fmt.plex_deeplink(server_url, machine_id, rating_key),
        tautulli_url=fmt.tautulli_deeplink(config.tautulli.url, rating_key),
        bazarr_url=bazarr_link(r, movie, config.bazarr),
        radarr_url=fmt.radarr_deeplink(
            config.radarr.url, movie.tmdb_id if movie else None
        ),
        sonarr_url=fmt.sonarr_deeplink(
            config.sonarr.url, series.title_slug if series else None
        ),
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
        show = r.data.plex_show_title
        season = r.data.plex_season_number
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
        ep_num = r.data.plex_episode_number
        ep_title = r.data.plex_title or fmt.format_display_title(
            r.data.path, None, None
        )
        r.display_title = f"{ep_num}. {ep_title}" if ep_num is not None else ep_title
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


# --- detail page ----------------------------------------------------------


@dataclass
class DetailView:
    """media_file + mediainfo + ardetector flattened for the detail template.
    Absent probe rows surface as None, which the template renders as '—'."""

    path: str
    container: str | None
    video_codec: str | None
    video_profile: str | None
    video_width: int | None
    video_height: int | None
    video_bit_depth: int | None
    video_hdr: str | None
    video_hdr_format: str | None
    video_frame_rate: float | None
    video_bit_rate: int | None
    video_max_bit_rate: int | None
    aspect_primary: float | None
    color: str
    error: str | None


def detail_view(
    mf: models.VideoFile,
    mi: models.Mediainfo | None,
    ar: models.Ardetector | None,
) -> DetailView:
    errors: list[str] = []
    if mi and mi.error:
        errors.append(f"mediainfo: {mi.error}")
    if ar and ar.error:
        errors.append(f"ardetector: {ar.error}")
    return DetailView(
        path=mf.path,
        container=mi.container if mi else None,
        video_codec=mi.video_codec if mi else None,
        video_profile=mi.video_profile if mi else None,
        video_width=mi.video_width if mi else None,
        video_height=mi.video_height if mi else None,
        video_bit_depth=mi.video_bit_depth if mi else None,
        video_hdr=mi.video_hdr if mi else None,
        video_hdr_format=mi.video_hdr_format if mi else None,
        video_frame_rate=mi.video_frame_rate if mi else None,
        video_bit_rate=mi.video_bit_rate if mi else None,
        video_max_bit_rate=mi.video_max_bit_rate if mi else None,
        aspect_primary=ar.aspect_primary if ar else None,
        color=fmt.format_color(ar.color_pct if ar else None),
        error="; ".join(errors) if errors else None,
    )


# --- detail-page track tables ---------------------------------------------


@dataclass
class AudioView:
    """An audio track for the detail table: the model plus display strings."""

    track: models.AudioTrack
    details: str
    lang_display: str
    title_display: str


@dataclass
class SubtitleView:
    """A subtitle track for the detail table: the model plus display strings."""

    track: fmt.SubtitleTrack
    lang_display: str
    file_ext: str


def annotate_tracks(
    media_path: str,
    audio: list[models.AudioTrack],
    subtitle: list[fmt.SubtitleTrack],
) -> tuple[list[AudioView], list[SubtitleView]]:
    """Pair each audio/subtitle track with its computed display fields."""
    audio_view = [
        AudioView(
            track=t,
            details=fmt.format_audio_details(t),
            lang_display=fmt.lang_name(t.language),
            title_display=fmt.clean_audio_title(t.title, t.language),
        )
        for t in audio
    ]
    file_exts = fmt.subtitle_file_exts(media_path, subtitle)
    subtitle_view = [
        SubtitleView(track=t, lang_display=fmt.lang_name(t.language), file_ext=ext)
        for t, ext in zip(subtitle, file_exts, strict=True)
    ]
    return audio_view, subtitle_view
