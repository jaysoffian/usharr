"""SQLite-backed store: media files, tracks, Plex items, and kv.

Read accessors return frozen-slots dataclasses (``VideoFileRow``,
``MediainfoRow``, ``ArdetectorRow``, ``AudioTrackRow``,
``SubtitleTrackRow``, ``PlexItemRow``, ``LibraryRow``).
View-layer code that needs to add template fields should
``dataclasses.asdict(row)`` first and work in dict-space from there.

Schema overview:
  * ``video_file`` is the discovery row — path/size/mtime.
    A row exists iff we've seen the file on disk.
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


# Shared so CREATE_TABLES (fresh DB) and the migration rebuild can't diverge.
CREATE_SUBTITLE_TRACK = """
CREATE TABLE IF NOT EXISTS subtitle_track (
    video_path    TEXT NOT NULL REFERENCES video_file(path) ON DELETE CASCADE,
    subtitle_path TEXT REFERENCES subtitle_file(path) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,
    codec         TEXT,
    language      TEXT,
    title         TEXT,
    is_default    INTEGER NOT NULL DEFAULT 0,
    is_forced     INTEGER NOT NULL DEFAULT 0,
    is_sdh        INTEGER NOT NULL DEFAULT 0
);
"""

CREATE_TABLES = (
    """
