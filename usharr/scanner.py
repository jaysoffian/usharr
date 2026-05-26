"""Media tree walker and per-pass probe workers."""

import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import NamedTuple

from usharr import db, mediainfo, sidecars
from usharr.ardetector import detect
from usharr.config import load_config

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".iso", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".webm"}
)


class ProbeRequest(NamedTuple):
    path: Path
    force: bool


# Per-pass queues drained by long-lived workers started in app.lifespan.
# Mediainfo and ardetector run concurrently — the cheap mediainfo pass
# isn't blocked behind the slow ardetector pass for the same file.
mediainfo_queue: asyncio.Queue[ProbeRequest] = asyncio.Queue()
ardetector_queue: asyncio.Queue[ProbeRequest] = asyncio.Queue()

# Coalesces full-library scan requests. Callers set the event (and may
# upgrade pending flags); scan_worker snapshots+clears them per run.
scan_event = asyncio.Event()
pending_reanalyze = False
pending_refresh = False

# Mutex for the walker itself, so scan_worker and scan_and_drain (the
# foreground entry used by reconcile_loop and the CLI) don't run two
# walks concurrently.
scan_lock = asyncio.Lock()


def request_scan(*, reanalyze: bool = False, refresh: bool = False) -> None:
    """Schedule a full-library scan. Coalesces with any pending request."""
    global pending_reanalyze, pending_refresh  # noqa: PLW0603
    if reanalyze:
        pending_reanalyze = True
    if refresh:
        pending_refresh = True
    scan_event.set()


def request_probe(
    path: Path,
    *,
    reanalyze: bool = False,
    refresh: bool = False,
) -> None:
    """Enqueue a single file for probing. Workers cache-check on dequeue."""
    mediainfo_queue.put_nowait(ProbeRequest(path, reanalyze or refresh))
    ardetector_queue.put_nowait(ProbeRequest(path, reanalyze))


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


async def walk_and_enqueue(*, reanalyze: bool, refresh: bool) -> None:
    """Reconcile DB with the media tree; enqueue per-pass work as needed.

    Maintains the ``media_file`` rows inline (stub-insert new, update
    stat on changed) and handles sidecar-only updates without involving
    the probe workers. Anything that needs ffmpeg / mediainfo lands in
    the per-pass queues.
    """
    paths = load_config().all_paths
    logger.info(
        "scan walk starting across %d path(s) reanalyze=%s refresh=%s",
        len(paths),
        reanalyze,
        refresh,
    )
    start = time.monotonic()

    disk_files = iter_video_files(paths)
    disk_paths = {str(p) for p in disk_files}

    removed = sorted(db.list_paths() - disk_paths)
    if removed:
        n = db.delete_paths(removed)
        logger.info("Removed %d stale row(s) from DB", n)

    now = int(time.time())
    stubs = 0
    mi_enqueued = 0
    ar_enqueued = 0
    sidecar_only = 0

    for p in disk_files:
        try:
            st = p.stat()
        except OSError as exc:
            logger.warning("stat failed for %s: %s", p, exc)
            continue

        sidecar_paths = sidecars.find_sidecars(p)
        sidecar_mtime = sidecars.mtime_ns_max(sidecar_paths)
        mf = db.get(str(p))
        mi_row = db.get_mediainfo(str(p)) if mf is not None else None
        ar_row = db.get_ardetector(str(p)) if mf is not None else None

        video_unchanged = (
            mf is not None
            and mf.size_bytes == st.st_size
            and mf.mtime_ns == st.st_mtime_ns
        )
        sidecars_unchanged = mf is not None and mf.sidecars_mtime_ns == sidecar_mtime

        if mf is None:
            db.insert_media_file(
                path=str(p),
                size_bytes=st.st_size,
                mtime_ns=st.st_mtime_ns,
                sidecars_mtime_ns=sidecar_mtime,
                discovered_at=now,
            )
            stubs += 1
        elif not video_unchanged or not sidecars_unchanged:
            db.update_media_file_stat(
                path=str(p),
                size_bytes=st.st_size,
                mtime_ns=st.st_mtime_ns,
                sidecars_mtime_ns=sidecar_mtime,
            )

        # Mediainfo is cheap: retry when its row is missing (e.g. after
        # a schema bump that DELETEd from `mediainfo`). Ardetector is
        # slow and deterministic — if it failed on these exact bytes,
        # it'll fail again, so only re-attempt when forced, when the
        # video changed, or when there's no row yet.
        do_mediainfo = reanalyze or refresh or not video_unchanged or mi_row is None
        do_ardetector = reanalyze or not video_unchanged or ar_row is None

        if do_mediainfo:
            mediainfo_queue.put_nowait(ProbeRequest(p, force=reanalyze or refresh))
            mi_enqueued += 1
        if do_ardetector:
            ardetector_queue.put_nowait(ProbeRequest(p, force=reanalyze))
            ar_enqueued += 1

        # Sidecar-only update: probes don't need to run, but external
        # subs do — re-derive inline.
        if not do_mediainfo and not do_ardetector and not sidecars_unchanged:
            update_external_subs(p, sidecar_paths)
            sidecar_only += 1

    logger.info(
        "scan walk complete: files=%d removed=%d stubs=%d "
        "mi_enqueued=%d ar_enqueued=%d sidecar_only=%d elapsed=%.1fs",
        len(disk_files),
        len(removed),
        stubs,
        mi_enqueued,
        ar_enqueued,
        sidecar_only,
        time.monotonic() - start,
    )


