"""Media tree walker and hourly reconciliation loop."""

import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from usharr import db, mediainfo, sidecars
from usharr.ardetector import detect
from usharr.config import INTERVAL_SECONDS, Config

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".iso", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".webm"}
)

# At most one probe runs at a time. scan_loop and the webhook probe_worker
# both go through probe_and_store, which acquires this lock for the
# ffmpeg/mediainfo pass.
_probe_lock = asyncio.Lock()

# Serializes full_scan. Without this, scan_loop's periodic pass and a
# manual trigger (Refresh / Analyze) interleave at every await point in
# probe_and_store — files arrive out of order and each gets probed once
# per concurrent scan.
_scan_lock = asyncio.Lock()

# Bounded queue fed by webhook PUTs; drained by a single probe_worker task
# started in app.lifespan. Tuple: (path, force, force_mediainfo).
_QUEUE_MAX = 50
_queue: asyncio.Queue[tuple[Path, bool, bool]] = asyncio.Queue(maxsize=_QUEUE_MAX)


def is_scanned(path: Path) -> bool:
    """True if discovery + both probe passes are current for this file."""
    try:
        stat = path.stat()
    except OSError:
        return False
    mf = db.get(str(path))
    if mf is None:
        return False
    if mf.size_bytes != stat.st_size:
        return False
    if mf.mtime_ns != stat.st_mtime_ns:
        return False
    # Row presence in the per-pass tables means we attempted that pass
    # at least once (success or recorded failure). Either way, no need
    # to re-attempt.
    if db.get_mediainfo(str(path)) is None:
        return False
    if db.get_ardetector(str(path)) is None:
        return False
    current_sidecars = sidecars.find_sidecars(path)
    return mf.sidecars_mtime_ns == sidecars.mtime_ns_max(current_sidecars)


def enqueue_probe(
    path: Path,
    *,
    force: bool = False,
    force_mediainfo: bool = False,
) -> bool:
    """Non-blocking enqueue. Returns False if the queue is full."""
    try:
        _queue.put_nowait((path, force, force_mediainfo))
    except asyncio.QueueFull:
        return False
    return True


