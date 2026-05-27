"""Container/codec/track extraction via pymediainfo."""

import asyncio
import dataclasses
import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pymediainfo import MediaInfo

from usharr.db import AudioTrackRow, MediainfoRow, SubtitleTrackRow
from usharr.langs import norm_lang

logger = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    codec: str
    profile: str | None
    width: int
    height: int
    bit_depth: int | None
    hdr: str | None
    hdr_format: str | None
    frame_rate: float | None
    bit_rate: int | None = None
    max_bit_rate: int | None = None


@dataclass
class AudioTrack:
    idx: int
    codec: str
    channels: int
    layout: str | None
    language: str | None
    title: str | None
    is_default: bool
    is_forced: bool
    format: str | None = None
    commercial_name: str | None = None
    bit_rate: int | None = None
    bit_rate_mode: str | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    compression_mode: str | None = None


@dataclass
class SubtitleTrack:
    idx: int
    codec: str
    language: str | None
    title: str | None
    is_default: bool
    is_forced: bool
    is_sdh: bool


@dataclass
class MediaInfoResult:
    container: str | None
    duration: float | None
    video: VideoInfo | None
    audio: list[AudioTrack] = field(default_factory=list)
    subtitle: list[SubtitleTrack] = field(default_factory=list)


# --- helpers --------------------------------------------------------------


def get(track: object, *names: str) -> str | None:
    for n in names:
        v = getattr(track, n, None)
        if v not in (None, ""):
            return str(v)
    return None


def to_int(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).split()[0].split(".")[0])
    except ValueError, IndexError:
        return None


def to_float(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).split()[0])
    except ValueError, IndexError:
        return None


def to_bool(v: str | None) -> bool:
    if v is None:
        return False
    return str(v).strip().lower() in {"yes", "true", "1"}


# --- normalization ---------------------------------------------------------

VIDEO_ALIAS = {
    "AVC": "AVC",
    "H264": "AVC",
    "H.264": "AVC",
    "HEVC": "HEVC",
    "H265": "HEVC",
    "H.265": "HEVC",
    "AV1": "AV1",
    "VC1": "VC-1",
    "VC-1": "VC-1",
    "VP9": "VP9",
    "VP8": "VP8",
    "MPEG-4 VISUAL": "MPEG-4",
    "MPEG VIDEO": "MPEG-2",
}


def norm_video_codec(fmt: str | None, codec_id: str | None) -> str:
    raw = (fmt or codec_id or "").strip().upper()
    return VIDEO_ALIAS.get(raw, raw or "Unknown")


AUDIO_ALIAS = {
    "E-AC-3": "EAC3",
    "EAC3": "EAC3",
    "AC-3": "AC3",
    "AC3": "AC3",
    "MLP FBA": "TrueHD",
    "TRUEHD": "TrueHD",
    "AAC": "AAC",
    "FLAC": "FLAC",
    "OPUS": "Opus",
    "PCM": "PCM",
    "VORBIS": "Vorbis",
    "MPEG AUDIO": "MP3",
}


def norm_audio_codec(
    fmt: str | None,
    profile: str | None,
    commercial: str | None,
) -> str:
    """Resolve a user-facing codec name.

    Commercial name is the most authoritative source (e.g. "DTS-HD Master
    Audio", "Dolby TrueHD with Dolby Atmos"). Fall back to
    format + format_profile.
    """
    raw = (fmt or "").strip().upper()
    prof = (profile or "").upper()
    cn = (commercial or "").upper()

    if cn:
        if "DTS-HD MASTER" in cn:
            return "DTS-HD MA"
        if "DTS-HD HIGH" in cn:
            return "DTS-HD HRA"
        if "DTS:X" in cn or "DTS-X" in cn:
            return "DTS:X"
        if "DTS EXPRESS" in cn:
            return "DTS Express"
        if "DTS-ES" in cn:
            return "DTS-ES"
        if "TRUEHD" in cn and "ATMOS" in cn:
            return "TrueHD Atmos"
        if "DOLBY DIGITAL PLUS" in cn and "ATMOS" in cn:
            return "EAC3 Atmos"
        if "DOLBY DIGITAL PLUS" in cn or "E-AC-3" in cn:
            return "EAC3"
        if "DOLBY DIGITAL" in cn:
            return "AC3"
        if "DOLBY TRUEHD" in cn:
            return "TrueHD"

    if raw in {"DTS", "DTS XLL", "DTS XBR", "DTS ES", "DTS LBR"}:
        if "MA" in prof:
            return "DTS-HD MA"
        if "HRA" in prof or "HIGH RESOLUTION" in prof:
            return "DTS-HD HRA"
        if "ES" in prof or raw == "DTS ES":
            return "DTS-ES"
        return "DTS"
    return AUDIO_ALIAS.get(raw, raw or "Unknown")


