"""Sibling subtitle file detection + filename parsing."""

import logging
from pathlib import Path

from usharr.db import SubtitleTrackRow
from usharr.langs import norm_lang

logger = logging.getLogger(__name__)

SIDECAR_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"})

SUB_CODEC_BY_EXT = {
    ".srt": "SRT",
    ".ass": "ASS",
    ".ssa": "SSA",
    ".sub": "VobSub",
    ".idx": "VobSub",
    ".vtt": "WebVTT",
}

# Filename tail tokens that flag the sub rather than naming its language.
FORCED_TOKENS = {"forced"}
SDH_TOKENS = {"sdh", "hi", "cc"}


def find_sidecars(video_path: Path) -> list[Path]:
    """Return subtitle sidecar files sharing the video's stem."""
    stem = video_path.stem
    parent = video_path.parent
    out: list[Path] = []
    try:
        for p in parent.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in SIDECAR_EXTENSIONS:
                continue
            if not p.name.startswith(stem):
                continue
            if p.name == video_path.name:
                continue
            out.append(p)
    except OSError as exc:
        logger.debug("sidecar scan failed for %s: %s", parent, exc)
    out.sort(key=lambda p: p.name.lower())
    return out


def mtime_ns_max(paths: list[Path]) -> int | None:
    """Max mtime_ns across paths, or None if paths is empty / all stat fail."""
    best: int | None = None
    for p in paths:
        try:
            m = p.stat().st_mtime_ns
        except OSError:
            continue
        if best is None or m > best:
            best = m
    return best


def parse_sidecar(video_stem: str, sidecar: Path, idx: int) -> SubtitleTrackRow:
    """Produce a subtitle_track row for a sidecar file."""
    suffix = sidecar.suffix.lower()
    codec = SUB_CODEC_BY_EXT.get(suffix, suffix.lstrip(".").upper())

    tail = sidecar.name.removeprefix(video_stem)
    tail = tail.removesuffix(sidecar.suffix)
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
    return SubtitleTrackRow(
        idx=idx,
        source="external",
        file_path=str(sidecar),
        codec=codec,
        language=lang,
        title=None,
        is_default=False,
        is_forced=forced,
        is_sdh=sdh,
    )