async def probe_worker(config: Config) -> None:
    """Drain the probe queue, serially. Cancel-safe."""
    while True:
        path, force, force_mediainfo = await _queue.get()
        try:
            await probe_and_store(
                config,
                path,
                force=force,
                force_mediainfo=force_mediainfo,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("queued probe failed for %s", path)
        finally:
            _queue.task_done()


def iter_video_files(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for root in paths:
        root_path = Path(root)
        if not root_path.is_dir():
            logger.warning("Scan path %s is missing or not a directory", root)
            continue
        found.extend(
            p
            for p in root_path.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        )
    return sorted(found, key=lambda p: str(p).lower())


async def probe_and_store(
    config: Config,
    path: Path,
    *,
    force: bool = False,
    force_mediainfo: bool = False,
) -> tuple[db.MediaFileRow | None, bool]:
    """Probe ``path`` (unless cached) and upsert. Returns (row, probed).

    ``force`` runs both ardetector and mediainfo regardless of cache state.
    ``force_mediainfo`` only forces the (cheap) mediainfo pass; cached
    ardetector output is preserved — useful when track metadata has
    changed upstream (e.g. tags edited) but AR is stable.
    """
    try:
        stat = path.stat()
    except OSError as exc:
        logger.warning("stat failed for %s: %s", path, exc)
        return None, False

    sidecar_paths = sidecars.find_sidecars(path)
    sidecar_mtime = sidecars.mtime_ns_max(sidecar_paths)

    mf = db.get(str(path))
    mi_row = db.get_mediainfo(str(path)) if mf is not None else None
    ar_row = db.get_ardetector(str(path)) if mf is not None else None
    video_unchanged = (
        mf is not None
        and mf.size_bytes == stat.st_size
        and mf.mtime_ns == stat.st_mtime_ns
    )
    sidecars_unchanged = mf is not None and mf.sidecars_mtime_ns == sidecar_mtime

    # Decide up front what each pass would actually do, then use those
    # decisions to pick the short-circuit path. Short-circuits must honor
    # every force flag, not just `force`.
    #
    # Mediainfo is cheap, so we retry if we don't have its row yet
    # (e.g. after a schema bump that DELETEd from `mediainfo`).
    # Ardetector is slow and deterministic — if it failed on this exact
    # file bytes, it'll fail again. Only attempt ardetector when forced,
    # when the video changed, or when there's no ardetector row yet
    # (covers both never-discovered and "stub" media_file rows from
    # full_scan's pre-pass). Users can `Redetect` per-title or
    # library-wide to retry persistent failures.
    run_mediainfo = force or force_mediainfo or not video_unchanged or mi_row is None
    run_ardetector = force or not video_unchanged or ar_row is None

    # Make sure media_file is current before we touch the per-pass tables
    # (the FKs require it). New file → insert; changed file → refresh
    # size/mtime so video_unchanged checks on later scans are accurate.
    now = int(time.time())
    if mf is None:
        db.insert_media_file(
            path=str(path),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sidecars_mtime_ns=sidecar_mtime,
            discovered_at=now,
        )
    elif not video_unchanged or not sidecars_unchanged:
        db.update_media_file_stat(
            path=str(path),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sidecars_mtime_ns=sidecar_mtime,
        )

    if not run_mediainfo and not run_ardetector and sidecars_unchanged:
        return db.get(str(path)), False

    if not run_mediainfo and not run_ardetector:
        start_idx = db.count_internal_subs(str(path))
        external_subs = [
            sidecars.parse_sidecar(path.stem, s, start_idx + i)
            for i, s in enumerate(sidecar_paths)
        ]
        db.update_external_subtitles(path=str(path), subtitles=external_subs)
        logger.info(
            "sidecar-only update for %s (%d external)",
            path,
            len(external_subs),
        )
        return db.get(str(path)), True

    logger.info(
        "probe %s: mediainfo=%s ardetector=%s",
        path,
        run_mediainfo,
        run_ardetector,
    )

    async with _probe_lock:
        if run_mediainfo:
            await _run_mediainfo(path, mi_row, now)
        if run_ardetector:
            await _run_ardetector(path, now)

    # External sidecar subs always re-derived from disk so a sidecar
    # add/remove between probe passes still gets picked up. Keep them
    # numbered after the internal track block (now refreshed if mediainfo
    # ran).
    internal_count = db.count_internal_subs(str(path))
    external_subs = [
        sidecars.parse_sidecar(path.stem, s, internal_count + i)
        for i, s in enumerate(sidecar_paths)
    ]
    db.update_external_subtitles(path=str(path), subtitles=external_subs)

    return db.get(str(path)), True


async def _run_mediainfo(
    path: Path,
    cached: db.MediainfoRow | None,
    now: int,
) -> None:
    """Run the mediainfo pass. On success, replace the row + audio +
    internal subs. On failure, record the error but preserve cached
    track metadata so the UI keeps showing what we had.
    """
    try:
        mi = await mediainfo.extract(path)
    except Exception as exc:
        logger.warning("mediainfo failed for %s: %s", path, exc)
        # Preserve cached video data if any — only update error/probed_at.
        db.upsert_mediainfo(
            path=str(path),
            probed_at=now,
            error=str(exc)[:500],
            container=cached.container if cached else None,
            duration=cached.duration if cached else None,
            video_codec=cached.video_codec if cached else None,
            video_profile=cached.video_profile if cached else None,
            video_width=cached.video_width if cached else None,
            video_height=cached.video_height if cached else None,
            video_bit_depth=cached.video_bit_depth if cached else None,
            video_hdr=cached.video_hdr if cached else None,
            video_hdr_format=cached.video_hdr_format if cached else None,
            video_frame_rate=cached.video_frame_rate if cached else None,
            video_bit_rate=cached.video_bit_rate if cached else None,
            video_max_bit_rate=cached.video_max_bit_rate if cached else None,
            audio=None,
            internal_subs=None,
        )
        return

    v = mi.video
    db.upsert_mediainfo(
        path=str(path),
        probed_at=now,
        error=None,
        container=mi.container,
        duration=mi.duration,
        video_codec=v.codec if v else None,
        video_profile=v.profile if v else None,
        video_width=(v.width or None) if v else None,
        video_height=(v.height or None) if v else None,
        video_bit_depth=v.bit_depth if v else None,
        video_hdr=v.hdr if v else None,
        video_hdr_format=v.hdr_format if v else None,
        video_frame_rate=v.frame_rate if v else None,
        video_bit_rate=v.bit_rate if v else None,
        video_max_bit_rate=v.max_bit_rate if v else None,
        audio=[mediainfo.to_audio_row(a) for a in mi.audio],
        internal_subs=[mediainfo.to_internal_sub_row(s) for s in mi.subtitle],
    )


async def _run_ardetector(path: Path, now: int) -> None:
    """Run ardetector and upsert its row. On failure, record the error;
    we don't try to preserve old aspect data (a re-run failure on the
    same bytes would be the same outcome the user already has).
    """
    try:
        result = await detect(path)
    except Exception as exc:
        logger.warning("ardetector failed for %s: %s", path, exc)
        db.upsert_ardetector(
            path=str(path),
            probed_at=now,
            error=str(exc)[:500],
            aspect_primary=None,
            aspect_widest=None,
            aspect_samples=None,
        )
        return

    db.upsert_ardetector(
        path=str(path),
        probed_at=now,
        error=None,
        aspect_primary=result.primary_aspect,
        aspect_widest=result.widest_aspect,
        aspect_samples=json.dumps([asdict(d) for d in result.detected]),
    )
    # Backfill duration onto mediainfo if mediainfo didn't get one
    # (the AR sampler measures runtime as a side effect).
    if result.duration is not None:
        db.set_mediainfo_duration(str(path), result.duration)


async def full_scan(
    config: Config,
    *,
    force: bool = False,
    force_mediainfo: bool = False,
) -> dict:
    """Reconcile DB with the media tree; probe new/changed files.

    ``force`` re-runs both tools on every file (slow — ardetector samples
    frames). ``force_mediainfo`` re-reads track metadata but preserves
    cached AR data (fast).
    """
    if _scan_lock.locked():
        logger.info("full_scan waiting for in-flight scan to finish")
    async with _scan_lock:
        paths = config.all_paths
        logger.info(
            "Starting full scan across %d path(s) force=%s force_mediainfo=%s",
            len(paths),
            force,
            force_mediainfo,
        )
        start = time.monotonic()
        disk_files = iter_video_files(paths)
        disk_paths = {str(p) for p in disk_files}

        removed = sorted(db.list_paths() - disk_paths)
        if removed:
            n = db.delete_paths(removed)
            logger.info("Removed %d stale row(s) from DB", n)

        # Stub-insert pass: surface every disk file in the library view
        # by name immediately, so a fresh / large library isn't blank
        # while the (slow) mediainfo + ardetector passes catch up. A
        # stub is just a `media_file` row with no companion `mediainfo`
        # / `ardetector` row; the loop below probes those in.
        now = int(time.time())
        stub_count = 0
        for p in disk_files:
            try:
                st = p.stat()
            except OSError as exc:
                logger.warning("stat failed for %s: %s", p, exc)
                continue
            sidecar_paths = sidecars.find_sidecars(p)
            if db.insert_media_file(
                path=str(p),
                size_bytes=st.st_size,
                mtime_ns=st.st_mtime_ns,
                sidecars_mtime_ns=sidecars.mtime_ns_max(sidecar_paths),
                discovered_at=now,
            ):
                stub_count += 1
        if stub_count:
            logger.info("Inserted %d stub row(s)", stub_count)

        probed_count = 0
        for p in disk_files:
            _, probed = await probe_and_store(
                config,
                p,
                force=force,
                force_mediainfo=force_mediainfo,
            )
            if probed:
                probed_count += 1

        summary = {
            "files": len(disk_files),
            "removed": len(removed),
            "stubs": stub_count,
            "probed": probed_count,
            "cached": len(disk_files) - probed_count,
            "elapsed_seconds": round(time.monotonic() - start, 1),
        }
        logger.info("Full scan complete: %s", summary)
        return summary


async def scan_loop(config: Config) -> None:
    """Run full_scan forever, sleeping between passes. Cancel-safe."""
    while True:
        try:
            await full_scan(config)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Full scan errored")
        try:
            await asyncio.sleep(INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
