"""Async data access over the Oxyde models.

Returns model instances directly — they double as the presentation/API layer,
so there's no separate row-dataclass copy. Connection lifecycle and migrations
live in ``usharr.database``; the models themselves in ``usharr.models``.

Referential integrity is DB-enforced (FK CASCADE — sqlx enables
``PRAGMA foreign_keys`` per connection by default), so deleting a ``video_file``
row reaps its mediainfo/ardetector/tracks; deleting a ``subtitle_file`` reaps
its external tracks.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from oxyde.db import transaction
from oxyde.queries.raw import execute_raw

from usharr.models import (
    Ardetector,
    AudioTrack,
    Mediainfo,
    Movie,
    PlexItem,
    Series,
    SubtitleFile,
    SubtitleTrackExternal,
    SubtitleTrackInternal,
    VideoFile,
)

logger = logging.getLogger(__name__)


# --- path mapping ---------------------------------------------------------


def map_remote_path(remote: str, path_map: dict[str, str]) -> str:
    """Rewrite a remote path to local using a {local: remote} prefix map.

    Returns the input unchanged when no prefix matches — fine for setups where
    the container sees the same tree under the same paths as the remote service.
    """
    for local, r in path_map.items():
        r2 = r.rstrip("/")
        if remote == r2 or remote.startswith(r2 + "/"):
            return local.rstrip("/") + remote[len(r2) :]
    return remote


async def file_exists(p: str) -> bool:
    return await VideoFile.objects.filter(path=p).exists()


async def folder_has_files(p: str) -> bool:
    return await VideoFile.objects.filter(path__startswith=p.rstrip("/") + "/").exists()


async def resolve_local_file(
    remote: str | None, path_map: dict[str, str]
) -> str | None:
    """Map a remote file path to local; None if it doesn't match a video_file row."""
    if not remote:
        return None
    mapped = map_remote_path(remote, path_map)
    return mapped if await file_exists(mapped) else None


async def resolve_local_folder(
    remote: str | None, path_map: dict[str, str]
) -> str | None:
    """Map a remote folder path to local; None if no video_file row sits under it."""
    if not remote:
        return None
    mapped = map_remote_path(remote, path_map)
    return mapped if await folder_has_files(mapped) else None


# --- video_file -----------------------------------------------------------


async def get(path: Path | str) -> VideoFile | None:
    return await VideoFile.objects.get_or_none(path=str(path))


async def get_by_remote_path(remote: str, path_map: dict[str, str]) -> VideoFile | None:
    return await get(map_remote_path(remote, path_map))


async def upsert_video_file(*, path: Path, size_bytes: int, mtime_ns: int) -> None:
    """Insert a video_file row, or refresh stat fields if it already exists."""
    await VideoFile.objects.update_or_create(
        path=str(path),
        defaults={"size_bytes": size_bytes, "mtime_ns": mtime_ns},
    )


async def list_paths() -> set[str]:
    return {v.path for v in await VideoFile.objects.all()}


async def delete_orphans(present: Iterable[Path | str]) -> None:
    """Delete video_file rows whose path is not in ``present`` (FK cascade reaps
    the dependent mediainfo/ardetector/track rows)."""
    orphans = sorted(await list_paths() - {str(p) for p in present})
    if not orphans:
        return
    total = 0
    for i in range(0, len(orphans), 500):
        chunk = orphans[i : i + 500]
        total += await VideoFile.objects.filter(path__in=chunk).delete()
    logger.info("Removed %d stale row(s) from DB", total)


# --- mediainfo ------------------------------------------------------------


async def get_mediainfo(path: Path | str) -> Mediainfo | None:
    return await Mediainfo.objects.get_or_none(video_path=str(path))


async def upsert_mediainfo(
    row: Mediainfo,
    *,
    audio: Iterable[AudioTrack],
    internal_subs: Iterable[SubtitleTrackInternal],
) -> None:
    """Upsert the mediainfo row and replace the file's audio + internal subtitle
    tracks. External subtitles aren't touched (see ``replace_external_subtitles``).
    """
    path = row.video_path
    async with transaction.atomic():
        await Mediainfo.objects.update_or_create(
            video_path=path,
            defaults=row.model_dump(exclude={"id", "video", "video_path"}),
        )
        # Tracks are a variable-length set, so replace them wholesale.
        await AudioTrack.objects.filter(video_path=path).delete()
        await AudioTrack.objects.bulk_create(list(audio))
        await SubtitleTrackInternal.objects.filter(video_path=path).delete()
        await SubtitleTrackInternal.objects.bulk_create(list(internal_subs))


