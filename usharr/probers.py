"""Per-pass probe workers. One queue + worker per probe type.

Probers run on every path they're handed. Scanner decides whether a
probe is warranted (file changed, force flag, no cached row) before
queueing.
"""

import asyncio
import logging
from pathlib import Path

from usharr import ardetector, db, mediainfo


class ProberEvents:
    """Fan-out notifier for prober state changes. Each subscriber gets
    its own asyncio.Event; ``notify()`` sets them all. Subscribers wait,
    clear, then re-read live state from the prober singletons. Repeated
    ``notify()`` between waits collapses to a single wake-up."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Event] = set()

    def subscribe(self) -> asyncio.Event:
        ev = asyncio.Event()
        self.subscribers.add(ev)
        return ev

    def unsubscribe(self, ev: asyncio.Event) -> None:
        self.subscribers.discard(ev)

    def notify(self) -> None:
        for ev in self.subscribers:
            ev.set()


events = ProberEvents()


class Prober:
    """Long-lived per-pass worker. Subclass and implement ``probe()``."""

    def __init__(self) -> None:
        self.queue: asyncio.PriorityQueue[tuple[float, int, Path]] = (
            asyncio.PriorityQueue()
        )
        self.queue_order = 0
        self.pending: set[Path] = set()
        self.probing: Path | None = None
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def __len__(self) -> int:
        return len(self.pending)

    def enqueue(self, path: Path, *, priority: float = 0) -> None:
        if path in self.pending and priority >= 0:
            return
        self.pending.add(path)
        self.queue.put_nowait((priority, self.queue_order, path))
        self.queue_order += 1
        events.notify()

    async def process_queue_forever(self) -> None:
        """Drain the queue forever. Cancel-safe."""
        while True:
            _, _, path = await self.queue.get()
            if path not in self.pending:
                # path was bumped to front of queue and already probed.
                self.queue.task_done()
                continue
            self.pending.discard(path)
            try:
                if not path.exists():
                    continue
                self.probing = path
                events.notify()
                self.logger.info("probing %s (pending %d)", path, len(self))
                await self.probe(path)
            except Exception as e:
                self.logger.exception("%s: %s", path, str(e))
            finally:
                self.probing = None
                events.notify()
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
            db.upsert_ardetector(
                db.ArdetectorRow(video_path=str(path), error=str(exc)[:500])
            )
            return

        db.upsert_ardetector(ardetector.to_ardetector_row(path, result))
        # Backfill duration onto mediainfo if mediainfo didn't get one since
        # the AR sampler measures runtime as a side effect.
        if result.duration is not None:
            db.set_mediainfo_duration(path, result.duration)
