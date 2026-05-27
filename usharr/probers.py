"""Per-pass probe workers. One queue + worker per probe type.

Probers run on every path they're handed. Scanner decides whether a
probe is warranted (file changed, force flag, no cached row) before
queueing.
"""

import asyncio
import logging
from pathlib import Path

from usharr import ardetector, db, mediainfo

logger = logging.getLogger(__name__)


class Prober:
    """Long-lived per-pass worker. Subclass and implement ``probe()``."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Path] = asyncio.Queue()
        self.pending: set[Path] = set()

    def enqueue(self, path: Path) -> None:
        if path in self.pending:
            return
        self.pending.add(path)
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
                self.pending.discard(path)
                self.queue.task_done()

    async def probe(self, path: Path) -> None:
        raise NotImplementedError


class MediainfoProber(Prober):
    """Extract Media Info"""

    async def probe(self, path: Path) -> None:
        logger.info("mediainfo: %s", path)
        try:
            mi = await mediainfo.extract(path)
        except Exception as exc:
            logger.warning("mediainfo failed for %s: %s", path, exc)
            db.set_mediainfo_error(path, str(exc)[:500])
            return

        db.upsert_mediainfo(
            mediainfo.to_mediainfo_row(path, mi),
            audio=[mediainfo.to_audio_row(a) for a in mi.audio],
            internal_subs=[mediainfo.to_internal_sub_row(s) for s in mi.subtitle],
        )


class ArdetectorProber(Prober):
    """Detect Aspect Ratios"""

    async def probe(self, path: Path) -> None:
        logger.info("ardetector: %s", path)
        try:
            result = await ardetector.detect(path)
        except Exception as exc:
            logger.warning("ardetector failed for %s: %s", path, exc)
            db.upsert_ardetector(db.ArdetectorRow(path=str(path), error=str(exc)[:500]))
            return

        db.upsert_ardetector(ardetector.to_ardetector_row(path, result))
        # Backfill duration onto mediainfo if mediainfo didn't get one since
        # the AR sampler measures runtime as a side effect.
        if result.duration is not None:
            db.set_mediainfo_duration(path, result.duration)
