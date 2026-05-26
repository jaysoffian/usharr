"""Media tree walker. Owns the per-pass probers it feeds."""

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import NamedTuple

from usharr import bazarr_sync, db, plex_sync, radarr_sync, sonarr_sync, subtitles
from usharr.config import get_config
from usharr.probers import ArdetectorProber, MediainfoProber, update_external_subs

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

    async def scan(self, /, req: ScanRequest) -> None:
        """Scan for new/updated media files and/or refresh/analyze existing files."""
        paths = get_config().all_paths
        logger.info(
            "scan walk starting across %d path(s) refresh=%s analyze=%s",
            len(paths),
            req.refresh,
            req.analyze,
        )
        start = time.monotonic()

        disk_files = self.iter_video_files(paths)
        disk_paths = {str(p) for p in disk_files}

        removed = sorted(db.list_paths() - disk_paths)
        if removed:
            n = db.delete_paths(removed)
            logger.info("Removed %d stale row(s) from DB", n)

        now = int(time.time())
        stubs = 0
        mi_enqueued = 0
        ar_enqueued = 0
        subtitle_only = 0

        for p in disk_files:
            try:
                st = p.stat()
            except OSError as exc:
                logger.warning("stat failed for %s: %s", p, exc)
                continue

            subtitle_paths = subtitles.find_subtitles(p)
            subtitles_mtime = subtitles.mtime_ns_max(subtitle_paths)
            mf = db.get(str(p))
            mi_row = db.get_mediainfo(str(p)) if mf is not None else None
            ar_row = db.get_ardetector(str(p)) if mf is not None else None

            video_unchanged = (
                mf is not None
                and mf.size_bytes == st.st_size
                and mf.mtime_ns == st.st_mtime_ns
            )
            subtitles_unchanged = (
                mf is not None and mf.subtitles_mtime_ns == subtitles_mtime
            )

            if mf is None:
                db.insert_media_file(
                    path=str(p),
                    size_bytes=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    subtitles_mtime_ns=subtitles_mtime,
                    discovered_at=now,
                )
                stubs += 1
            elif not video_unchanged or not subtitles_unchanged:
                db.update_media_file_stat(
                    path=str(p),
                    size_bytes=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    subtitles_mtime_ns=subtitles_mtime,
                )

            # Mediainfo is cheap: retry when its row is missing (e.g. after
            # a schema bump that DELETEd from `mediainfo`). Ardetector is
            # slow and deterministic — if it failed on these exact bytes,
            # it'll fail again, so only re-attempt when forced, when the
            # video changed, or when there's no row yet.
            do_mediainfo = req.force_refresh or not video_unchanged or mi_row is None
            do_ardetector = req.force_detect or not video_unchanged or ar_row is None

            if do_mediainfo:
                self.mediainfo.enqueue(p, force=req.force_refresh)
                mi_enqueued += 1
            if do_ardetector:
                self.ardetector.enqueue(p, force=req.force_detect)
                ar_enqueued += 1

            # Subtitle-only update: probes don't need to run, but
            # external subs do — re-derive inline.
            if not do_mediainfo and not do_ardetector and not subtitles_unchanged:
                update_external_subs(p, subtitle_paths)
                subtitle_only += 1

        logger.info(
            "scan walk complete: files=%d removed=%d stubs=%d "
            "mi_enqueued=%d ar_enqueued=%d subtitle_only=%d elapsed=%.1fs",
            len(disk_files),
            len(removed),
            stubs,
            mi_enqueued,
            ar_enqueued,
            subtitle_only,
            time.monotonic() - start,
        )


scanner = Scanner()
