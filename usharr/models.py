"""Oxyde ORM models — the data layer and the API/presentation layer in one.

Schema notes:
  * ``video_file`` is the discovery row (path/size/mtime); a row exists iff the
    file has been seen on disk.
  * ``mediainfo`` / ``ardetector`` are 1:1 with ``video_file`` (one surrogate-id
    row per probed file, a unique ``video_path``). Row presence ⇒ probed; the
    ``error`` column distinguishes success from a recorded failure.
  * Subtitle tracks split by origin: ``subtitle_track_internal`` (container
    tracks, keyed under the video) and ``subtitle_track_external`` (sidecar-file
    tracks, keyed under ``subtitle_file``).
  * External-service overlays (``plex_item``, ``movie``, ``series``) are synced
    independently and reconciled by their service id.

Oxyde caveats baked into these definitions:
  * FK = an optional relation field; the scalar FK column (``video_path`` etc.)
    is what you write/filter by, and ``join()`` hydrates the relation.
  * ``db_pk`` on a relation field is ignored, so 1:1 children use a surrogate
    ``id`` PK + a unique index on the FK column.
  * Composite/conditional uniqueness goes through ``Meta.indexes`` (Index),
    never ``Meta.unique_together`` (silently dropped).
  * String columns need ``db_type="TEXT"`` or they render ``VARCHAR(255)``.
"""

import json
from typing import ClassVar

from oxyde import Field, Index, Model


class VideoFile(Model):
    path: str = Field(db_pk=True, db_type="TEXT")
    size_bytes: int
    mtime_ns: int

    class Meta:
        is_table = True
        table_name = "video_file"


class SubtitleFile(Model):
    path: str = Field(db_pk=True, db_type="TEXT")
    video: VideoFile | None = Field(
        default=None, db_on_delete="CASCADE", db_nullable=False
    )
    size_bytes: int
    mtime_ns: int

    class Meta:
        is_table = True
        table_name = "subtitle_file"
        indexes: ClassVar[list[Index]] = [
            Index(("video_path",), name="subtitle_file_video_idx")
        ]


class Mediainfo(Model):
    id: int | None = Field(default=None, db_pk=True)
    video: VideoFile | None = Field(
        default=None, db_on_delete="CASCADE", db_nullable=False
    )
    error: str | None = Field(default=None, db_type="TEXT")
    container: str | None = Field(default=None, db_type="TEXT")
    duration: float | None = None
    video_codec: str | None = Field(default=None, db_type="TEXT")
    video_profile: str | None = Field(default=None, db_type="TEXT")
    video_width: int | None = None
    video_height: int | None = None
    video_bit_depth: int | None = None
    video_hdr: str | None = Field(default=None, db_type="TEXT")
    video_hdr_format: str | None = Field(default=None, db_type="TEXT")
    video_frame_rate: float | None = None
    video_bit_rate: int | None = None
    video_max_bit_rate: int | None = None

    class Meta:
        is_table = True
        table_name = "mediainfo"
        indexes: ClassVar[list[Index]] = [
            Index(("video_path",), unique=True, name="mediainfo_video_uq")
        ]


class Ardetector(Model):
    id: int | None = Field(default=None, db_pk=True)
    video: VideoFile | None = Field(
        default=None, db_on_delete="CASCADE", db_nullable=False
    )
    error: str | None = Field(default=None, db_type="TEXT")
    aspect_primary: float | None = None
    aspect_widest: float | None = None
    aspect_samples: str | None = Field(default=None, db_type="TEXT")  # JSON list
    color_pct: float | None = None  # 0.0=monochrome, 1.0=color; NULL=unknown

    class Meta:
        is_table = True
        table_name = "ardetector"
        indexes: ClassVar[list[Index]] = [
            Index(("video_path",), unique=True, name="ardetector_video_uq")
        ]

    @property
    def aspect_samples_parsed(self) -> list[dict] | None:
        return json.loads(self.aspect_samples) if self.aspect_samples else None


class AudioTrack(Model):
    id: int | None = Field(default=None, db_pk=True)
    video: VideoFile | None = Field(
        default=None, db_on_delete="CASCADE", db_nullable=False
    )
    idx: int
    codec: str | None = Field(default=None, db_type="TEXT")
    channels: int | None = None
    layout: str | None = Field(default=None, db_type="TEXT")
    language: str | None = Field(default=None, db_type="TEXT")
    title: str | None = Field(default=None, db_type="TEXT")
    is_default: bool = False
    is_forced: bool = False
    format: str | None = Field(default=None, db_type="TEXT")
    commercial_name: str | None = Field(default=None, db_type="TEXT")
    bit_rate: int | None = None
    bit_rate_mode: str | None = Field(default=None, db_type="TEXT")
    sample_rate: int | None = None
    bit_depth: int | None = None
    compression_mode: str | None = Field(default=None, db_type="TEXT")

    class Meta:
        is_table = True
        table_name = "audio_track"
        indexes: ClassVar[list[Index]] = [
            Index(("video_path", "idx"), unique=True, name="audio_track_uq")
        ]


