"""Per-pass probe workers. One queue + worker per probe type.

Probers run on every path they're handed. Scanner decides whether a
probe is warranted (file changed, force flag, no cached row) before
queueing.
"""

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

import usharr.mediainfo as mediainfo_lib
from usharr import db
from usharr.ardetector import detect

logger = logging.getLogger(__name__)


class Prober:
    """Long-lived per-pass worker. Subclass and implement ``probe()``."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Path] = asyncio.Queue()

    def enqueue(self, path: Path) -> None:
        self.queue.put_nowait(path)

    async def process_queue_forever(self) -> None:
        """Drain the queue forever. Cancel-safe."""
        while True:
            path = await self.queue.get()
            try:
                if path.exists():
                    await self.probe(path)
            except Exception as e:
                logger.exception("%s: %s", path, str(e))
            finally:
                self.queue.task_done()

    async def probe(self, path: Path) -> None:
        raise NotImplementedError


class MediainfoProber(Prober):
    """Extract Media Info"""

    async def probe(self, path: Path) -> None:
        logger.info("mediainfo: %s", path)
        cached = db.get_mediainfo(path)
        try:
            mi = await mediainfo_lib.extract(path)
        except Exception as exc:
            logger.warning("mediainfo failed for %s: %s", path, exc)
            db.upsert_mediainfo(
                path=path,
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
                path=path,
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


class ArdetectorProber(Prober):
    """Detect Aspect Ratios"""

    async def probe(self, path: Path) -> None:
        logger.info("ardetector: %s", path)
        try:
            result = await detect(path)
        except Exception as exc:
            logger.warning("ardetector failed for %s: %s", path, exc)
            db.upsert_ardetector(
                path=path,
                error=str(exc)[:500],
                aspect_primary=None,
                aspect_widest=None,
                aspect_samples=None,
            )
            return

        db.upsert_ardetector(
            path=path,
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
            db.set_mediainfo_duration(path, result.duration)
