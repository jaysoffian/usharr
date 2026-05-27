"""SQLite-backed store: media files, tracks, Plex items, and kv.

Read accessors return frozen-slots dataclasses (``MediaFileRow``,
``MediainfoRow``, ``ArdetectorRow``, ``AudioTrackRow``,
``SubtitleTrackRow``, ``PlexItemRow``, ``LibraryRow``).
View-layer code that needs to add template fields should
``dataclasses.asdict(row)`` first and work in dict-space from there.

Schema overview:
  * ``media_file`` is the discovery row — path/size/mtime/subtitles +
    ``discovered_at``. A row exists iff we've seen the file on disk.
  * ``mediainfo`` holds the cheap track-metadata pass (container,
    video_*, duration). Row presence ⇒ at least one attempt; ``error``
    column distinguishes success from a recorded failure.
  * ``ardetector`` holds the slow ardetector pass (aspect_primary,
    aspect_widest, aspect_samples JSON). Same row-presence semantics
    as ``mediainfo``.

Stub-vs-probed is read off table presence — no nullable-mush heuristic
is needed.
"""

import logging
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("USHARR_DB", "/config/usharr.db"))
DB_DIR = DB_PATH.parent

db: sqlite3.Connection | None = None


CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS media_file (
    path              TEXT PRIMARY KEY,
    size_bytes        INTEGER NOT NULL,
    mtime_ns          INTEGER NOT NULL,
    subtitles_mtime_ns INTEGER,
    discovered_at     INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS mediainfo (
    path               TEXT PRIMARY KEY REFERENCES media_file(path) ON DELETE CASCADE,
    probed_at          INTEGER NOT NULL,
    error              TEXT,
    container          TEXT,
    duration           REAL,
    video_codec        TEXT,
    video_profile      TEXT,
    video_width        INTEGER,
    video_height       INTEGER,
    video_bit_depth    INTEGER,
    video_hdr          TEXT,
    video_hdr_format   TEXT,
    video_frame_rate   REAL,
    video_bit_rate     INTEGER,
    video_max_bit_rate INTEGER
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ardetector (
    path           TEXT PRIMARY KEY REFERENCES media_file(path) ON DELETE CASCADE,
    probed_at      INTEGER NOT NULL,
    error          TEXT,
    aspect_primary REAL,
    aspect_widest  REAL,
    aspect_samples TEXT  -- JSON list of {aspect, percentage} samples
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS audio_track (
    path             TEXT NOT NULL REFERENCES media_file(path) ON DELETE CASCADE,
    idx              INTEGER NOT NULL,
    codec            TEXT,
    channels         INTEGER,
    layout           TEXT,
    language         TEXT,
    title            TEXT,
    is_default       INTEGER NOT NULL DEFAULT 0,
    is_forced        INTEGER NOT NULL DEFAULT 0,
    format           TEXT,
    commercial_name  TEXT,
    bit_rate         INTEGER,
    bit_rate_mode    TEXT,
    sample_rate      INTEGER,
    bit_depth        INTEGER,
    compression_mode TEXT,
    PRIMARY KEY (path, idx)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS subtitle_track (
    path       TEXT NOT NULL REFERENCES media_file(path) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    source     TEXT NOT NULL,
    file_path  TEXT,
    codec      TEXT,
    language   TEXT,
    title      TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    is_forced  INTEGER NOT NULL DEFAULT 0,
    is_sdh     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (path, idx)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS plex_item (
    rating_key     TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    title          TEXT,
    year           INTEGER,
    show_title     TEXT,
    season_number  INTEGER,
    episode_number INTEGER,
    local_path     TEXT,
    updated_at     INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bazarr_movie (
    radarr_id    INTEGER PRIMARY KEY,
    local_path   TEXT,             -- remote movie file path mapped via path_map
    updated_at   INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bazarr_series (
    sonarr_id    INTEGER PRIMARY KEY,
    local_folder TEXT,             -- remote show folder mapped via path_map
    updated_at   INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS radarr_movie (
    movie_id   INTEGER PRIMARY KEY,  -- Radarr's internal id
    tmdb_id    INTEGER,               -- for /movie/{tmdbId} deep-links
    local_path TEXT,                  -- remote movie file path mapped via path_map
    updated_at INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sonarr_series (
    series_id    INTEGER PRIMARY KEY, -- Sonarr's internal id
    title_slug   TEXT,                -- for /series/{slug} deep-links
    local_folder TEXT,                -- remote series folder mapped via path_map
    updated_at   INTEGER NOT NULL
) WITHOUT ROWID;
"""

CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS plex_item_local_path       ON plex_item(local_path);
CREATE INDEX IF NOT EXISTS plex_item_type             ON plex_item(type);
CREATE INDEX IF NOT EXISTS bazarr_movie_local_path    ON bazarr_movie(local_path);
CREATE INDEX IF NOT EXISTS bazarr_series_local_folder ON bazarr_series(local_folder);
CREATE INDEX IF NOT EXISTS radarr_movie_local_path    ON radarr_movie(local_path);
CREATE INDEX IF NOT EXISTS sonarr_series_local_folder ON sonarr_series(local_folder);
"""


# Bump when mediainfo extraction gains fields so existing rows get re-probed
# on next scan. Aspect data, external subs, and plex_item/bazarr_* rows are
# preserved — only the mediainfo pass reruns.
MEDIAINFO_SCHEMA_VERSION = 7


# --- typed row containers -------------------------------------------------


@dataclass(frozen=True, slots=True)
class MediaFileRow:
    path: str
    size_bytes: int
    mtime_ns: int
    subtitles_mtime_ns: int | None
    discovered_at: int


@dataclass(frozen=True, slots=True)
class MediainfoRow:
    path: str
    probed_at: int
    error: str | None
    container: str | None
    duration: float | None
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


@dataclass(frozen=True, slots=True)
class ArdetectorRow:
    path: str
    probed_at: int
    error: str | None
    aspect_primary: float | None
    aspect_widest: float | None
    aspect_samples: str | None


@dataclass(frozen=True, slots=True)
class AudioTrackRow:
    idx: int
    codec: str | None
    channels: int | None
    layout: str | None
    language: str | None
    title: str | None
    is_default: bool
    is_forced: bool
    format: str | None
    commercial_name: str | None
    bit_rate: int | None
    bit_rate_mode: str | None
    sample_rate: int | None
    bit_depth: int | None
    compression_mode: str | None


@dataclass(frozen=True, slots=True)
class SubtitleTrackRow:
    idx: int
    source: str
    file_path: str | None
    codec: str | None
    language: str | None
    title: str | None
    is_default: bool
    is_forced: bool
    is_sdh: bool


@dataclass(frozen=True, slots=True)
class PlexItemRow:
    rating_key: str
    type: str
    title: str | None
    year: int | None
    show_title: str | None
    season_number: int | None
    episode_number: int | None
    local_path: str | None
    updated_at: int


@dataclass(frozen=True, slots=True)
class LibraryRow:
    # media_file fields (mirrors MediaFileRow)
    path: str
    size_bytes: int
    mtime_ns: int
    subtitles_mtime_ns: int | None
    discovered_at: int
    # mediainfo fields (NULL when no mediainfo row yet — i.e. stub).
    mediainfo_probed_at: int | None
    mediainfo_error: str | None
    container: str | None
    duration: float | None
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
    # ardetector fields (NULL when no ardetector row yet).
    ardetector_probed_at: int | None
    ardetector_error: str | None
    aspect_primary: float | None
    aspect_widest: float | None
    aspect_samples: str | None
    # plex_item fields aliased with a plex_ prefix; nullable via LEFT JOIN.
    plex_rating_key: str | None
    plex_type: str | None
    plex_title: str | None
    plex_year: int | None
    plex_show_title: str | None
    plex_season_number: int | None
    plex_episode_number: int | None
    plex_updated_at: int | None


def make_row[T](
    cls: type[T],
    cols: tuple[str, ...],
    row: tuple,
    *,
    bool_fields: tuple[str, ...] = (),
) -> T:
    d = dict(zip(cols, row, strict=True))
    for f in bool_fields:
        d[f] = bool(d[f])
    return cls(**d)


def init_db() -> None:
    global db  # noqa: PLW0603
    if db is not None:
        return
    # USHARR_DB_RO implies `make dev` against a copied prod DB. Open the
    # SQLite file read-only so a stray menu click (or any other write
    # path) can't clobber the snapshot — failure surfaces at the DB
    # layer regardless of which endpoint tried to mutate.
    if os.environ.get("USHARR_DB_RO"):
        db = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
        logger.info("Opened DB at %s (read-only)", DB_PATH)
        return
    DB_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(
        str(DB_PATH),
        isolation_level=None,
        check_same_thread=False,
    )
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(CREATE_TABLES)
    db.executescript(CREATE_INDEXES)
    maybe_rename_subtitles_column()
    maybe_reprobe_on_schema_bump()
    logger.info("Opened DB at %s", DB_PATH)


def maybe_rename_subtitles_column() -> None:
    """One-shot rename of media_file.sidecars_mtime_ns -> subtitles_mtime_ns.

    No-op on fresh DBs (CREATE TABLE already used the new name) and on
    DBs that have already been migrated.
    """
    conn = get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(media_file)").fetchall()}
    if "sidecars_mtime_ns" not in cols:
        return
    conn.execute(
        "ALTER TABLE media_file RENAME COLUMN sidecars_mtime_ns TO subtitles_mtime_ns"
    )
    logger.info("Renamed media_file.sidecars_mtime_ns to subtitles_mtime_ns")


def maybe_reprobe_on_schema_bump() -> None:
    """On mediainfo schema bumps, drop every `mediainfo` row so the next
    scan re-probes track metadata. `ardetector` rows (slow ffmpeg pass)
    and audio_track / subtitle_track rows are untouched — the next
    probe rebuilds those alongside the new mediainfo row.
    """
    stored = kv_get("mediainfo_schema_version") or "0"
    try:
        last = int(stored)
    except ValueError:
        last = 0
    if last >= MEDIAINFO_SCHEMA_VERSION:
        return
    conn = get_conn()
    n = conn.execute("DELETE FROM mediainfo").rowcount or 0
    if n:
        logger.info(
            "mediainfo schema v%d → v%d: dropped %d row(s) for re-probe",
            last,
            MEDIAINFO_SCHEMA_VERSION,
            n,
        )
    kv_set("mediainfo_schema_version", str(MEDIAINFO_SCHEMA_VERSION))


def close_db() -> None:
    global db  # noqa: PLW0603
    if db is not None:
        db.close()
        db = None


def get_conn() -> sqlite3.Connection:
    if db is None:
        msg = "db.init_db() has not been called"
        raise RuntimeError(msg)
    return db


# --- path mapping ---------------------------------------------------------


def map_remote_path(remote: str, path_map: dict[str, str]) -> str:
    """Rewrite a remote path to local using a {local: remote} prefix map.

    Returns the input unchanged when no prefix matches — fine for setups
    where the container sees the same tree under the same paths as the
    remote service (identical mounts).
    """
    for local, r in path_map.items():
        r2 = r.rstrip("/")
        if remote == r2 or remote.startswith(r2 + "/"):
            return local.rstrip("/") + remote[len(r2) :]
    return remote


def file_exists(p: str) -> bool:
    return (
        get_conn()
        .execute("SELECT 1 FROM media_file WHERE path = ? LIMIT 1", (p,))
        .fetchone()
        is not None
    )


def folder_has_files(p: str) -> bool:
    like = like_prefix(p.rstrip("/") + "/")
    return (
        get_conn()
        .execute(
            "SELECT 1 FROM media_file WHERE path LIKE ? ESCAPE '\\' LIMIT 1",
            (like,),
        )
        .fetchone()
        is not None
    )


def resolve_local_file(remote_path: str | None, path_map: dict[str, str]) -> str | None:
    """Map a remote file path to local; None if it doesn't match a media_file row."""
    if not remote_path:
        return None
    mapped = map_remote_path(remote_path, path_map)
    return mapped if file_exists(mapped) else None


def resolve_local_folder(
    remote_path: str | None, path_map: dict[str, str]
) -> str | None:
    """Map a remote folder path to local; None if no media_file row sits under it."""
    if not remote_path:
        return None
    mapped = map_remote_path(remote_path, path_map)
    return mapped if folder_has_files(mapped) else None


# --- media_file -----------------------------------------------------------

MEDIA_COLS = (
    "path",
    "size_bytes",
    "mtime_ns",
    "subtitles_mtime_ns",
    "discovered_at",
)


def get(path: Path | str) -> MediaFileRow | None:
    cols = ", ".join(MEDIA_COLS)
    row = (
        get_conn()
        .execute(f"SELECT {cols} FROM media_file WHERE path = ?", (str(path),))
        .fetchone()
    )
    if row is None:
        return None
    return make_row(MediaFileRow, MEDIA_COLS, row)


def get_by_remote_path(remote: str, path_map: dict[str, str]) -> MediaFileRow | None:
    """Map a remote path to local and return the matching media_file row."""
    return get(map_remote_path(remote, path_map))


def upsert_media_file(
    *,
    path: Path,
    size_bytes: int,
    mtime_ns: int,
    subtitles_mtime_ns: int | None,
    discovered_at: int,
) -> None:
    """Insert a media_file row, or refresh stat fields if it already exists.

    `discovered_at` is set on first insert and preserved on update.
    """
    get_conn().execute(
        "INSERT INTO media_file"
        " (path, size_bytes, mtime_ns, subtitles_mtime_ns, discovered_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(path) DO UPDATE SET"
        " size_bytes = excluded.size_bytes,"
        " mtime_ns = excluded.mtime_ns,"
        " subtitles_mtime_ns = excluded.subtitles_mtime_ns",
        (str(path), size_bytes, mtime_ns, subtitles_mtime_ns, discovered_at),
    )


# --- mediainfo ------------------------------------------------------------

MEDIAINFO_COLS = (
    "path",
    "probed_at",
    "error",
    "container",
    "duration",
    "video_codec",
    "video_profile",
    "video_width",
    "video_height",
    "video_bit_depth",
    "video_hdr",
    "video_hdr_format",
    "video_frame_rate",
    "video_bit_rate",
    "video_max_bit_rate",
)


def get_mediainfo(path: Path | str) -> MediainfoRow | None:
    cols = ", ".join(MEDIAINFO_COLS)
    row = (
        get_conn()
        .execute(f"SELECT {cols} FROM mediainfo WHERE path = ?", (str(path),))
        .fetchone()
    )
    if row is None:
        return None
    return make_row(MediainfoRow, MEDIAINFO_COLS, row)


def upsert_mediainfo(
    *,
    path: Path,
    probed_at: int,
    error: str | None,
    container: str | None,
    duration: float | None,
    video_codec: str | None,
    video_profile: str | None,
    video_width: int | None,
    video_height: int | None,
    video_bit_depth: int | None,
    video_hdr: str | None,
    video_hdr_format: str | None,
    video_frame_rate: float | None,
    video_bit_rate: int | None,
    video_max_bit_rate: int | None,
    audio: Iterable[AudioTrackRow] | None = None,
    internal_subs: Iterable[SubtitleTrackRow] | None = None,
) -> None:
    """Upsert the mediainfo row and (optionally) replace the file's
    audio + internal subtitle tracks.

    Pass ``audio`` / ``internal_subs`` as iterables (possibly empty) to
    replace the cached tracks; pass ``None`` to leave them alone (used
    when only the error/probed_at fields are being refreshed without
    new track data — see ``set_mediainfo_duration``).
    External subtitles aren't touched by this function — they live or
    die with the subtitle files (see ``update_external_subtitles``).
    """
    path_str = str(path)
    placeholders = ", ".join("?" * len(MEDIAINFO_COLS))
    cols = ", ".join(MEDIAINFO_COLS)
    values = (
        path_str,
        probed_at,
        error,
        container,
        duration,
        video_codec,
        video_profile,
        video_width,
        video_height,
        video_bit_depth,
        video_hdr,
        video_hdr_format,
        video_frame_rate,
        video_bit_rate,
        video_max_bit_rate,
    )
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO mediainfo ({cols}) VALUES ({placeholders})",
            values,
        )
        if audio is not None:
            conn.execute("DELETE FROM audio_track WHERE path = ?", (path_str,))
            for t in audio:
                conn.execute(
                    "INSERT INTO audio_track"
                    " (path, idx, codec, channels, layout, language, title,"
                    "  is_default, is_forced, format, commercial_name,"
                    "  bit_rate, bit_rate_mode, sample_rate, bit_depth,"
                    "  compression_mode)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        path_str,
                        t.idx,
                        t.codec,
                        t.channels,
                        t.layout,
                        t.language,
                        t.title,
                        1 if t.is_default else 0,
                        1 if t.is_forced else 0,
                        t.format,
                        t.commercial_name,
                        t.bit_rate,
                        t.bit_rate_mode,
                        t.sample_rate,
                        t.bit_depth,
                        t.compression_mode,
                    ),
                )
        if internal_subs is not None:
            conn.execute(
                "DELETE FROM subtitle_track WHERE path = ? AND source = 'internal'",
                (path_str,),
            )
            for t in internal_subs:
                conn.execute(
                    "INSERT INTO subtitle_track"
                    " (path, idx, source, file_path, codec, language, title,"
                    "  is_default, is_forced, is_sdh)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        path_str,
                        t.idx,
                        "internal",
                        t.file_path,
                        t.codec,
                        t.language,
                        t.title,
                        1 if t.is_default else 0,
                        1 if t.is_forced else 0,
                        1 if t.is_sdh else 0,
                    ),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def set_mediainfo_duration(path: Path, duration: float) -> None:
    """Backfill mediainfo.duration from the ardetector pass when mediainfo
    didn't get one (the AR sampler measures runtime as a side effect).
    No-op if no mediainfo row exists for `path`.
    """
    get_conn().execute(
        "UPDATE mediainfo SET duration = ? WHERE path = ? AND duration IS NULL",
        (duration, str(path)),
    )


# --- ardetector -----------------------------------------------------------

ARDETECTOR_COLS = (
    "path",
    "probed_at",
    "error",
    "aspect_primary",
    "aspect_widest",
    "aspect_samples",
)


def get_ardetector(path: Path | str) -> ArdetectorRow | None:
    cols = ", ".join(ARDETECTOR_COLS)
    row = (
        get_conn()
        .execute(f"SELECT {cols} FROM ardetector WHERE path = ?", (str(path),))
        .fetchone()
    )
    if row is None:
        return None
    return make_row(ArdetectorRow, ARDETECTOR_COLS, row)


def upsert_ardetector(
    *,
    path: Path,
    probed_at: int,
    error: str | None,
    aspect_primary: float | None,
    aspect_widest: float | None,
    aspect_samples: str | None,
) -> None:
    cols = ", ".join(ARDETECTOR_COLS)
    placeholders = ", ".join("?" * len(ARDETECTOR_COLS))
    get_conn().execute(
        f"INSERT OR REPLACE INTO ardetector ({cols}) VALUES ({placeholders})",
        (
            str(path),
            probed_at,
            error,
            aspect_primary,
            aspect_widest,
            aspect_samples,
        ),
    )


AUDIO_COLS = (
    "idx",
    "codec",
    "channels",
    "layout",
    "language",
    "title",
    "is_default",
    "is_forced",
    "format",
    "commercial_name",
    "bit_rate",
    "bit_rate_mode",
    "sample_rate",
    "bit_depth",
    "compression_mode",
)
AUDIO_BOOLS = ("is_default", "is_forced")

SUBTITLE_COLS = (
    "idx",
    "source",
    "file_path",
    "codec",
    "language",
    "title",
    "is_default",
    "is_forced",
    "is_sdh",
)
SUBTITLE_BOOLS = ("is_default", "is_forced", "is_sdh")


def get_audio_tracks(path: str) -> list[AudioTrackRow]:
    cols = ", ".join(AUDIO_COLS)
    rows = (
        get_conn()
        .execute(
            f"SELECT {cols} FROM audio_track WHERE path = ? ORDER BY idx",
            (path,),
        )
        .fetchall()
    )
    return [
        make_row(AudioTrackRow, AUDIO_COLS, r, bool_fields=AUDIO_BOOLS) for r in rows
    ]


def get_subtitle_tracks(path: str) -> list[SubtitleTrackRow]:
    """Internal subs (by their own idx) first, then externals (by idx)."""
    cols = ", ".join(SUBTITLE_COLS)
    rows = (
        get_conn()
        .execute(
            f"SELECT {cols} FROM subtitle_track WHERE path = ?"
            " ORDER BY source = 'external', idx",
            (path,),
        )
        .fetchall()
    )
    return [
        make_row(SubtitleTrackRow, SUBTITLE_COLS, r, bool_fields=SUBTITLE_BOOLS)
        for r in rows
    ]


def update_external_subtitles(
    *,
    path: Path,
    subtitles: Iterable[SubtitleTrackRow],
) -> None:
    """Replace only the external subtitle_track rows.

    Caller is responsible for keeping ``media_file.subtitles_mtime_ns``
    in sync via ``upsert_media_file`` — discovery state lives on the
    discovery row.
    """
    path_str = str(path)
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        conn.execute(
            "DELETE FROM subtitle_track WHERE path = ? AND source = 'external'",
            (path_str,),
        )
        for t in subtitles:
            conn.execute(
                "INSERT INTO subtitle_track"
                " (path, idx, source, file_path, codec, language, title,"
                "  is_default, is_forced, is_sdh)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    path_str,
                    t.idx,
                    "external",
                    t.file_path,
                    t.codec,
                    t.language,
                    t.title,
                    1 if t.is_default else 0,
                    1 if t.is_forced else 0,
                    1 if t.is_sdh else 0,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def list_paths() -> set[str]:
    return {row[0] for row in get_conn().execute("SELECT path FROM media_file")}


def library_rows(path_prefix: str) -> list[LibraryRow]:
    """media_file LEFT JOIN mediainfo / ardetector / plex_item.

    A NULL `mediainfo_probed_at` (and the associated columns) means the
    file is a stub — discovered but not yet probed for track metadata.
    Same for `ardetector_probed_at`. Audio and subtitle tracks are NOT
    included here; fetch via ``get_audio_tracks(path)`` /
    ``get_subtitle_tracks(path)`` as needed.
    """
    media_cols = ", ".join(f"m.{c}" for c in MEDIA_COLS)
    # mediainfo: skip the duplicate `path` column; alias `probed_at` /
    # `error` so they don't collide with the ardetector versions.
    mi_data = tuple(c for c in MEDIAINFO_COLS if c != "path")
    mi_select = ", ".join(
        f"mi.{c} AS mediainfo_{c}" if c in {"probed_at", "error"} else f"mi.{c}"
        for c in mi_data
    )
    ar_data = tuple(c for c in ARDETECTOR_COLS if c != "path")
    ar_select = ", ".join(
        f"ar.{c} AS ardetector_{c}" if c in {"probed_at", "error"} else f"ar.{c}"
        for c in ar_data
    )
    plex_cols = ", ".join(
        f"p.{c} AS plex_{c}" for c in PLEX_ITEM_COLS if c not in {"path", "local_path"}
    )
    rows = (
        get_conn()
        .execute(
            f"SELECT {media_cols}, {mi_select}, {ar_select}, {plex_cols}"
            " FROM media_file m"
            " LEFT JOIN mediainfo  mi ON mi.path = m.path"
            " LEFT JOIN ardetector ar ON ar.path = m.path"
            " LEFT JOIN plex_item   p ON p.local_path = m.path"
            " WHERE m.path LIKE ? ESCAPE '\\'"
            " ORDER BY m.path",
            (like_prefix(path_prefix),),
        )
        .fetchall()
    )
    mi_names = tuple(
        f"mediainfo_{c}" if c in {"probed_at", "error"} else c for c in mi_data
    )
    ar_names = tuple(
        f"ardetector_{c}" if c in {"probed_at", "error"} else c for c in ar_data
    )
    plex_names = tuple(
        f"plex_{c}" for c in PLEX_ITEM_COLS if c not in {"path", "local_path"}
    )
    col_names = (*MEDIA_COLS, *mi_names, *ar_names, *plex_names)
    return [make_row(LibraryRow, col_names, r) for r in rows]


def like_prefix(p: str) -> str:
    return p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def library_paths(path_prefix: str) -> list[str]:
    """Every media_file.path under the library, sorted. Caller may filter."""
    like = like_prefix(path_prefix)
    rows = (
        get_conn()
        .execute(
            "SELECT path FROM media_file WHERE path LIKE ? ESCAPE '\\' ORDER BY path",
            (like,),
        )
        .fetchall()
    )
    return [r[0] for r in rows]


def library_tracks(
    path_prefix: str,
) -> tuple[dict[str, list[AudioTrackRow]], dict[str, list[SubtitleTrackRow]]]:
    """Return (audio_by_path, subtitle_by_path) for every file under prefix."""
    conn = get_conn()
    audio: dict[str, list[AudioTrackRow]] = {}
    subtitle: dict[str, list[SubtitleTrackRow]] = {}
    like = like_prefix(path_prefix)
    audio_cols = ", ".join(AUDIO_COLS)
    for row in conn.execute(
        f"SELECT path, {audio_cols}"
        " FROM audio_track WHERE path LIKE ? ESCAPE '\\'"
        " ORDER BY path, idx",
        (like,),
    ):
        audio.setdefault(row[0], []).append(
            make_row(AudioTrackRow, AUDIO_COLS, row[1:], bool_fields=AUDIO_BOOLS)
        )
    subtitle_cols = ", ".join(SUBTITLE_COLS)
    for row in conn.execute(
        f"SELECT path, {subtitle_cols}"
        " FROM subtitle_track WHERE path LIKE ? ESCAPE '\\'"
        " ORDER BY path, idx",
        (like,),
    ):
        subtitle.setdefault(row[0], []).append(
            make_row(
                SubtitleTrackRow, SUBTITLE_COLS, row[1:], bool_fields=SUBTITLE_BOOLS
            )
        )
    return audio, subtitle


def delete_orphans(present: Iterable[Path | str]) -> int:
    """Delete media_file rows whose path is not in `present`."""
    orphans = sorted(list_paths() - {str(p) for p in present})
    if not orphans:
        return 0
    conn = get_conn()
    total = 0
    for i in range(0, len(orphans), 500):
        chunk = orphans[i : i + 500]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"DELETE FROM media_file WHERE path IN ({placeholders})",
            chunk,
        )
        total += cur.rowcount or 0
    return total


# --- plex_item ------------------------------------------------------------

PLEX_ITEM_COLS = (
    "rating_key",
    "type",
    "title",
    "year",
    "show_title",
    "season_number",
    "episode_number",
    "local_path",
    "updated_at",
)


def upsert_plex_item(
    *,
    rating_key: str,
    item_type: str,
    updated_at: int,
    path_map: dict[str, str],
    title: str | None = None,
    year: int | None = None,
    show_title: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    remote_path: str | None = None,
) -> str | None:
    """Upsert a plex_item. Returns the resolved local_path (or None)."""
    local_path = resolve_local_file(remote_path, path_map)
    cols = ", ".join(PLEX_ITEM_COLS)
    placeholders = ", ".join("?" * len(PLEX_ITEM_COLS))
    get_conn().execute(
        f"INSERT OR REPLACE INTO plex_item ({cols}) VALUES ({placeholders})",
        (
            rating_key,
            item_type,
            title,
            year,
            show_title,
            season_number,
            episode_number,
            local_path,
            updated_at,
        ),
    )
    return local_path


def get_plex_item_by_local_path(local_path: str) -> PlexItemRow | None:
    cols = ", ".join(PLEX_ITEM_COLS)
    row = (
        get_conn()
        .execute(
            f"SELECT {cols} FROM plex_item WHERE local_path = ? LIMIT 1",
            (local_path,),
        )
        .fetchone()
    )
    if row is None:
        return None
    return make_row(PlexItemRow, PLEX_ITEM_COLS, row)


def get_plex_item(rating_key: str) -> PlexItemRow | None:
    cols = ", ".join(PLEX_ITEM_COLS)
    row = (
        get_conn()
        .execute(
            f"SELECT {cols} FROM plex_item WHERE rating_key = ?",
            (rating_key,),
        )
        .fetchone()
    )
    if row is None:
        return None
    return make_row(PlexItemRow, PLEX_ITEM_COLS, row)


def list_plex_rating_keys() -> set[str]:
    return {row[0] for row in get_conn().execute("SELECT rating_key FROM plex_item")}


def delete_plex_rating_keys(keys: list[str]) -> int:
    if not keys:
        return 0
    conn = get_conn()
    total = 0
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"DELETE FROM plex_item WHERE rating_key IN ({placeholders})",
            chunk,
        )
        total += cur.rowcount or 0
    return total


# --- bazarr ---------------------------------------------------------------


def upsert_bazarr_movie(
    *,
    radarr_id: int,
    updated_at: int,
    path_map: dict[str, str],
    remote_path: str | None = None,
) -> None:
    local_path = resolve_local_file(remote_path, path_map)
    get_conn().execute(
        "INSERT OR REPLACE INTO bazarr_movie"
        " (radarr_id, local_path, updated_at)"
        " VALUES (?,?,?)",
        (radarr_id, local_path, updated_at),
    )


def upsert_bazarr_series(
    *,
    sonarr_id: int,
    updated_at: int,
    path_map: dict[str, str],
    remote_path: str | None = None,
) -> None:
    local_folder = resolve_local_folder(remote_path, path_map)
    get_conn().execute(
        "INSERT OR REPLACE INTO bazarr_series"
        " (sonarr_id, local_folder, updated_at)"
        " VALUES (?,?,?)",
        (sonarr_id, local_folder, updated_at),
    )


def list_bazarr_movie_ids() -> set[int]:
    return {row[0] for row in get_conn().execute("SELECT radarr_id FROM bazarr_movie")}


def list_bazarr_series_ids() -> set[int]:
    return {row[0] for row in get_conn().execute("SELECT sonarr_id FROM bazarr_series")}


def delete_bazarr_movies(ids: list[int]) -> int:
    return bulk_delete("bazarr_movie", "radarr_id", ids)


def delete_bazarr_series(ids: list[int]) -> int:
    return bulk_delete("bazarr_series", "sonarr_id", ids)


def bulk_delete(table: str, col: str, ids: list) -> int:
    if not ids:
        return 0
    conn = get_conn()
    total = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {col} IN ({placeholders})",
            chunk,
        )
        total += cur.rowcount or 0
    return total


def bazarr_movie_for_local_path(local_path: str) -> int | None:
    row = (
        get_conn()
        .execute(
            "SELECT radarr_id FROM bazarr_movie WHERE local_path = ? LIMIT 1",
            (local_path,),
        )
        .fetchone()
    )
    return row[0] if row else None


def bazarr_series_for_local_path(local_path: str) -> int | None:
    row = (
        get_conn()
        .execute(
            "SELECT sonarr_id FROM bazarr_series"
            " WHERE local_folder IS NOT NULL AND ? LIKE local_folder || '/%'"
            " ORDER BY LENGTH(local_folder) DESC LIMIT 1",
            (local_path,),
        )
        .fetchone()
    )
    return row[0] if row else None


# --- radarr ---------------------------------------------------------------


def upsert_radarr_movie(
    *,
    movie_id: int,
    updated_at: int,
    path_map: dict[str, str],
    tmdb_id: int | None = None,
    remote_path: str | None = None,
) -> None:
    local_path = resolve_local_file(remote_path, path_map)
    get_conn().execute(
        "INSERT OR REPLACE INTO radarr_movie"
        " (movie_id, tmdb_id, local_path, updated_at)"
        " VALUES (?,?,?,?)",
        (movie_id, tmdb_id, local_path, updated_at),
    )


def list_radarr_movie_ids() -> set[int]:
    return {row[0] for row in get_conn().execute("SELECT movie_id FROM radarr_movie")}


def delete_radarr_movies(ids: list[int]) -> int:
    return bulk_delete("radarr_movie", "movie_id", ids)


def radarr_tmdb_for_local_path(local_path: str) -> int | None:
    row = (
        get_conn()
        .execute(
            "SELECT tmdb_id FROM radarr_movie"
            " WHERE local_path = ? AND tmdb_id IS NOT NULL LIMIT 1",
            (local_path,),
        )
        .fetchone()
    )
    return row[0] if row else None


# --- sonarr ---------------------------------------------------------------


def upsert_sonarr_series(
    *,
    series_id: int,
    updated_at: int,
    path_map: dict[str, str],
    title_slug: str | None = None,
    remote_path: str | None = None,
) -> None:
    local_folder = resolve_local_folder(remote_path, path_map)
    get_conn().execute(
        "INSERT OR REPLACE INTO sonarr_series"
        " (series_id, title_slug, local_folder, updated_at)"
        " VALUES (?,?,?,?)",
        (series_id, title_slug, local_folder, updated_at),
    )


def list_sonarr_series_ids() -> set[int]:
    return {row[0] for row in get_conn().execute("SELECT series_id FROM sonarr_series")}


def delete_sonarr_series(ids: list[int]) -> int:
    return bulk_delete("sonarr_series", "series_id", ids)


def all_bazarr_movies_by_local_path() -> dict[str, int]:
    rows = (
        get_conn()
        .execute(
            "SELECT local_path, radarr_id FROM bazarr_movie"
            " WHERE local_path IS NOT NULL",
        )
        .fetchall()
    )
    return {r[0]: r[1] for r in rows}


def all_bazarr_series_by_local_folder() -> dict[str, int]:
    rows = (
        get_conn()
        .execute(
            "SELECT local_folder, sonarr_id FROM bazarr_series"
            " WHERE local_folder IS NOT NULL",
        )
        .fetchall()
    )
    return {r[0]: r[1] for r in rows}


def all_radarr_movies_by_local_path() -> dict[str, int]:
    rows = (
        get_conn()
        .execute(
            "SELECT local_path, tmdb_id FROM radarr_movie"
            " WHERE local_path IS NOT NULL AND tmdb_id IS NOT NULL",
        )
        .fetchall()
    )
    return {r[0]: r[1] for r in rows}


def all_sonarr_series_by_local_folder() -> dict[str, str]:
    rows = (
        get_conn()
        .execute(
            "SELECT local_folder, title_slug FROM sonarr_series"
            " WHERE local_folder IS NOT NULL AND title_slug IS NOT NULL",
        )
        .fetchall()
    )
    return {r[0]: r[1] for r in rows}


def sonarr_slug_for_local_path(local_path: str) -> str | None:
    row = (
        get_conn()
        .execute(
            "SELECT title_slug FROM sonarr_series"
            " WHERE local_folder IS NOT NULL AND ? LIKE local_folder || '/%'"
            "   AND title_slug IS NOT NULL"
            " ORDER BY LENGTH(local_folder) DESC LIMIT 1",
            (local_path,),
        )
        .fetchone()
    )
    return row[0] if row else None


# --- kv store -------------------------------------------------------------


def kv_get(key: str) -> str | None:
    row = get_conn().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def kv_set(key: str, value: str | None) -> None:
    if value is None:
        get_conn().execute("DELETE FROM kv WHERE key = ?", (key,))
    else:
        get_conn().execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (key, value),
        )