async def delete_mediainfo(path: Path | str) -> None:
    await Mediainfo.objects.filter(video_path=str(path)).delete()


async def set_mediainfo_error(path: Path | str, error: str) -> None:
    """Record an error on the mediainfo row, preserving any cached track
    metadata; inserts a near-blank row if none exists."""
    await Mediainfo.objects.update_or_create(
        video_path=str(path), defaults={"error": error}
    )


async def set_mediainfo_duration(path: Path | str, duration: float) -> None:
    """Backfill mediainfo.duration from the ardetector pass when mediainfo didn't
    get one. No-op if no mediainfo row exists or duration is already set."""
    await Mediainfo.objects.filter(video_path=str(path), duration__isnull=True).update(
        duration=duration
    )


# --- ardetector -----------------------------------------------------------


async def get_ardetector(path: Path | str) -> Ardetector | None:
    return await Ardetector.objects.get_or_none(video_path=str(path))


async def delete_ardetector(path: Path | str) -> None:
    await Ardetector.objects.filter(video_path=str(path)).delete()


async def upsert_ardetector(row: Ardetector) -> None:
    await Ardetector.objects.update_or_create(
        video_path=row.video_path,
        defaults=row.model_dump(exclude={"id", "video", "video_path"}),
    )


# --- tracks ---------------------------------------------------------------


async def get_audio_tracks(path: str) -> list[AudioTrack]:
    return await AudioTrack.objects.filter(video_path=path).order_by("idx").all()


async def get_subtitle_tracks(
    path: str,
) -> tuple[list[SubtitleTrackInternal], list[SubtitleTrackExternal]]:
    """Internal tracks (by idx), then external tracks grouped by sidecar file."""
    internal = (
        await SubtitleTrackInternal.objects.filter(video_path=path)
        .order_by("idx")
        .all()
    )
    sub_paths = [
        f.path for f in await SubtitleFile.objects.filter(video_path=path).all()
    ]
    external: list[SubtitleTrackExternal] = []
    if sub_paths:
        external = (
            await SubtitleTrackExternal.objects.filter(subtitle_path__in=sub_paths)
            .order_by("subtitle_path", "idx")
            .all()
        )
    return internal, external


async def subtitle_files_for(video_path: Path | str) -> dict[str, tuple[int, int]]:
    """Return {subtitle_path: (size_bytes, mtime_ns)} for a video's sidecars."""
    rows = await SubtitleFile.objects.filter(video_path=str(video_path)).all()
    return {r.path: (r.size_bytes, r.mtime_ns) for r in rows}


async def replace_external_subtitles(
    *,
    video_path: Path,
    files: Iterable[tuple[str, int, int, list[SubtitleTrackExternal]]],
) -> None:
    """Replace every external sidecar (and its tracks) for one video. Deleting the
    subtitle_file rows cascades the external subtitle_track rows; internal tracks
    are untouched."""
    video_str = str(video_path)
    files = list(files)
    sub_files = [
        SubtitleFile.model_validate(
            {
                "path": path,
                "size_bytes": size_bytes,
                "mtime_ns": mtime_ns,
                "video_path": video_str,
            }
        )
        for path, size_bytes, mtime_ns, _ in files
    ]
    tracks = [t for *_, file_tracks in files for t in file_tracks]
    async with transaction.atomic():
        await SubtitleFile.objects.filter(video_path=video_str).delete()
        # Files before tracks: the external-track FK targets subtitle_file.path.
        await SubtitleFile.objects.bulk_create(sub_files)
        await SubtitleTrackExternal.objects.bulk_create(tracks)


