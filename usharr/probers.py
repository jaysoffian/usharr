"""Per-pass probe workers. One queue + worker per probe type.

Probers run on every path they're handed. Scanner decides whether a
probe is warranted (file changed, force flag, no cached row) before
queueing.
"""

import asyncio
import logging
from pathlib import Path

from usharr import ardetector, db, mediainfo


class Prober:
    """Long-lived per-pass worker. Subclass and implement ``probe()``."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Path] = asyncio.Queue()
        self.pending: set[Path] = set()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def enqueue(self, path: Path) -> None:
        if path in self.pending:
            return
        self.pending.add(path)
        self.queue.put_nowait(path)

    async def process_queue_forever(self) -> None:
        """Drain the queue forever. Cancel-safe."""
        while True:
            path = await self.queue.get()
            self.pending.discard(path)
            try:
                if path.exists():
                    self.logger.info("probe %s (qsize %d)", path, self.queue.qsize())
                    await self.probe(path)
            except Exception as e:
                self.logger.exception("%s: %s", path, str(e))
            finally:
                self.queue.task_done()

    async def probe(self, path: Path) -> None:
        raise NotImplementedError


class MediainfoProber(Prober):
    """Extract Media Info"""

    async def probe(self, path: Path) -> None:
        try:
            mi = await mediainfo.extract(path)
        except Exception as exc:
            self.logger.exception("%s: %s", path, exc)
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
        try:
            result = await ardetector.detect(path)
        except Exception as exc:
            self.logger.exception("%s: %s", path, exc)
            db.upsert_ardetector(db.ArdetectorRow(path=str(path), error=str(exc)[:500]))
            return

        db.upsert_ardetector(ardetector.to_ardetector_row(path, result))
        # Backfill duration onto mediainfo if mediainfo didn't get one since
        # the AR sampler measures runtime as a side effect.
        if result.duration is not None:
            db.set_mediainfo_duration(path, result.duration)