SUB_ALIAS = {
    "PGS": "PGS",
    "HDMV PGS": "PGS",
    "PGSSUB": "PGS",
    "SUBRIP": "SRT",
    "SRT": "SRT",
    "ASS": "ASS",
    "SSA": "SSA",
    "VOBSUB": "VobSub",
    "DVB SUBTITLE": "DVB",
    "DVB_SUB": "DVB",
    "WEBVTT": "WebVTT",
    "UTF-8": "SRT",
    "TIMED TEXT": "TX3G",
    "TELETEXT": "Teletext",
}


def norm_sub_codec(fmt: str | None, codec_id: str | None) -> str:
    raw = (fmt or codec_id or "").strip().upper()
    for prefix in ("S_HDMV/", "S_TEXT/", "S_VOBSUB", "S_"):
        if raw.startswith(prefix):
            raw = raw.removeprefix(prefix)
            break
    return SUB_ALIAS.get(raw, raw or "Unknown")


def build_hdr_format(track: object) -> str | None:
    """Compose the fullest HDR description available.

    pymediainfo mirrors MediaInfo's `HDR_Format/String` (the pre-assembled
    human-readable line — `"Dolby Vision, Version 1.0, Profile 8.1,
    dvhe.08.06, BL+RPU, no metadata compression, HDR10 compatible / SMPTE
    ST 2094 App 4, Version HDR10+ Profile B, HDR10+ Profile B compatible"`)
    as `other_hdr_format`, a list. Prefer that; fall back to assembling
    the raw sub-fields (`hdr_format`, `version`, `profile`, `settings`,
    `compatibility`) when it's missing. The raw fields use `" / "` as a
    per-HDR-system separator with empty slots when one system lacks a
    value; `clean_slashes` drops those.
    """
    other = getattr(track, "other_hdr_format", None)
    if isinstance(other, list) and other:
        return str(other[0])
    base = get(track, "hdr_format")
    if not base:
        return None
    base = clean_slashes(base)
    extras: list[str] = []
    for name in (
        "hdr_format_version",
        "hdr_format_profile",
        "hdr_format_settings",
        "hdr_format_compatibility",
    ):
        v = get(track, name)
        if not v:
            continue
        cleaned = clean_slashes(v)
        if cleaned and cleaned not in base:
            extras.append(cleaned)
    if extras:
        return base + ", " + ", ".join(extras)
    return base


def clean_slashes(v: str) -> str:
    """Drop empty segments around MediaInfo's per-stream `' / '` separator."""
    return " / ".join(p.strip() for p in v.split("/") if p.strip())


DV_PROFILE_RE = re.compile(r"dv(?:he|av)\.(\d{1,2})", re.IGNORECASE)


def dv_profile_num(track: object) -> int | None:
    """Return the Dolby Vision major profile (5, 7, 8, ...) if present."""
    profile = get(track, "hdr_format_profile") or ""
    m = DV_PROFILE_RE.search(profile)
    return int(m.group(1)) if m else None


DOVI_TOOL_TIMEOUT = 30


