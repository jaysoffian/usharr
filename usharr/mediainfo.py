"""Container/codec/track extraction via the mediainfo CLI."""

import asyncio
import json
import logging
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from usharr import models
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


# --- mediainfo JSON -------------------------------------------------------

MEDIAINFO_TIMEOUT = 300


def run_mediainfo(path: Path, *options: str) -> bytes:
    return subprocess.run(
        ["mediainfo", *options, path],
        check=True,
        capture_output=True,
        timeout=MEDIAINFO_TIMEOUT,
    ).stdout


async def text_report(path: Path) -> str:
    out = await asyncio.to_thread(run_mediainfo, path)
    # mediainfo exits 0 and prints a bare newline when it can't open the file.
    if not out.strip():
        raise OSError(f"mediainfo could not read {path}")
    return out.decode(errors="replace")


def parse_text_report(report: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Split a mediainfo text report into (section title, [(key, value)]) pairs."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for line in report.splitlines():
        if not line.strip():
            continue
        # Menu keys are timestamps like 00:09:22.353, so split on " : " only.
        key, sep, value = line.partition(" : ")
        if sep:
            sections[-1][1].append((key.rstrip(), value))
        else:
            sections.append((line.strip(), []))
    return sections


class General(BaseModel):
    format: str | None = Field(default=None, alias="Format")
    duration: float | None = Field(default=None, alias="Duration")
    overall_bit_rate: int | None = Field(default=None, alias="OverallBitRate")
    file_size: int | None = Field(default=None, alias="FileSize")


class Video(BaseModel):
    format: str | None = Field(default=None, alias="Format")
    codec_id: str | None = Field(default=None, alias="CodecID")
    format_profile: str | None = Field(default=None, alias="Format_Profile")
    format_level: str | None = Field(default=None, alias="Format_Level")
    format_tier: str | None = Field(default=None, alias="Format_Tier")
    width: int | None = Field(default=None, alias="Width")
    height: int | None = Field(default=None, alias="Height")
    bit_depth: int | None = Field(default=None, alias="BitDepth")
    frame_rate: float | None = Field(default=None, alias="FrameRate")
    bit_rate: int | None = Field(default=None, alias="BitRate")
    bit_rate_nominal: int | None = Field(default=None, alias="BitRate_Nominal")
    bit_rate_maximum: int | None = Field(default=None, alias="BitRate_Maximum")
    hdr_format: str | None = Field(default=None, alias="HDR_Format")
    hdr_format_string: str | None = Field(default=None, alias="HDR_Format_String")
    hdr_format_commercial: str | None = Field(
        default=None, alias="HDR_Format_Commercial"
    )
    hdr_format_version: str | None = Field(default=None, alias="HDR_Format_Version")
    hdr_format_profile: str | None = Field(default=None, alias="HDR_Format_Profile")
    hdr_format_settings: str | None = Field(default=None, alias="HDR_Format_Settings")
    hdr_format_compatibility: str | None = Field(
        default=None, alias="HDR_Format_Compatibility"
    )
    transfer_characteristics: str | None = None

    @property
    def profile(self) -> str | None:
        """Profile as mediainfo prints it, e.g. "Main 10@L5.1@High"."""
        # The JSON output splits that into Format_Profile/Level/Tier and
        # drops the "L" in front of a numeric level ("Main@Main" for
        # MPEG-2 has none).
        if not self.format_profile:
            return None
        parts = [self.format_profile]
        if level := self.format_level:
            parts.append(f"L{level}" if level[0].isdigit() else level)
        if self.format_tier:
            parts.append(self.format_tier)
        return "@".join(parts)


class Audio(BaseModel):
    format: str | None = Field(default=None, alias="Format")
    format_profile: str | None = Field(default=None, alias="Format_Profile")
    format_commercial: str | None = Field(default=None, alias="Format_Commercial")
    format_commercial_if_any: str | None = Field(
        default=None, alias="Format_Commercial_IfAny"
    )
    channels: int | None = Field(default=None, alias="Channels")
    language: str | None = Field(default=None, alias="Language")
    title: str | None = Field(default=None, alias="Title")
    default: bool = Field(default=False, alias="Default")
    forced: bool = Field(default=False, alias="Forced")
    bit_rate: int | None = Field(default=None, alias="BitRate")
    bit_rate_mode: str | None = Field(default=None, alias="BitRate_Mode")
    sampling_rate: int | None = Field(default=None, alias="SamplingRate")
    bit_depth: int | None = Field(default=None, alias="BitDepth")
    compression_mode: str | None = Field(default=None, alias="Compression_Mode")


class Text(BaseModel):
    format: str | None = Field(default=None, alias="Format")
    codec_id: str | None = Field(default=None, alias="CodecID")
    language: str | None = Field(default=None, alias="Language")
    title: str | None = Field(default=None, alias="Title")
    default: bool = Field(default=False, alias="Default")
    forced: bool = Field(default=False, alias="Forced")


class Media(BaseModel):
    track: list[dict[str, Any]]


class MediaInfoJson(BaseModel):
    media: Media | None


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


def build_hdr_format(video: Video) -> str | None:
    """Return the fullest HDR description available."""
    if video.hdr_format_string:
        return video.hdr_format_string
    if not video.hdr_format:
        return None
    base = clean_slashes(video.hdr_format)
    extras: list[str] = []
    for value in (
        video.hdr_format_version,
        video.hdr_format_profile,
        video.hdr_format_settings,
        video.hdr_format_compatibility,
    ):
        if not value:
            continue
        cleaned = clean_slashes(value)
        if cleaned and cleaned not in base:
            extras.append(cleaned)
    if extras:
        return base + ", " + ", ".join(extras)
    return base


def clean_slashes(v: str) -> str:
    # The raw HDR fields separate HDR systems with " / " and leave an empty
    # slot when one system lacks a value ("dvhe.07 / ").
    return " / ".join(p.strip() for p in v.split("/") if p.strip())


DV_PROFILE_RE = re.compile(r"dv(?:he|av)\.(\d{1,2})", re.IGNORECASE)


def dv_profile_num(video: Video) -> int | None:
    """Return the Dolby Vision major profile (5, 7, 8, ...) if present."""
    m = DV_PROFILE_RE.search(video.hdr_format_profile or "")
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


def detect_hdr(video: Video) -> str | None:
    hdr_fmt = (video.hdr_format or video.hdr_format_commercial or "").lower()
    xfer = (video.transfer_characteristics or "").lower()

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
        p = dv_profile_num(video)
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
    info = MediaInfoJson.model_validate_json(
        run_mediainfo(path, "--Output=JSON", "--Full")
    )
    if info.media is None:
        raise OSError(f"mediainfo could not read {path}")
    container: str | None = None
    duration_s: float | None = None
    overall_bit_rate: int | None = None
    file_size: int | None = None
    video: VideoInfo | None = None
    video_stream_br: int | None = None
    video_nominal_br: int | None = None
    audio: list[AudioTrack] = []
    subtitle: list[SubtitleTrack] = []

    for track in info.media.track:
        match track.get("@type"):
            case "General":
                t = General.model_validate(track)
                container = t.format or path.suffix.lstrip(".").upper() or None
                duration_s = t.duration
                overall_bit_rate = t.overall_bit_rate
                file_size = t.file_size
            case "Video" if video is None:
                t = Video.model_validate(track)
                video_stream_br = t.bit_rate
                video_nominal_br = t.bit_rate_nominal
                video = VideoInfo(
                    codec=norm_video_codec(t.format, t.codec_id),
                    profile=t.profile,
                    width=t.width or 0,
                    height=t.height or 0,
                    bit_depth=t.bit_depth,
                    hdr=detect_hdr(t),
                    # Verbose HDR string surfaced on detail page. For Dolby
                    # Vision BD remuxes this includes the profile code
                    # (dvhe.07.06) and layer structure (BL+EL+RPU).
                    hdr_format=build_hdr_format(t),
                    frame_rate=t.frame_rate,
                    max_bit_rate=t.bit_rate_maximum,
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
            case "Audio":
                t = Audio.model_validate(track)
                commercial = t.format_commercial or t.format_commercial_if_any
                audio.append(
                    AudioTrack(
                        idx=len(audio),
                        codec=norm_audio_codec(t.format, t.format_profile, commercial),
                        channels=t.channels or 0,
                        layout=layout(t.channels),
                        language=norm_lang(t.language),
                        title=t.title,
                        is_default=t.default,
                        is_forced=t.forced,
                        format=t.format,
                        commercial_name=commercial,
                        bit_rate=t.bit_rate,
                        bit_rate_mode=t.bit_rate_mode,
                        sample_rate=t.sampling_rate,
                        bit_depth=t.bit_depth,
                        compression_mode=t.compression_mode,
                    ),
                )
            case "Text":
                t = Text.model_validate(track)
                subtitle.append(
                    SubtitleTrack(
                        idx=len(subtitle),
                        codec=norm_sub_codec(t.format, t.codec_id),
                        language=norm_lang(t.language),
                        title=t.title,
                        is_default=t.default,
                        is_forced=t.forced,
                        is_sdh=is_sdh(t.title),
                    ),
                )

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
    return await asyncio.to_thread(parse_sync, path)


# --- dict adapters for db layer -------------------------------------------


def to_audio_row(video_path: str, a: AudioTrack) -> models.AudioTrack:
    return models.AudioTrack.model_validate(asdict(a) | {"video_path": video_path})


def to_internal_sub_row(
    video_path: str, s: SubtitleTrack
) -> models.SubtitleTrackInternal:
    return models.SubtitleTrackInternal.model_validate(
        asdict(s) | {"video_path": video_path}
    )


def to_mediainfo_row(path: Path, mi: MediaInfoResult) -> models.Mediainfo:
    v = mi.video
    return models.Mediainfo.model_validate(
        {
            "video_path": str(path),
            "container": mi.container,
            "duration": mi.duration,
            "video_codec": v.codec if v else None,
            "video_profile": v.profile if v else None,
            "video_width": (v.width or None) if v else None,
            "video_height": (v.height or None) if v else None,
            "video_bit_depth": v.bit_depth if v else None,
            "video_hdr": v.hdr if v else None,
            "video_hdr_format": v.hdr_format if v else None,
            "video_frame_rate": v.frame_rate if v else None,
            "video_bit_rate": v.bit_rate if v else None,
            "video_max_bit_rate": v.max_bit_rate if v else None,
        }
    )
