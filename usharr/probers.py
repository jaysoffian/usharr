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
        try:
            mi = await mediainfo_lib.extract(path)
        except Exception as exc:
            logger.warning("mediainfo failed for %s: %s", path, exc)
            db.set_mediainfo_error(path, str(exc)[:500])
            return

        db.upsert_mediainfo(
            mediainfo_lib.to_mediainfo_row(path, mi),
            audio=[mediainfo_lib.to_audio_row(a) for a in mi.audio],
            internal_subs=[mediainfo_lib.to_internal_sub_row(s) for s in mi.subtitle],
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
