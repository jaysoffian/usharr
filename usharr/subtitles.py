"""External subtitle file detection + filename parsing."""

import logging
import re
from pathlib import Path

from usharr import db
from usharr.db import SubtitleTrackRow
from usharr.langs import norm_lang

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


def find_subtitle_files(video_path: Path) -> list[Path]:
    """Return external subtitle sidecar files sharing the video's stem.

    A VobSub ``.idx`` is included only when its companion ``.sub`` exists
    (the ``.idx`` alone is useless); the ``.sub`` itself is never returned.
    """
    stem = video_path.stem
    parent = video_path.parent
    out: list[Path] = []
    try:
        for p in parent.iterdir():
            if not p.is_file():
                continue
            suffix = p.suffix.lower()
            if suffix not in SUBTITLE_EXTENSIONS:
                continue
            if not p.name.startswith(stem):
                continue
            if p.name == video_path.name:
                continue
            if suffix == ".idx" and not p.with_suffix(".sub").exists():
                continue
            out.append(p)
    except OSError as exc:
        logger.debug("subtitle scan failed for %s: %s", parent, exc)
    out.sort(key=lambda p: p.name.lower())
    return out


def parse_text_sub(video_stem: str, path: Path, codec: str | None) -> SubtitleTrackRow:
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
    return SubtitleTrackRow(
        idx=0,
        subtitle_path=str(path),
        codec=codec,
        language=lang,
        title=None,
        is_default=False,
        is_forced=forced,
        is_sdh=sdh,
    )


def parse_vobsub_idx(video_stem: str, path: Path) -> list[SubtitleTrackRow]:
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
    rows: list[SubtitleTrackRow] = []
    seen: set[int] = set()
    for code, index in VOBSUB_ID_RE.findall(text):
        idx = int(index)
        if idx in seen:
            continue
        seen.add(idx)
        rows.append(
            SubtitleTrackRow(
                idx=idx,
                subtitle_path=str(path),
                codec="VobSub",
                language=norm_lang(code),
                title=None,
                is_default=False,
                is_forced=False,
                is_sdh=False,
            )
        )
    if rows:
        return rows
    return [parse_text_sub(video_stem, path, "VobSub")]


def parse_subtitle_file(video_stem: str, path: Path) -> list[SubtitleTrackRow]:
    """Parse a sidecar into one or more subtitle_track rows."""
    suffix = path.suffix.lower()
    if suffix == ".idx":
        return parse_vobsub_idx(video_stem, path)
    codec = CODEC_BY_EXT.get(suffix, suffix.lstrip(".").upper())
    return [parse_text_sub(video_stem, path, codec)]


def sync_external_subs(video_path: Path, files: list[Path]) -> None:
    """Reconcile a video's external subtitle rows with its on-disk sidecars.

    Cheap when nothing changed: stats the files and compares against the
    recorded (size, mtime) set, only re-parsing on a diff.
    """
    disk: dict[str, tuple[int, int]] = {}
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        disk[str(p)] = (st.st_size, st.st_mtime_ns)

    existing = db.subtitle_files_for(video_path)
    if disk == existing:
        return

    payload: list[tuple[str, int, int, list[SubtitleTrackRow]]] = []
    for p in files:
        key = str(p)
        if key not in disk:
            continue
        size, mtime = disk[key]
        payload.append((key, size, mtime, parse_subtitle_file(video_path.stem, p)))
    db.replace_external_subtitles(video_path=video_path, files=payload)