CREATE TABLE IF NOT EXISTS video_file (
    path              TEXT PRIMARY KEY,
    size_bytes        INTEGER NOT NULL,
    mtime_ns          INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS subtitle_file (
    path       TEXT PRIMARY KEY,
    video_path TEXT NOT NULL REFERENCES video_file(path) ON DELETE CASCADE,
    size_bytes INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL
) WITHOUT ROWID;
"""
    + CREATE_SUBTITLE_TRACK
    + """
CREATE TABLE IF NOT EXISTS mediainfo (
    video_path         TEXT PRIMARY KEY REFERENCES video_file(path) ON DELETE CASCADE,
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
    video_path     TEXT PRIMARY KEY REFERENCES video_file(path) ON DELETE CASCADE,
    error          TEXT,
    aspect_primary REAL,
    aspect_widest  REAL,
    aspect_samples TEXT,  -- JSON list of {aspect, percentage} samples
    color_pct      REAL   -- 0.0=monochrome, 1.0=color; NULL=unknown
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS audio_track (
    video_path       TEXT NOT NULL REFERENCES video_file(path) ON DELETE CASCADE,
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
    PRIMARY KEY (video_path, idx)
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
    local_path     TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bazarr_movie (
    radarr_id    INTEGER PRIMARY KEY,
    local_path   TEXT              -- remote movie file path mapped via path_map
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bazarr_series (
    sonarr_id    INTEGER PRIMARY KEY,
    local_folder TEXT              -- remote show folder mapped via path_map
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS radarr_movie (
    movie_id   INTEGER PRIMARY KEY,  -- Radarr's internal id
    tmdb_id    INTEGER,               -- for /movie/{tmdbId} deep-links
    local_path TEXT                   -- remote movie file path mapped via path_map
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sonarr_series (
    series_id    INTEGER PRIMARY KEY, -- Sonarr's internal id
    title_slug   TEXT,                -- for /series/{slug} deep-links
    local_folder TEXT                 -- remote series folder mapped via path_map
) WITHOUT ROWID;
"""
)

CREATE_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS subtitle_track_internal ON subtitle_track(video_path, idx)    WHERE subtitle_path IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS subtitle_track_external ON subtitle_track(subtitle_path, idx) WHERE subtitle_path IS NOT NULL;
CREATE INDEX        IF NOT EXISTS subtitle_track_video_path ON subtitle_track(video_path);
CREATE INDEX        IF NOT EXISTS subtitle_file_video_path  ON subtitle_file(video_path);
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
class VideoFileRow:
    path: str
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class MediainfoRow:
    video_path: str
    error: str | None = None
    container: str | None = None
    duration: float | None = None
    video_codec: str | None = None
    video_profile: str | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_bit_depth: int | None = None
    video_hdr: str | None = None
    video_hdr_format: str | None = None
    video_frame_rate: float | None = None
    video_bit_rate: int | None = None
    video_max_bit_rate: int | None = None


@dataclass(frozen=True, slots=True)
class ArdetectorRow:
    video_path: str
    error: str | None = None
    aspect_primary: float | None = None
    aspect_widest: float | None = None
    aspect_samples: str | None = None
    color_pct: float | None = None


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
    subtitle_path: str | None
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


@dataclass(frozen=True, slots=True)
class LibraryRow:
    # video_file fields (mirrors VideoFileRow)
    path: str
    size_bytes: int
    mtime_ns: int
    # mediainfo fields (NULL when no mediainfo row yet — i.e. stub).
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
    ardetector_error: str | None
    aspect_primary: float | None
    aspect_widest: float | None
    aspect_samples: str | None
    color_pct: float | None
    # plex_item fields aliased with a plex_ prefix; nullable via LEFT JOIN.
    plex_rating_key: str | None
    plex_type: str | None
    plex_title: str | None
    plex_year: int | None
    plex_show_title: str | None
    plex_season_number: int | None
    plex_episode_number: int | None


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
    # PRE-schema migrations: must run before CREATE_TABLES so the
    # IF NOT EXISTS statements don't shadow the tables being migrated.
    maybe_rename_media_file_to_video_file()
    db.execute(
        "CREATE TABLE IF NOT EXISTS subtitle_file ("
        " path TEXT PRIMARY KEY,"
        " video_path TEXT NOT NULL REFERENCES video_file(path) ON DELETE CASCADE,"
        " size_bytes INTEGER NOT NULL,"
        " mtime_ns INTEGER NOT NULL"
        ") WITHOUT ROWID"
    )
    maybe_rebuild_subtitle_track()
    db.executescript(CREATE_TABLES)
    db.executescript(CREATE_INDEXES)
    maybe_drop_discovered_at()
    maybe_drop_timestamp_columns()
    maybe_add_ardetector_color_pct()
    maybe_rename_path_to_video_path()
    maybe_reprobe_on_schema_bump()
    logger.info("Opened DB at %s", DB_PATH)


def maybe_drop_discovered_at() -> None:
    """One-shot drop of video_file.discovered_at. No-op on fresh DBs."""
    conn = get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(video_file)").fetchall()}
    if "discovered_at" not in cols:
        return
    conn.execute("ALTER TABLE video_file DROP COLUMN discovered_at")
    logger.info("Dropped video_file.discovered_at")


def maybe_drop_timestamp_columns() -> None:
    """One-shot drop of unused probed_at / updated_at columns."""
    conn = get_conn()
    drops = (
        ("mediainfo", "probed_at"),
        ("ardetector", "probed_at"),
        ("plex_item", "updated_at"),
        ("bazarr_movie", "updated_at"),
        ("bazarr_series", "updated_at"),
        ("radarr_movie", "updated_at"),
        ("sonarr_series", "updated_at"),
    )
    for table, col in drops:
        present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col in present:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
            logger.info("Dropped %s.%s", table, col)


def maybe_add_ardetector_color_pct() -> None:
    """One-shot add of ardetector.color_pct. Existing rows get NULL; users
    re-trigger detection per file via the Analyze action to populate it.
    No-op on fresh DBs (CREATE TABLE already declares the column) and on
    DBs that have already been migrated.
    """
    conn = get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ardetector)").fetchall()}
    if "color_pct" in cols:
        return
    conn.execute("ALTER TABLE ardetector ADD COLUMN color_pct REAL")
    logger.info("Added ardetector.color_pct")


def maybe_rename_path_to_video_path() -> None:
    """One-shot rename of the video FK column `path` -> `video_path` on the
    child tables. `video_file`/`subtitle_file` keep `path` (their own
    identity). No-op on fresh DBs and already-migrated DBs.
    """
    conn = get_conn()
    for table in ("mediainfo", "ardetector", "audio_track"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "path" not in cols or "video_path" in cols:
            continue
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN path TO video_path")
        logger.info("Renamed %s.path to video_path", table)


def maybe_rename_media_file_to_video_file() -> None:
    """PRE-schema: rename media_file -> video_file and drop the obsolete
    subtitles_mtime_ns column. SQLite auto-rewrites the child FK references
    on rename (legacy_alter_table is off by default). No-op on fresh DBs
    (no media_file) and on already-migrated DBs (video_file exists).
    """
    conn = get_conn()
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "video_file" in tables or "media_file" not in tables:
        return
    conn.execute("ALTER TABLE media_file RENAME TO video_file")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(video_file)").fetchall()}
    if "subtitles_mtime_ns" in cols:
        conn.execute("ALTER TABLE video_file DROP COLUMN subtitles_mtime_ns")
    logger.info("Renamed media_file to video_file and dropped subtitles_mtime_ns")


def maybe_rebuild_subtitle_track() -> None:
    """PRE-schema: rebuild the old (path, source, idx) subtitle_track into
    the new (video_path, subtitle_path, idx) shape. Migrates only internal
    rows (subtitle_path NULL); external rows are dropped and rebuilt from
    disk on the next scan. No-op on fresh DBs (no table) and on
    already-migrated DBs (no `source` column).
    """
    conn = get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(subtitle_track)").fetchall()}
    if not cols:
        return
    if "source" not in cols:
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE subtitle_track RENAME TO subtitle_track_old")
        conn.execute(CREATE_SUBTITLE_TRACK)
        conn.execute(
            "INSERT INTO subtitle_track"
            " (video_path, subtitle_path, idx, codec, language, title,"
            "  is_default, is_forced, is_sdh)"
            " SELECT path, NULL, idx, codec, language, title,"
            "  is_default, is_forced, is_sdh"
            " FROM subtitle_track_old WHERE source = 'internal'"
        )
        conn.execute("DROP TABLE subtitle_track_old")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("PRAGMA foreign_keys=ON")
    logger.info("Rebuilt subtitle_track keyed by (video_path, subtitle_path, idx)")


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
        .execute("SELECT 1 FROM video_file WHERE path = ? LIMIT 1", (p,))
        .fetchone()
        is not None
    )


def folder_has_files(p: str) -> bool:
    like = like_prefix(p.rstrip("/") + "/")
    return (
        get_conn()
        .execute(
            "SELECT 1 FROM video_file WHERE path LIKE ? ESCAPE '\\' LIMIT 1",
            (like,),
        )
        .fetchone()
        is not None
    )


def resolve_local_file(remote_path: str | None, path_map: dict[str, str]) -> str | None:
    """Map a remote file path to local; None if it doesn't match a video_file row."""
    if not remote_path:
        return None
    mapped = map_remote_path(remote_path, path_map)
    return mapped if file_exists(mapped) else None


def resolve_local_folder(
    remote_path: str | None, path_map: dict[str, str]
) -> str | None:
    """Map a remote folder path to local; None if no video_file row sits under it."""
    if not remote_path:
        return None
    mapped = map_remote_path(remote_path, path_map)
    return mapped if folder_has_files(mapped) else None


# --- video_file -----------------------------------------------------------

VIDEO_COLS = (
    "path",
    "size_bytes",
    "mtime_ns",
)


def get(path: Path | str) -> VideoFileRow | None:
    cols = ", ".join(VIDEO_COLS)
    row = (
        get_conn()
        .execute(f"SELECT {cols} FROM video_file WHERE path = ?", (str(path),))
        .fetchone()
    )
    if row is None:
        return None
    return make_row(VideoFileRow, VIDEO_COLS, row)


def get_by_remote_path(remote: str, path_map: dict[str, str]) -> VideoFileRow | None:
    """Map a remote path to local and return the matching video_file row."""
    return get(map_remote_path(remote, path_map))


def upsert_video_file(
    *,
    path: Path,
    size_bytes: int,
    mtime_ns: int,
) -> None:
    """Insert a video_file row, or refresh stat fields if it already exists."""
    get_conn().execute(
        "INSERT INTO video_file"
        " (path, size_bytes, mtime_ns)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(path) DO UPDATE SET"
        " size_bytes = excluded.size_bytes,"
        " mtime_ns = excluded.mtime_ns",
        (str(path), size_bytes, mtime_ns),
    )


# --- mediainfo ------------------------------------------------------------

MEDIAINFO_COLS = (
    "video_path",
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
        .execute(f"SELECT {cols} FROM mediainfo WHERE video_path = ?", (str(path),))
        .fetchone()
    )
    if row is None:
        return None
    return make_row(MediainfoRow, MEDIAINFO_COLS, row)


def upsert_mediainfo(
    row: MediainfoRow,
    *,
    audio: Iterable[AudioTrackRow],
    internal_subs: Iterable[SubtitleTrackRow],
) -> None:
    """Upsert the mediainfo row and replace the file's audio + internal
    subtitle tracks. External subtitles aren't touched (they live or die
    with the subtitle files — see ``replace_external_subtitles``).
    """
    path_str = row.video_path
    placeholders = ", ".join("?" * len(MEDIAINFO_COLS))
    cols = ", ".join(MEDIAINFO_COLS)
    values = tuple(getattr(row, c) for c in MEDIAINFO_COLS)
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO mediainfo ({cols}) VALUES ({placeholders})",
            values,
        )
        conn.execute("DELETE FROM audio_track WHERE video_path = ?", (path_str,))
        for t in audio:
            conn.execute(
                "INSERT INTO audio_track"
                " (video_path, idx, codec, channels, layout, language, title,"
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
        conn.execute(
            "DELETE FROM subtitle_track WHERE video_path = ? AND subtitle_path IS NULL",
            (path_str,),
        )
        for t in internal_subs:
            conn.execute(
                "INSERT INTO subtitle_track"
                " (video_path, subtitle_path, idx, codec, language, title,"
                "  is_default, is_forced, is_sdh)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    path_str,
                    t.subtitle_path,
                    t.idx,
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


def delete_mediainfo(path: Path) -> None:
    get_conn().execute("DELETE FROM mediainfo WHERE video_path = ?", (str(path),))


def set_mediainfo_error(path: Path, error: str) -> None:
    """Record an error on the mediainfo row, preserving any cached track
    metadata. Inserts a near-blank row if none exists.
    """
    get_conn().execute(
        "INSERT INTO mediainfo (video_path, error) VALUES (?, ?)"
        " ON CONFLICT(video_path) DO UPDATE SET error = excluded.error",
        (str(path), error),
    )


def set_mediainfo_duration(path: Path, duration: float) -> None:
    """Backfill mediainfo.duration from the ardetector pass when mediainfo
    didn't get one (the AR sampler measures runtime as a side effect).
    No-op if no mediainfo row exists for `path`.
    """
    get_conn().execute(
        "UPDATE mediainfo SET duration = ? WHERE video_path = ? AND duration IS NULL",
        (duration, str(path)),
    )


# --- ardetector -----------------------------------------------------------

ARDETECTOR_COLS = (
    "video_path",
    "error",
    "aspect_primary",
    "aspect_widest",
    "aspect_samples",
    "color_pct",
)


def get_ardetector(path: Path | str) -> ArdetectorRow | None:
    cols = ", ".join(ARDETECTOR_COLS)
    row = (
        get_conn()
        .execute(f"SELECT {cols} FROM ardetector WHERE video_path = ?", (str(path),))
        .fetchone()
    )
    if row is None:
        return None
    return make_row(ArdetectorRow, ARDETECTOR_COLS, row)


def delete_ardetector(path: Path) -> None:
    get_conn().execute("DELETE FROM ardetector WHERE video_path = ?", (str(path),))


def upsert_ardetector(row: ArdetectorRow) -> None:
    cols = ", ".join(ARDETECTOR_COLS)
    placeholders = ", ".join("?" * len(ARDETECTOR_COLS))
    values = tuple(getattr(row, c) for c in ARDETECTOR_COLS)
    get_conn().execute(
        f"INSERT OR REPLACE INTO ardetector ({cols}) VALUES ({placeholders})",
        values,
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
    "subtitle_path",
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
            f"SELECT {cols} FROM audio_track WHERE video_path = ? ORDER BY idx",
            (path,),
        )
        .fetchall()
    )
    return [
        make_row(AudioTrackRow, AUDIO_COLS, r, bool_fields=AUDIO_BOOLS) for r in rows
    ]


def get_subtitle_tracks(path: str) -> list[SubtitleTrackRow]:
    """Internal subs (by their own idx) first, then externals (by file/idx)."""
    cols = ", ".join(SUBTITLE_COLS)
    rows = (
        get_conn()
        .execute(
            f"SELECT {cols} FROM subtitle_track WHERE video_path = ?"
            " ORDER BY subtitle_path IS NOT NULL, subtitle_path, idx",
            (path,),
        )
        .fetchall()
    )
    return [
        make_row(SubtitleTrackRow, SUBTITLE_COLS, r, bool_fields=SUBTITLE_BOOLS)
        for r in rows
    ]


def subtitle_files_for(video_path: Path | str) -> dict[str, tuple[int, int]]:
    """Return {subtitle_path: (size_bytes, mtime_ns)} for a video's sidecars."""
    rows = (
        get_conn()
        .execute(
            "SELECT path, size_bytes, mtime_ns FROM subtitle_file WHERE video_path = ?",
            (str(video_path),),
        )
        .fetchall()
    )
    return {r[0]: (r[1], r[2]) for r in rows}


def replace_external_subtitles(
    *,
    video_path: Path,
    files: Iterable[tuple[str, int, int, list[SubtitleTrackRow]]],
) -> None:
    """Replace every external sidecar (and its tracks) for one video.

    Deleting the subtitle_file rows cascades the external subtitle_track
    rows via the subtitle_path FK; internal rows (NULL subtitle_path) are
    untouched.
    """
    video_str = str(video_path)
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM subtitle_file WHERE video_path = ?", (video_str,))
        for sub_path, size_bytes, mtime_ns, tracks in files:
            conn.execute(
                "INSERT INTO subtitle_file (path, video_path, size_bytes, mtime_ns)"
                " VALUES (?, ?, ?, ?)",
                (sub_path, video_str, size_bytes, mtime_ns),
            )
            for t in tracks:
                conn.execute(
                    "INSERT INTO subtitle_track"
                    " (video_path, subtitle_path, idx, codec, language, title,"
                    "  is_default, is_forced, is_sdh)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        video_str,
                        sub_path,
                        t.idx,
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
    return {row[0] for row in get_conn().execute("SELECT path FROM video_file")}


def library_rows(path_prefix: str) -> list[LibraryRow]:
    """video_file LEFT JOIN mediainfo / ardetector / plex_item.

    A NULL `mediainfo_error` (and the associated columns) means the file
    is a stub — discovered but not yet probed for track metadata. Same
    for `ardetector_error`. Audio and subtitle tracks are NOT included
    here; fetch via ``get_audio_tracks(path)`` /
    ``get_subtitle_tracks(path)`` as needed.
    """
    media_cols = ", ".join(f"m.{c}" for c in VIDEO_COLS)
    # mediainfo: skip the duplicate `video_path` column; alias `error` so it
    # doesn't collide with the ardetector version.
    mi_data = tuple(c for c in MEDIAINFO_COLS if c != "video_path")
    mi_select = ", ".join(
        f"mi.{c} AS mediainfo_{c}" if c == "error" else f"mi.{c}" for c in mi_data
    )
    ar_data = tuple(c for c in ARDETECTOR_COLS if c != "video_path")
    ar_select = ", ".join(
        f"ar.{c} AS ardetector_{c}" if c == "error" else f"ar.{c}" for c in ar_data
    )
    plex_cols = ", ".join(
        f"p.{c} AS plex_{c}" for c in PLEX_ITEM_COLS if c not in {"path", "local_path"}
    )
    rows = (
        get_conn()
        .execute(
            f"SELECT {media_cols}, {mi_select}, {ar_select}, {plex_cols}"
            " FROM video_file m"
            " LEFT JOIN mediainfo  mi ON mi.video_path = m.path"
            " LEFT JOIN ardetector ar ON ar.video_path = m.path"
            " LEFT JOIN plex_item   p ON p.local_path = m.path"
            " WHERE m.path LIKE ? ESCAPE '\\'"
            " ORDER BY m.path",
            (like_prefix(path_prefix),),
        )
        .fetchall()
    )
    mi_names = tuple(f"mediainfo_{c}" if c == "error" else c for c in mi_data)
    ar_names = tuple(f"ardetector_{c}" if c == "error" else c for c in ar_data)
    plex_names = tuple(
        f"plex_{c}" for c in PLEX_ITEM_COLS if c not in {"path", "local_path"}
    )
    col_names = (*VIDEO_COLS, *mi_names, *ar_names, *plex_names)
    return [make_row(LibraryRow, col_names, r) for r in rows]


def like_prefix(p: str) -> str:
    return p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def library_paths(path_prefix: str) -> list[str]:
    """Every video_file.path under the library, sorted. Caller may filter."""
    like = like_prefix(path_prefix)
    rows = (
        get_conn()
        .execute(
            "SELECT path FROM video_file WHERE path LIKE ? ESCAPE '\\' ORDER BY path",
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
        f"SELECT video_path, {audio_cols}"
        " FROM audio_track WHERE video_path LIKE ? ESCAPE '\\'"
        " ORDER BY video_path, idx",
        (like,),
    ):
        audio.setdefault(row[0], []).append(
            make_row(AudioTrackRow, AUDIO_COLS, row[1:], bool_fields=AUDIO_BOOLS)
        )
    subtitle_cols = ", ".join(SUBTITLE_COLS)
    for row in conn.execute(
        f"SELECT video_path, {subtitle_cols}"
        " FROM subtitle_track WHERE video_path LIKE ? ESCAPE '\\'"
        " ORDER BY video_path, subtitle_path IS NOT NULL, subtitle_path, idx",
        (like,),
    ):
        subtitle.setdefault(row[0], []).append(
            make_row(
                SubtitleTrackRow, SUBTITLE_COLS, row[1:], bool_fields=SUBTITLE_BOOLS
            )
        )
    return audio, subtitle


def delete_orphans(present: Iterable[Path | str]) -> None:
    """Delete video_file rows whose path is not in `present`."""
    orphans = sorted(list_paths() - {str(p) for p in present})
    if not orphans:
        return
    conn = get_conn()
    total = 0
    for i in range(0, len(orphans), 500):
        chunk = orphans[i : i + 500]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"DELETE FROM video_file WHERE path IN ({placeholders})",
            chunk,
        )
        total += cur.rowcount or 0
    logger.info("Removed %d stale row(s) from DB", total)


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
)


def upsert_plex_item(
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
    path_map: dict[str, str],
    remote_path: str | None = None,
) -> None:
    local_path = resolve_local_file(remote_path, path_map)
    get_conn().execute(
        "INSERT OR REPLACE INTO bazarr_movie (radarr_id, local_path) VALUES (?,?)",
        (radarr_id, local_path),
    )


def upsert_bazarr_series(
    *,
    sonarr_id: int,
    path_map: dict[str, str],
    remote_path: str | None = None,
) -> None:
    local_folder = resolve_local_folder(remote_path, path_map)
    get_conn().execute(
        "INSERT OR REPLACE INTO bazarr_series (sonarr_id, local_folder) VALUES (?,?)",
        (sonarr_id, local_folder),
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
    path_map: dict[str, str],
    tmdb_id: int | None = None,
    remote_path: str | None = None,
) -> None:
    local_path = resolve_local_file(remote_path, path_map)
    get_conn().execute(
        "INSERT OR REPLACE INTO radarr_movie"
        " (movie_id, tmdb_id, local_path)"
        " VALUES (?,?,?)",
        (movie_id, tmdb_id, local_path),
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
    path_map: dict[str, str],
    title_slug: str | None = None,
    remote_path: str | None = None,
) -> None:
    local_folder = resolve_local_folder(remote_path, path_map)
    get_conn().execute(
        "INSERT OR REPLACE INTO sonarr_series"
        " (series_id, title_slug, local_folder)"
        " VALUES (?,?,?)",
        (series_id, title_slug, local_folder),
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
