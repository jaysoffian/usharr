"""Media tree walker. Owns the per-pass probers it feeds."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from usharr import format as fmt
from usharr import plex_sync, queries, radarr_sync, sonarr_sync, subtitles
from usharr.config import get_config
from usharr.probers import ArdetectorProber, MediainfoProber

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".iso", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".webm"}
)

# How often scan_forever wakes up between scan + sync passes.
INTERVAL_SECONDS = 3600


class VideoEntry(NamedTuple):
    """One video file with its stat and its external subtitle sidecars."""

    path: Path
    size_bytes: int
    mtime_ns: int
    sidecars: list[subtitles.Sidecar]


def find_video_files(roots: Iterable[str]) -> list[VideoEntry]:
    """Walk every scan root, collecting each video's stat and its sidecars.

    Blocking: call it from a worker thread. Videos are ordered naturally by
    their path relative to their root, roots in the order given.
    """
    found: list[VideoEntry] = []
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            logger.warning("scan root %s is missing or not a directory", root)
            continue
        entries: list[VideoEntry] = []
        for dirpath, _dirnames, filenames in root_path.walk():
            for name in filenames:
                if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                path = dirpath / name
                try:
                    st = path.stat()
                except OSError as exc:
                    logger.warning("stat failed for %s: %s", path, exc)
                    continue
                sidecars: list[subtitles.Sidecar] = []
                for sub_name in subtitles.sidecar_names(name, filenames):
                    sub_path = dirpath / sub_name
                    try:
                        sub_st = sub_path.stat()
                    except OSError:
                        continue
                    sidecars.append(
                        subtitles.Sidecar(sub_path, sub_st.st_size, sub_st.st_mtime_ns)
                    )
                entries.append(VideoEntry(path, st.st_size, st.st_mtime_ns, sidecars))
        entries.sort(
            key=lambda e: fmt.natural_sort_key(str(e.path.relative_to(root_path)))
        )
        found.extend(entries)
    return found


class ScanRequest(NamedTuple):
    path: Path | None = None
    refresh: bool = False
    analyze: bool = False

    @property
    def force_refresh(self) -> bool:
        return self.refresh or self.analyze

    @property
    def force_detect(self) -> bool:
        return self.analyze


class Scanner:
    """Library Scanner"""

    def __init__(self) -> None:
        self.mediainfo = MediainfoProber()
        self.ardetector = ArdetectorProber()
        self.queue: asyncio.Queue[ScanRequest] = asyncio.Queue()
        self.tasks: tuple[asyncio.Task, ...] = ()

    async def enqueue(
        self,
        /,
        req: ScanRequest,
    ) -> None:
        """Add ScanRequest to queue."""
        if req.path is None:
            self.queue.put_nowait(req)
            return
        if req.force_refresh:
            await queries.delete_mediainfo(req.path)
            self.mediainfo.enqueue(req.path, priority=-time.monotonic())
        if req.force_detect:
            await queries.delete_ardetector(req.path)
            self.ardetector.enqueue(req.path, priority=-time.monotonic())

    def start(self) -> None:
        """Spawn every long-lived worker. Call once from lifespan / CLI."""
        self.tasks = (
            asyncio.create_task(self.mediainfo.process_queue_forever()),
            asyncio.create_task(self.ardetector.process_queue_forever()),
            asyncio.create_task(self.process_queue_forever()),
            asyncio.create_task(self.scan_forever()),
        )

    async def stop(self) -> None:
        """Cancel every worker task and wait for it to exit."""
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self.tasks = ()

    async def scan_forever(self) -> None:
        """Periodic full reconcile: scan, then refresh Plex + *arr metadata."""
        while True:
            await self.enqueue(ScanRequest())
            await self.queue.join()
            await asyncio.sleep(INTERVAL_SECONDS)

    async def process_queue_forever(self) -> None:
        """Drain scan requests forever. Coalesces rapid re-triggers."""
        while True:
            req = await self.queue.get()
            # Coalesce queued requests to a single scan
            while not self.queue.empty():
                next_req = self.queue.get_nowait()
                req = ScanRequest(
                    analyze=req.analyze or next_req.analyze,
                    refresh=req.refresh or next_req.refresh,
                    # req.path is always None
                )
                self.queue.task_done()
            try:
                await self.scan(req)
                await self.sync()
            except Exception as exc:
                logger.exception("Exception while processing queue: %s", str(exc))
            finally:
                self.queue.task_done()


    async def scan(self, /, req: ScanRequest) -> None:
        """Scan for new/updated media files and/or refresh/analyze existing files."""
        logger.info("scan started refresh=%s analyze=%s", req.refresh, req.analyze)
        start = time.monotonic()

        videos = await asyncio.to_thread(find_video_files, get_config().all_paths)
        await queries.delete_orphans(v.path for v in videos)

        # Preload the per-file read side in one query each, so an unchanged
        # file costs no round trip at all.
        recorded = await queries.video_file_stats()
        mediainfo_paths = await queries.mediainfo_paths()
        ardetector_paths = await queries.ardetector_paths()
        sidecars_by_video = await queries.subtitle_files_by_video()

        for video in videos:
            path = video.path
            key = str(path)
            changed = recorded.get(key) != (video.size_bytes, video.mtime_ns)

            if changed:
                await queries.upsert_video_file(
                    path=path,
                    size_bytes=video.size_bytes,
                    mtime_ns=video.mtime_ns,
                )

            if changed or req.force_refresh:
                await queries.delete_mediainfo(path)
            if changed or req.force_detect:
                await queries.delete_ardetector(path)

            if changed or req.force_refresh or key not in mediainfo_paths:
                self.mediainfo.enqueue(path)
            if changed or req.force_detect or key not in ardetector_paths:
                self.ardetector.enqueue(path)

            # Reconcile sidecars from disk every scan so adds/deletes/edits
            # are caught; cheap when nothing changed (compare only).
            # Guard so one bad sidecar can't abort the whole scan pass.
            try:
                await subtitles.sync_external_subs(
                    path, video.sidecars, sidecars_by_video.get(key, {})
                )
            except Exception as exc:
                logger.warning("subtitle sync failed for %s: %s", path, exc)

        logger.info("scan completed in %.1fs", time.monotonic() - start)

    async def sync(self) -> None:
        logger.info("sync started")
        start = time.monotonic()
        await asyncio.gather(
            plex_sync.sync(),
            radarr_sync.sync(),
            sonarr_sync.sync(),
        )
        logger.info("sync completed in %.1fs", time.monotonic() - start)


scanner = Scanner()
