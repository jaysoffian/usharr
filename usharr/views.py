"""Library-grid presentation: turn LibraryRow records into the flat template
dicts the library page renders. Pure view logic — no DB access and no request
handling (those live in usharr.queries / usharr.app)."""

import json

from usharr import format as fmt
from usharr import models, queries
from usharr.config import Config


def row_links(
    r: queries.LibraryRow,
    movie: models.Movie | None,
    config: Config,
    server_url: str | None,
    machine_id: str | None,
) -> dict:
    """The five integration deep-links for a grid row, from the overlays already
    loaded (no per-row query). Bazarr links off the Radarr movie id when present,
    else the Sonarr series id, gated by config."""
    p, s = r.plex, r.series
    rating_key = p.rating_key if p else None
    series_id = s.id if s else None
    bazarr = config.bazarr
    return {
        "plex_url": fmt.plex_deeplink(server_url, machine_id, rating_key),
        "tautulli_url": fmt.tautulli_deeplink(config.tautulli.url, rating_key),
        "bazarr_url": (
            fmt.bazarr_movie_deeplink(bazarr.url, movie.id)
            if bazarr.url and bazarr.link_movies and movie is not None
            else fmt.bazarr_series_deeplink(bazarr.url, series_id)
            if bazarr.url and bazarr.link_series and series_id is not None
            else None
        ),
        "radarr_url": fmt.radarr_deeplink(
            config.radarr.url, movie.tmdb_id if movie else None
        ),
        "sonarr_url": fmt.sonarr_deeplink(
            config.sonarr.url, s.title_slug if s else None
        ),
    }


def render_row(
    r: queries.LibraryRow,
    *,
    audio: list,
    subs: list,
    movie: models.Movie | None,
    config: Config,
    server_url: str | None,
    machine_id: str | None,
) -> dict:
    """Flatten one LibraryRow into the dict the library template renders:
    identity + plex metadata + the formatted video/audio/subtitle/aspect
    summaries + integration links. Absent overlays surface as None."""
    p, mi, ar = r.plex, r.mediainfo, r.ardetector
    path = r.video.path
    aspect_set = json.loads(ar.aspect_samples) if ar and ar.aspect_samples else None
    aspects, aspects_truncated = fmt.format_aspects_for_row(
        aspect_set, ar.aspect_primary if ar else None
    )
    season = p.season_number if p else None
    show_title = p.show_title if p else None
    title = p.title if p else None
    return {
        "kind": "episode" if season is not None else "movie",
        "path": path,
        "plex_title": title,
        "plex_show_title": show_title,
        "plex_season_number": season,
        "plex_episode_number": p.episode_number if p else None,
        "display_title": fmt.format_display_title(path, title, show_title),
        "plex_year": (p.year if p else None) or fmt.year_from_path(path),
        "edition": fmt.edition_from_path(path),
        "video_summary": fmt.format_video(
            mi.video_width if mi else None,
            mi.video_height if mi else None,
            mi.video_hdr if mi else None,
        ),
        "audio_summary": fmt.format_audio(audio),
        "sub_chip": fmt.format_sub_chip(subs),
        "has_error": bool((mi.error if mi else None) or (ar.error if ar else None)),
        "aspects": aspects,
        "aspects_truncated": aspects_truncated,
        **row_links(r, movie, config, server_url, machine_id),
    }


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
