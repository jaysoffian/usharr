"""Media tree walker. Owns the per-pass probers it feeds."""

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import NamedTuple

from usharr import bazarr_sync, db, plex_sync, radarr_sync, sonarr_sync, subtitles
from usharr.config import get_config
from usharr.probers import ArdetectorProber, MediainfoProber

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".iso", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".webm"}
)

# How often scan_forever wakes up between scan + sync passes.
INTERVAL_SECONDS = 3600


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

    def enqueue(
        self,
        /,
        req: ScanRequest,
    ) -> None:
        """Add ScanRequest to queue."""
        if req.path:
            self.mediainfo.enqueue(req.path, force=req.force_refresh)
            self.ardetector.enqueue(req.path, force=req.force_detect)
        else:
            self.queue.put_nowait(req)

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
            self.enqueue(ScanRequest())
            await self.queue.join()
            await asyncio.gather(
                plex_sync.sync(),
                bazarr_sync.sync(),
                radarr_sync.sync(),
                sonarr_sync.sync(),
            )
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
            except Exception:
                logger.exception("scan walk errored")
            finally:
                self.queue.task_done()

    @staticmethod
    def find_video_files() -> list[Path]:
        found: list[Path] = []
        for root in get_config().all_paths:
            root_path = Path(root)
            if not root_path.is_dir():
                logger.warning("Scan root %s is missing or not a directory", root)
                continue
            found.extend(
                p
                for p in root_path.rglob("*")
                if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
            )
        return sorted(found, key=lambda p: str(p).lower())

    async def scan(self, /, req: ScanRequest) -> None:
        """Scan for new/updated media files and/or refresh/analyze existing files."""
        logger.info(
            "scan walk starting refresh=%s analyze=%s", req.refresh, req.analyze
        )
        start = time.monotonic()

        videos = self.find_video_files()
        db.delete_orphans(videos)

        for path in videos:
            try:
                st = path.stat()
            except OSError as exc:
                logger.warning("stat failed for %s: %s", path, exc)
                continue

            subtitle_paths = subtitles.find_subtitles(path)
            subtitles_mtime = subtitles.mtime_ns_max(subtitle_paths)
            if mf := db.get(path):
                changed = mf.size_bytes != st.st_size or mf.mtime_ns != st.st_mtime_ns
                subtitles_changed = mf.subtitles_mtime_ns != subtitles_mtime
            else:
                changed = subtitles_changed = True

            if changed or subtitles_changed:
                db.upsert_media_file(
                    path=path,
                    size_bytes=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    subtitles_mtime_ns=subtitles_mtime,
                )

            # Probers skip unchanged files on their own
            self.mediainfo.enqueue(path, force=req.force_refresh)
            self.ardetector.enqueue(path, force=req.force_detect)

            if subtitles_changed:
                subtitles.update_external_subs(path, subtitle_paths)

        logger.info("scan walk complete: elapsed=%.1fs", time.monotonic() - start)


scanner = Scanner()