async def scan_worker() -> None:
    """Drain scan requests forever. Coalesces rapid re-triggers."""
    global pending_reanalyze, pending_refresh  # noqa: PLW0603
    while True:
        await scan_event.wait()
        scan_event.clear()
        reanalyze = pending_reanalyze
        refresh = pending_refresh
        pending_reanalyze = False
        pending_refresh = False
        try:
            async with scan_lock:
                await walk_and_enqueue(reanalyze=reanalyze, refresh=refresh)
        except Exception:
            logger.exception("scan walk errored")


async def scan_and_drain(*, reanalyze: bool = False, refresh: bool = False) -> None:
    """Walk + wait for queues to drain. For reconcile_loop and CLI."""
    async with scan_lock:
        await walk_and_enqueue(reanalyze=reanalyze, refresh=refresh)
    await mediainfo_queue.join()
    await ardetector_queue.join()


async def mediainfo_worker() -> None:
    """Drain the mediainfo queue forever. Cancel-safe."""
    while True:
        req = await mediainfo_queue.get()
        try:
            await mediainfo_pass(req.path, force=req.force)
        except Exception:
            logger.exception("mediainfo_pass failed for %s", req.path)
        finally:
            mediainfo_queue.task_done()


async def ardetector_worker() -> None:
    """Drain the ardetector queue forever. Cancel-safe."""
    while True:
        req = await ardetector_queue.get()
        try:
            await ardetector_pass(req.path, force=req.force)
        except Exception:
            logger.exception("ardetector_pass failed for %s", req.path)
        finally:
            ardetector_queue.task_done()


async def mediainfo_pass(path: Path, *, force: bool) -> None:
    """Probe mediainfo for ``path`` and re-derive external subs.

    Cache-hit (returns without probing) when ``force`` is false, the
    video bytes haven't changed, and a mediainfo row already exists.
    """
    try:
        st = path.stat()
    except OSError as exc:
        logger.warning("stat failed for %s: %s", path, exc)
        return

    mf = db.get(str(path))
    if mf is None:
        logger.warning("mediainfo_pass: no media_file row for %s", path)
        return

    video_unchanged = mf.size_bytes == st.st_size and mf.mtime_ns == st.st_mtime_ns
    mi_row = db.get_mediainfo(str(path))
    if not force and video_unchanged and mi_row is not None:
        return

    logger.info("mediainfo: %s", path)
    await run_mediainfo(path, mi_row, int(time.time()))
    update_external_subs(path, sidecars.find_sidecars(path))


async def ardetector_pass(path: Path, *, force: bool) -> None:
    """Run the AR sampler on ``path``.

    Cache-hit when ``force`` is false, the video bytes haven't changed,
    and an ardetector row already exists. We don't auto-retry persistent
    AR failures on the same bytes — the user has `Redetect` for that.
    """
    try:
        st = path.stat()
    except OSError as exc:
        logger.warning("stat failed for %s: %s", path, exc)
        return

    mf = db.get(str(path))
    if mf is None:
        logger.warning("ardetector_pass: no media_file row for %s", path)
        return

    video_unchanged = mf.size_bytes == st.st_size and mf.mtime_ns == st.st_mtime_ns
    ar_row = db.get_ardetector(str(path))
    if not force and video_unchanged and ar_row is not None:
        return

    logger.info("ardetector: %s", path)
    await run_ardetector(path, int(time.time()))


def update_external_subs(path: Path, sidecar_paths: list[Path]) -> None:
    """Re-derive external sub rows numbered after the current internal block."""
    internal_count = db.count_internal_subs(str(path))
    external_subs = [
        sidecars.parse_sidecar(path.stem, s, internal_count + i)
        for i, s in enumerate(sidecar_paths)
    ]
    db.update_external_subtitles(path=str(path), subtitles=external_subs)


async def run_mediainfo(
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


async def run_ardetector(path: Path, now: int) -> None:
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
    # (the AR sampler measures runtime as a side effect). No-op if the
    # mediainfo row doesn't exist yet — that pass will fill its own.
    if result.duration is not None:
        db.set_mediainfo_duration(str(path), result.duration)
