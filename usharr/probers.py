"""Per-pass probe workers. One queue + worker per probe type."""

import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import NamedTuple

import usharr.mediainfo as mediainfo_lib
from usharr import db, subtitles
from usharr.ardetector import detect

logger = logging.getLogger(__name__)


class ProbeRequest(NamedTuple):
    path: Path
    force: bool


class Prober:
    """Long-lived per-pass worker. Subclass and implement ``probe()``."""

    name: str

    def __init__(self) -> None:
        self.queue: asyncio.Queue[ProbeRequest] = asyncio.Queue()

    def enqueue(self, path: Path, *, force: bool = False) -> None:
        self.queue.put_nowait(ProbeRequest(path, force))

    async def process_queue_forever(self) -> None:
        """Drain the queue forever. Cancel-safe."""
        while True:
            req = await self.queue.get()
            try:
                await self.probe(req.path, req.force)
            except Exception:
                logger.exception("%s failed for %s", self.name, req.path)
            finally:
                self.queue.task_done()

    async def wait(self) -> None:
        """Block until every enqueued item has been processed."""
        await self.queue.join()

    async def probe(self, path: Path, force: bool) -> None:
        raise NotImplementedError


class MediainfoProber(Prober):
    """Cheap track-metadata pass. Retried on missing rows; on failure,
    preserves cached track metadata so the UI keeps showing what we had.
    """

    name = "mediainfo"

    async def probe(self, path: Path, force: bool) -> None:
        try:
            st = path.stat()
        except OSError as exc:
            logger.warning("stat failed for %s: %s", path, exc)
            return

        mf = db.get(str(path))
        if mf is None:
            logger.warning("mediainfo: no media_file row for %s", path)
            return

        video_unchanged = mf.size_bytes == st.st_size and mf.mtime_ns == st.st_mtime_ns
        cached = db.get_mediainfo(str(path))
        if not force and video_unchanged and cached is not None:
            return

        logger.info("mediainfo: %s", path)
        now = int(time.time())
        try:
            mi = await mediainfo_lib.extract(path)
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
        else:
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
                audio=[mediainfo_lib.to_audio_row(a) for a in mi.audio],
                internal_subs=[
                    mediainfo_lib.to_internal_sub_row(s) for s in mi.subtitle
                ],
            )

        update_external_subs(path, subtitles.find_subtitles(path))


class ArdetectorProber(Prober):
    """Slow AR-sampling pass. Doesn't auto-retry persistent failures on
    the same bytes — the user has `Redetect` for that. On failure,
    records the error; cached aspect data isn't preserved (a re-run on
    the same bytes would just fail again).
    """

    name = "ardetector"

    async def probe(self, path: Path, force: bool) -> None:
        try:
            st = path.stat()
        except OSError as exc:
            logger.warning("stat failed for %s: %s", path, exc)
            return

        mf = db.get(str(path))
        if mf is None:
            logger.warning("ardetector: no media_file row for %s", path)
            return

        video_unchanged = mf.size_bytes == st.st_size and mf.mtime_ns == st.st_mtime_ns
        if not force and video_unchanged and db.get_ardetector(str(path)) is not None:
            return

        logger.info("ardetector: %s", path)
        now = int(time.time())
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
        # (the AR sampler measures runtime as a side effect). No-op if
        # the mediainfo row doesn't exist yet — that pass will fill its
        # own.
        if result.duration is not None:
            db.set_mediainfo_duration(str(path), result.duration)


def update_external_subs(path: Path, subtitle_paths: list[Path]) -> None:
    """Re-derive external sub rows numbered after the current internal block."""
    internal_count = db.count_internal_subs(str(path))
    external_subs = [
        subtitles.parse_subtitle(path.stem, s, internal_count + i)
        for i, s in enumerate(subtitle_paths)
    ]
    db.update_external_subtitles(path=str(path), subtitles=external_subs)
