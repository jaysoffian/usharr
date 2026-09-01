"""External subtitle file detection + filename parsing."""

import logging
import re
from collections.abc import Collection
from pathlib import Path
from typing import NamedTuple

from usharr import queries
from usharr.langs import norm_lang
from usharr.models import SubtitleTrackExternal

logger = logging.getLogger(__name__)

SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".idx", ".vtt"})

CODEC_BY_EXT = {
    ".srt": "SRT",
    ".ass": "ASS",
    ".ssa": "SSA",
    ".idx": "VobSub",
    ".vtt": "WebVTT",
}

# Filename tail tokens that flag the subtitle rather than naming its language.
FORCED_TOKENS = {"forced"}
SDH_TOKENS = {"sdh", "hi", "cc"}

# A VobSub `.idx` lists each stream as `id: <lang>, index: <n>`.
VOBSUB_ID_RE = re.compile(r"^id:\s*([A-Za-z]{2,3}),\s*index:\s*(\d+)", re.MULTILINE)


class Sidecar(NamedTuple):
    """One external subtitle file with the stat the scanner took of it."""

    path: Path
    size_bytes: int
    mtime_ns: int


def sidecar_names(video_name: str, filenames: Collection[str]) -> list[str]:
    """Pick the subtitle sidecars for ``video_name`` out of one directory's names.

    A VobSub ``.idx`` counts only when its companion ``.sub`` sits alongside it
    (the ``.idx`` alone is useless); the ``.sub`` itself is never a sidecar.
    """
    prefix = Path(video_name).stem + "."
    present = set(filenames)
    out = [
        name
        for name in filenames
        if (suffix := Path(name).suffix.lower()) in SUBTITLE_EXTENSIONS
        and name.startswith(prefix)
        and name != video_name
        and (suffix != ".idx" or Path(name).with_suffix(".sub").name in present)
    ]
    out.sort(key=str.lower)
    return out


def parse_text_sub(
    video_stem: str, path: Path, codec: str | None
) -> SubtitleTrackExternal:
    """Produce a subtitle_track row from a sidecar's filename tokens."""
    tail = path.name.removeprefix(video_stem)
    tail = tail.removesuffix(path.suffix)
    tail = tail.removeprefix(".")
    tokens = [t for t in tail.split(".") if t]

    lang: str | None = None
    forced = False
    sdh = False
    for tok in tokens:
        tl = tok.lower()
        if tl in FORCED_TOKENS:
            forced = True
            continue
        if tl in SDH_TOKENS:
            sdh = True
            continue
        if lang is None:
            maybe = norm_lang(tok)
            if maybe is not None:
                lang = maybe
    return SubtitleTrackExternal.model_validate(
        {
            "idx": 0,
            "subtitle_path": str(path),
            "codec": codec,
            "language": lang,
            "title": None,
            "is_default": False,
            "is_forced": forced,
            "is_sdh": sdh,
        }
    )


def parse_vobsub_idx(video_stem: str, path: Path) -> list[SubtitleTrackExternal]:
    """Produce one row per stream listed in a VobSub ``.idx`` file.

    Falls back to a single filename-derived row when the file can't be
    read or lists no streams.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        logger.debug("read failed for %s: %s", path, exc)
        text = ""
    # A real-world .idx may list several languages at the same index; that's
    # one stream, and (subtitle_path, idx) is the external key, so keep only
    # the first row per distinct index.
    rows: list[SubtitleTrackExternal] = []
    seen: set[int] = set()
    for code, index in VOBSUB_ID_RE.findall(text):
        idx = int(index)
        if idx in seen:
            continue
        seen.add(idx)
        rows.append(
            SubtitleTrackExternal.model_validate(
                {
                    "idx": idx,
                    "subtitle_path": str(path),
                    "codec": "VobSub",
                    "language": norm_lang(code),
                    "title": None,
                    "is_default": False,
                    "is_forced": False,
                    "is_sdh": False,
                }
            )
        )
    if rows:
        return rows
    return [parse_text_sub(video_stem, path, "VobSub")]


def parse_subtitle_file(video_stem: str, path: Path) -> list[SubtitleTrackExternal]:
    """Parse a sidecar into one or more subtitle_track rows."""
    suffix = path.suffix.lower()
    if suffix == ".idx":
        return parse_vobsub_idx(video_stem, path)
    codec = CODEC_BY_EXT.get(suffix, suffix.lstrip(".").upper())
    return [parse_text_sub(video_stem, path, codec)]


async def sync_external_subs(
    video_path: Path,
    sidecars: list[Sidecar],
    recorded: dict[str, tuple[int, int]],
) -> None:
    """Reconcile a video's external subtitle rows with its on-disk sidecars.

    ``recorded`` is the video's stored {subtitle_path: (size_bytes, mtime_ns)}.
    Cheap when nothing changed: compares the scanned stats against it, only
    re-parsing on a diff.
    """
    disk = {str(s.path): (s.size_bytes, s.mtime_ns) for s in sidecars}
    if disk == recorded:
        return

    payload: list[tuple[str, int, int, list[SubtitleTrackExternal]]] = [
        (
            str(s.path),
            s.size_bytes,
            s.mtime_ns,
            parse_subtitle_file(video_path.stem, s.path),
        )
        for s in sidecars
    ]
    await queries.replace_external_subtitles(video_path=video_path, files=payload)