# --- library listing ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LibraryRow:
    """One library-grid file: its video_file plus the optional mediainfo /
    ardetector / plex_item overlays and the Series whose folder contains it. The
    properties expose the null-safe flat fields the grid, sort key, and
    detail-page navigation read; the detail page and /api/info read the models
    directly. Owns the grid's read queries.
    """

    video: VideoFile
    mediainfo: Mediainfo | None
    ardetector: Ardetector | None
    plex: PlexItem | None
    series: Series | None

    @property
    def path(self) -> str:
        return self.video.path

    @property
    def mediainfo_error(self) -> str | None:
        return self.mediainfo.error if self.mediainfo else None

    @property
    def ardetector_error(self) -> str | None:
        return self.ardetector.error if self.ardetector else None

    @property
    def video_width(self) -> int | None:
        return self.mediainfo.video_width if self.mediainfo else None

    @property
    def video_height(self) -> int | None:
        return self.mediainfo.video_height if self.mediainfo else None

    @property
    def video_hdr(self) -> str | None:
        return self.mediainfo.video_hdr if self.mediainfo else None

    @property
    def aspect_primary(self) -> float | None:
        return self.ardetector.aspect_primary if self.ardetector else None

    @property
    def aspect_samples(self) -> str | None:
        return self.ardetector.aspect_samples if self.ardetector else None

    @property
    def plex_rating_key(self) -> str | None:
        return self.plex.rating_key if self.plex else None

    @property
    def plex_title(self) -> str | None:
        return self.plex.title if self.plex else None

    @property
    def plex_year(self) -> int | None:
        return self.plex.year if self.plex else None

    @property
    def plex_show_title(self) -> str | None:
        return self.plex.show_title if self.plex else None

    @property
    def plex_season_number(self) -> int | None:
        return self.plex.season_number if self.plex else None

    @property
    def plex_episode_number(self) -> int | None:
        return self.plex.episode_number if self.plex else None

    @property
    def series_id(self) -> int | None:
        return self.series.id if self.series else None

    @property
    def series_slug(self) -> str | None:
        return self.series.title_slug if self.series else None

    @staticmethod
    def _series_for(path: str, by_folder: dict[str, Series]) -> Series | None:
        """The Series whose video_folder is the deepest ancestor of ``path`` —
        the read-time, structure-agnostic file→series link."""
        p = Path(path).parent
        while True:
            if (s := by_folder.get(str(p))) is not None:
                return s
            parent = p.parent
            if parent == p:
                return None
            p = parent

    @classmethod
    async def _overlays(
        cls, path_prefix: str
    ) -> tuple[dict[str, Mediainfo], dict[str, Ardetector], dict[str, PlexItem]]:
        mi = {
            m.video_path: m
            for m in await Mediainfo.objects.filter(
                video_path__startswith=path_prefix
            ).all()
            if m.video_path
        }
        ar = {
            a.video_path: a
            for a in await Ardetector.objects.filter(
                video_path__startswith=path_prefix
            ).all()
            if a.video_path
        }
        plex = {
            p.video_path: p
            for p in await PlexItem.objects.filter(
                video_path__startswith=path_prefix
            ).all()
            if p.video_path
        }
        return mi, ar, plex

    @classmethod
    async def for_prefix(cls, path_prefix: str) -> list[LibraryRow]:
        """video_file LEFT JOIN mediainfo / ardetector / plex_item, plus the
        Series its folder contains (resolved by prefix), as flat rows."""
        videos = (
            await VideoFile.objects.filter(path__startswith=path_prefix)
            .order_by("path")
            .all()
        )
        mi, ar, plex = await cls._overlays(path_prefix)
        series_by_folder = {s.video_folder: s for s in await Series.objects.all()}
        return [
            cls(
                v,
                mi.get(v.path),
                ar.get(v.path),
                plex.get(v.path),
                cls._series_for(v.path, series_by_folder),
            )
            for v in videos
        ]

    @classmethod
    async def tracks_for_prefix(
        cls, path_prefix: str
    ) -> tuple[dict[str, list[AudioTrack]], dict[str, list[SubtitleTrackInternal]]]:
        """(audio_by_path, internal_subtitle_by_path) for every file under prefix."""
        audio = (
            await AudioTrack.objects.filter(video_path__startswith=path_prefix)
            .order_by("video_path", "idx")
            .all()
        )
        subs = (
            await SubtitleTrackInternal.objects.filter(
                video_path__startswith=path_prefix
            )
            .order_by("video_path", "idx")
            .all()
        )
        audio_by: dict[str, list[AudioTrack]] = {}
        for a in audio:
            if a.video_path:
                audio_by.setdefault(a.video_path, []).append(a)
        sub_by: dict[str, list[SubtitleTrackInternal]] = {}
        for s in subs:
            if s.video_path:
                sub_by.setdefault(s.video_path, []).append(s)
        return audio_by, sub_by

    @classmethod
    async def movies_for_prefix(cls, path_prefix: str) -> dict[str, Movie]:
        """Radarr movie overlay keyed by local path (for Radarr + Bazarr links)."""
        rows = await Movie.objects.filter(video_path__startswith=path_prefix).all()
        return {m.video_path: m for m in rows if m.video_path}