def dv_el_type(path: Path) -> str | None:
    """Return profile-7 enhancement layer type ("MEL" or "FEL").

    MediaInfoLib doesn't expose this yet (PR #2447 pending). Extract one
    frame's RPU with dovi_tool and read `el_type` from `dovi_tool info`.
    Returns None if dovi_tool is missing or the call fails.
    """
    with tempfile.NamedTemporaryFile(prefix="RPU", suffix=".bin") as tmp:
        try:
            subprocess.run(
                ["dovi_tool", "extract-rpu", path, "-l", "1", "-o", tmp.name],
                check=True,
                capture_output=True,
                timeout=DOVI_TOOL_TIMEOUT,
            )
            info = subprocess.run(
                ["dovi_tool", "info", "-i", tmp.name, "-f", "0"],
                check=True,
                capture_output=True,
                text=True,
                timeout=DOVI_TOOL_TIMEOUT,
            )
        except (
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as e:
            logger.debug("dovi_tool failed for %s: %s", path, e)
            return None
        # `info` prints a "Parsing RPU file..." preamble before the JSON.
        brace = info.stdout.find("{")
        if brace < 0:
            logger.warning("dovi_tool info: no JSON in output for %s", path)
            return None
        try:
            data = json.loads(info.stdout[brace:])
        except json.JSONDecodeError as e:
            logger.warning("dovi_tool info: JSON parse failed for %s: %s", path, e)
            return None
        el = data.get("el_type")
        return el if el in ("MEL", "FEL") else None


def detect_hdr(track: object) -> str | None:
    hdr_fmt = (get(track, "hdr_format", "hdr_format_commercial") or "").lower()
    xfer = (get(track, "transfer_characteristics") or "").lower()

    has_dv = "dolby vision" in hdr_fmt or "dolbyvision" in hdr_fmt
    # HDR10+: dynamic metadata (SMPTE ST 2094 App 4). Matching by the
    # standard name catches files that don't literally say "HDR10+".
    has_hdr10p = (
        "hdr10+" in hdr_fmt or "hdr10 plus" in hdr_fmt or "smpte st 2094" in hdr_fmt
    )
    # HDR10: static metadata (SMPTE ST 2086). Don't match "hdr10" alone
    # as substring of "hdr10+". HDR10+ implies HDR10 baseline, so if
    # dynamic metadata is present we force the baseline on too.
    has_hdr10 = (
        has_hdr10p
        or "smpte st 2086" in hdr_fmt
        or (
            "hdr10" in hdr_fmt
            and "hdr10+" not in hdr_fmt
            and "hdr10 plus" not in hdr_fmt
        )
    )
    has_hlg = "hlg" in hdr_fmt or "hybrid log-gamma" in xfer or xfer == "arib std-b67"

    parts = []
    if has_dv:
        p = dv_profile_num(track)
        parts.append(f"DV({p})" if p is not None else "DV")
    if has_hdr10:
        parts.append("HDR10")
    if has_hdr10p:
        parts.append("HDR10+")
    if has_hlg:
        parts.append("HLG")
    return "/".join(parts) if parts else None


CHANNEL_LAYOUT = {
    1: "1.0",
    2: "2.0",
    3: "2.1",
    6: "5.1",
    7: "6.1",
    8: "7.1",
}


def layout(channels: int | None) -> str | None:
    if channels is None:
        return None
    return CHANNEL_LAYOUT.get(channels, f"{channels}ch")


def is_sdh(title: str | None) -> bool:
    if not title:
        return False
    t = title.lower()
    return (
        "sdh" in t
        or "hard of hearing" in t
        or "hearing impaired" in t
        or " cc" in f" {t}"
    )


# --- parse ----------------------------------------------------------------


def parse_sync(path: Path) -> MediaInfoResult:
    mi = MediaInfo.parse(str(path))
    container: str | None = None
    duration_s: float | None = None
    overall_bit_rate: int | None = None
    file_size: int | None = None
    video: VideoInfo | None = None
    video_stream_br: int | None = None
    video_nominal_br: int | None = None
    audio: list[AudioTrack] = []
    subtitle: list[SubtitleTrack] = []

    audio_idx = 0
    sub_idx = 0

    for t in mi.tracks:
        tt = t.track_type
        if tt == "General":
            container = get(t, "format") or path.suffix.lstrip(".").upper() or None
            dur_ms = to_float(get(t, "duration"))
            if dur_ms is not None:
                duration_s = dur_ms / 1000.0
            overall_bit_rate = to_int(get(t, "overall_bit_rate"))
            file_size = to_int(get(t, "file_size"))
        elif tt == "Video" and video is None:
            video_stream_br = to_int(get(t, "bit_rate"))
            video_nominal_br = to_int(get(t, "nominal_bit_rate"))
            video = VideoInfo(
                codec=norm_video_codec(get(t, "format"), get(t, "codec_id")),
                profile=get(t, "format_profile"),
                width=to_int(get(t, "width")) or 0,
                height=to_int(get(t, "height")) or 0,
                bit_depth=to_int(get(t, "bit_depth")),
                hdr=detect_hdr(t),
                # Verbose HDR string surfaced on detail page. For Dolby
                # Vision BD remuxes this includes the profile code
                # (dvhe.07.06) and layer structure (BL+EL+RPU).
                hdr_format=build_hdr_format(t),
                frame_rate=to_float(get(t, "frame_rate")),
                max_bit_rate=to_int(get(t, "maximum_bit_rate")),
            )
            if (
                dv_profile_num(t) == 7
                and video.hdr_format
                and "BL+EL+RPU" in video.hdr_format
            ):
                el = dv_el_type(path)
                if el:
                    video.hdr_format = video.hdr_format.replace(
                        "BL+EL+RPU",
                        f"BL+{el}+RPU",
                    )
        elif tt == "Audio":
            channels = to_int(get(t, "channel_s", "channels"))
            raw_format = get(t, "format")
            commercial = get(t, "commercial_name", "format_commercial_if_any")
            bit_rate_hz = to_int(get(t, "bit_rate"))
            audio.append(
                AudioTrack(
                    idx=audio_idx,
                    codec=norm_audio_codec(
                        raw_format,
                        get(t, "format_profile"),
                        commercial,
                    ),
                    channels=channels or 0,
                    layout=layout(channels),
                    language=norm_lang(get(t, "language", "other_language")),
                    title=get(t, "title"),
                    is_default=to_bool(get(t, "default")),
                    is_forced=to_bool(get(t, "forced")),
                    format=raw_format,
                    commercial_name=commercial,
                    bit_rate=bit_rate_hz,
                    bit_rate_mode=get(t, "bit_rate_mode"),
                    sample_rate=to_int(get(t, "sampling_rate")),
                    bit_depth=to_int(get(t, "bit_depth")),
                    compression_mode=get(t, "compression_mode"),
                ),
            )
            audio_idx += 1
        elif tt == "Text":
            title = get(t, "title")
            subtitle.append(
                SubtitleTrack(
                    idx=sub_idx,
                    codec=norm_sub_codec(get(t, "format"), get(t, "codec_id")),
                    language=norm_lang(get(t, "language", "other_language")),
                    title=title,
                    is_default=to_bool(get(t, "default")),
                    is_forced=to_bool(get(t, "forced")),
                    is_sdh=is_sdh(title),
                ),
            )
            sub_idx += 1

    if video is not None:
        video.bit_rate = resolve_video_bit_rate(
            stream=video_stream_br,
            nominal=video_nominal_br,
            overall=overall_bit_rate,
            audio=audio,
            file_size=file_size,
            duration=duration_s,
        )

    return MediaInfoResult(
        container=container,
        duration=duration_s,
        video=video,
        audio=audio,
        subtitle=subtitle,
    )


def resolve_video_bit_rate(
    *,
    stream: int | None,
    nominal: int | None,
    overall: int | None,
    audio: list[AudioTrack],
    file_size: int | None,
    duration: float | None,
) -> int | None:
    """MKV/TS rarely store per-stream video bit rate. Fall back the same way
    MediaInfo's GUI does: encoder-declared nominal, then overall minus audio,
    then derive overall from file size and duration."""
    if stream:
        return stream
    if nominal:
        return nominal
    audio_total = sum(a.bit_rate for a in audio if a.bit_rate)
    if overall:
        derived = overall - audio_total
        return derived if derived > 0 else None
    if file_size and duration and duration > 0:
        derived = int(file_size * 8 / duration) - audio_total
        return derived if derived > 0 else None
    return None


async def extract(path: Path) -> MediaInfoResult:
    """Parse a file via pymediainfo off the event loop."""
    return await asyncio.to_thread(parse_sync, path)


# --- dict adapters for db layer -------------------------------------------


def to_audio_row(a: AudioTrack) -> AudioTrackRow:
    return AudioTrackRow(
        idx=a.idx,
        codec=a.codec,
        channels=a.channels,
        layout=a.layout,
        language=a.language,
        title=a.title,
        is_default=a.is_default,
        is_forced=a.is_forced,
        format=a.format,
        commercial_name=a.commercial_name,
        bit_rate=a.bit_rate,
        bit_rate_mode=a.bit_rate_mode,
        sample_rate=a.sample_rate,
        bit_depth=a.bit_depth,
        compression_mode=a.compression_mode,
    )


def to_internal_sub_row(s: SubtitleTrack) -> SubtitleTrackRow:
    return SubtitleTrackRow(
        idx=s.idx,
        source="internal",
        file_path=None,
        codec=s.codec,
        language=s.language,
        title=s.title,
        is_default=s.is_default,
        is_forced=s.is_forced,
        is_sdh=s.is_sdh,
    )


def to_mediainfo_row(path: Path, mi: MediaInfoResult) -> MediainfoRow:
    base = MediainfoRow(
        path=str(path),
        container=mi.container,
        duration=mi.duration,
    )
    if mi.video is None:
        return base
    v = mi.video
    return dataclasses.replace(
        base,
        video_codec=v.codec,
        video_profile=v.profile,
        video_width=v.width or None,
        video_height=v.height or None,
        video_bit_depth=v.bit_depth,
        video_hdr=v.hdr,
        video_hdr_format=v.hdr_format,
        video_frame_rate=v.frame_rate,
        video_bit_rate=v.bit_rate,
        video_max_bit_rate=v.max_bit_rate,
    )