class SubtitleTrackInternal(Model):
    id: int | None = Field(default=None, db_pk=True)
    video: VideoFile | None = Field(
        default=None, db_on_delete="CASCADE", db_nullable=False
    )
    idx: int
    codec: str | None = Field(default=None, db_type="TEXT")
    language: str | None = Field(default=None, db_type="TEXT")
    title: str | None = Field(default=None, db_type="TEXT")
    is_default: bool = False
    is_forced: bool = False
    is_sdh: bool = False

    class Meta:
        is_table = True
        table_name = "subtitle_track_internal"
        indexes: ClassVar[list[Index]] = [
            Index(("video_path", "idx"), unique=True, name="subtitle_internal_uq")
        ]


class SubtitleTrackExternal(Model):
    id: int | None = Field(default=None, db_pk=True)
    subtitle: SubtitleFile | None = Field(
        default=None, db_on_delete="CASCADE", db_nullable=False
    )
    idx: int
    codec: str | None = Field(default=None, db_type="TEXT")
    language: str | None = Field(default=None, db_type="TEXT")
    title: str | None = Field(default=None, db_type="TEXT")
    is_default: bool = False
    is_forced: bool = False
    is_sdh: bool = False

    class Meta:
        is_table = True
        table_name = "subtitle_track_external"
        indexes: ClassVar[list[Index]] = [
            Index(("subtitle_path", "idx"), unique=True, name="subtitle_external_uq")
        ]


class PlexItem(Model):
    rating_key: str = Field(db_pk=True, db_type="TEXT")
    type: str = Field(db_type="TEXT")
    title: str | None = Field(default=None, db_type="TEXT")
    year: int | None = None
    show_title: str | None = Field(default=None, db_type="TEXT")
    season_number: int | None = None
    episode_number: int | None = None
    # Only resolved items are stored, so the local file link is NOT NULL.
    video: VideoFile | None = Field(
        default=None, db_on_delete="CASCADE", db_nullable=False
    )

    class Meta:
        is_table = True
        table_name = "plex_item"
        indexes: ClassVar[list[Index]] = [
            Index(("video_path",), name="plex_item_video_idx")
        ]


class Movie(Model):
    """Radarr movie overlay. ``id`` is the Radarr movie id (Bazarr
    ``/movies/{id}`` deep-link); ``tmdb_id`` drives the Radarr ``/movie/{tmdbId}``
    deep-link. Only locally-resolved movies are stored.
    """

    id: int = Field(db_pk=True)  # Radarr movie id (externally assigned)
    tmdb_id: int | None = None
    video: VideoFile | None = Field(
        default=None, db_on_delete="CASCADE", db_nullable=False
    )

    class Meta:
        is_table = True
        table_name = "movie"
        indexes: ClassVar[list[Index]] = [
            Index(("video_path",), name="movie_video_idx")
        ]


class Series(Model):
    """A TV show occupying a folder, tracked by Sonarr. ``id`` is the Sonarr
    series id (used directly for the Bazarr ``/episodes/{id}`` deep-link).
    """

    id: int = Field(db_pk=True)  # Sonarr series id (externally assigned)
    video_folder: str = Field(db_type="TEXT")
    title_slug: str | None = Field(
        default=None, db_type="TEXT"
    )  # Sonarr /series/{slug}

    class Meta:
        is_table = True
        table_name = "series"


class PlexAuth(Model):
    """Single-row Plex link state acquired via ``usharr auth`` (PIN OAuth).
    Always id=1. ``client_id`` is set on first use; the rest on linking.
    """

    id: int = Field(db_pk=True)  # always 1
    client_id: str | None = Field(default=None, db_type="TEXT")
    token: str | None = Field(default=None, db_type="TEXT")
    server_url: str | None = Field(default=None, db_type="TEXT")
    server_name: str | None = Field(default=None, db_type="TEXT")
    machine_id: str | None = Field(default=None, db_type="TEXT")

    class Meta:
        is_table = True
        table_name = "plex_auth"