# --- plex_item ------------------------------------------------------------


async def upsert_plex_item(
    *,
    rating_key: str,
    item_type: str,
    path_map: dict[str, str],
    title: str | None = None,
    year: int | None = None,
    show_title: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    remote_path: str | None = None,
) -> str | None:
    """Upsert a plex_item iff it resolves to a local file. Returns the local path
    (or None — unresolved items aren't stored)."""
    local_path = await resolve_local_file(remote_path, path_map)
    if local_path is None:
        return None
    await PlexItem.objects.update_or_create(
        rating_key=rating_key,
        defaults={
            "type": item_type,
            "title": title,
            "year": year,
            "show_title": show_title,
            "season_number": season_number,
            "episode_number": episode_number,
            "video_path": local_path,
        },
    )
    return local_path


async def get_plex_item_by_local_path(local_path: str) -> PlexItem | None:
    return await PlexItem.objects.filter(video_path=local_path).first()


async def get_plex_item(rating_key: str) -> PlexItem | None:
    return await PlexItem.objects.get_or_none(rating_key=rating_key)


async def list_plex_rating_keys() -> set[str]:
    return {p.rating_key for p in await PlexItem.objects.all()}


async def delete_plex_rating_keys(keys: list[str]) -> int:
    total = 0
    for i in range(0, len(keys), 500):
        total += await PlexItem.objects.filter(
            rating_key__in=keys[i : i + 500]
        ).delete()
    return total


# --- radarr (Movie overlay) -----------------------------------------------


async def upsert_radarr_movie(
    *,
    movie_id: int,
    path_map: dict[str, str],
    tmdb_id: int | None = None,
    remote_path: str | None = None,
) -> None:
    local_path = await resolve_local_file(remote_path, path_map)
    if local_path is None:
        return
    await Movie.objects.update_or_create(
        id=movie_id,
        defaults={"tmdb_id": tmdb_id, "video_path": local_path},
    )


async def list_radarr_movie_ids() -> set[int]:
    return {m.id for m in await Movie.objects.all()}


async def delete_radarr_movies(ids: list[int]) -> int:
    total = 0
    for i in range(0, len(ids), 500):
        total += await Movie.objects.filter(id__in=ids[i : i + 500]).delete()
    return total


async def radarr_tmdb_for_local_path(local_path: str) -> int | None:
    m = await Movie.objects.filter(video_path=local_path, tmdb_id__isnull=False).first()
    return m.tmdb_id if m else None


async def all_radarr_movies_by_local_path() -> dict[str, int]:
    rows = await Movie.objects.filter(tmdb_id__isnull=False).all()
    return {
        m.video_path: m.tmdb_id for m in rows if m.video_path and m.tmdb_id is not None
    }


async def radarr_id_for_local_path(local_path: str) -> int | None:
    """Radarr movie id (for the Bazarr /movies/{id} deep-link)."""
    m = await Movie.objects.filter(video_path=local_path).first()
    return m.id if m else None


# --- sonarr (Series) ------------------------------------------------------


async def upsert_sonarr_series(
    *,
    series_id: int,
    path_map: dict[str, str],
    title_slug: str | None = None,
    remote_path: str | None = None,
) -> None:
    local_folder = await resolve_local_folder(remote_path, path_map)
    if local_folder is None:
        return
    await Series.objects.update_or_create(
        id=series_id,
        defaults={"title_slug": title_slug, "video_folder": local_folder},
    )


async def list_sonarr_series_ids() -> set[int]:
    return {s.id for s in await Series.objects.all()}


async def delete_sonarr_series(ids: list[int]) -> int:
    total = 0
    for i in range(0, len(ids), 500):
        total += await Series.objects.filter(id__in=ids[i : i + 500]).delete()
    return total


async def series_for_local_path(local_path: str) -> Series | None:
    """The Series whose video_folder is the longest prefix of ``local_path``."""
    rows = await execute_raw(
        "SELECT id FROM series"
        " WHERE ? LIKE video_folder || '/%'"
        " ORDER BY LENGTH(video_folder) DESC LIMIT 1",
        [local_path],
    )
    if not rows:
        return None
    return await Series.objects.get_or_none(id=rows[0]["id"])
