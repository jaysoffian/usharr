"""
Aspect ratio detection using ffmpeg cropdetect and heuristics.  Originally
based upon tinyMediaManager, but with changes discovered through trial and
error across a large library of movies and TV show episodes.
"""

import asyncio
import json
import logging
import math
import re
from array import array
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from itertools import pairwise
from pathlib import Path

from usharr.models import Ardetector

logger = logging.getLogger(__name__)

# All the aspect ratios I've ever come across in actual use.
ASPECT_RATIOS: tuple[float, ...] = (
    1.33,  # 4:3 (SDTV, Academy pre-sound)
    1.37,  # Academy (1.375)
    1.43,  # IMAX 70mm / GT
    1.46,  # ARRI ALEXA 35 3:2 Open Gate (4608x3164)
    1.50,  # 3:2
    1.54,  # Magnascope parts of Hell's Angels (Criterion 4K UHD release)
    1.66,  # Super 16mm, European Widescreen
    1.78,  # 16:9 (HDTV)
    1.85,  # Flat Widescreen
    1.90,  # IMAX Digital / DCI 4K (4096x2160≈1.90)
    2.00,  # RKO Superscope, Univisium
    2.20,  # Todd-AO, 70mm
    2.35,  # Original 35mm CinemaScope, Panavision
    2.40,  # Modern CinemaScope (rounded up from 4096x1716≈2.39)
    2.55,  # CinemaScope 55
    2.66,  # CinemaScope
    2.76,  # Ultra Panavision 70
    3.00,
    4.00,
)
# Ensure ASPECT_RATIOS is sorted ascending as required by round_ar
ASPECT_RATIOS = tuple(sorted(ASPECT_RATIOS))

# Sampling schedule — coarse initial pass, then bisect around orphans.
# Single-AR films converge at INITIAL_SAMPLE_COUNT samples. Multi-AR films
# densify adaptively around detected transitions/orphans, up to SAMPLE_COUNT_MAX.
INITIAL_SAMPLE_COUNT = 60
SAMPLE_COUNT_MAX = 360  # wall-clock ceiling for heavy multi-AR films
# Stop bisecting around an orphan once neighbouring samples are within this
# many seconds — further refinement can't resolve sub-15s blips.
MIN_REFINEMENT_GAP_SEC = 15
# Once a file reads as multi-AR, bisect between every adjacent pair of samples
# whose ARs differ until the pair is this close, so each AR change is placed
# to within this many seconds.
BOUNDARY_GAP_SEC = 10
SAMPLE_DURATION = 1  # used only as an end-of-file safety margin

# global parameters
IGNORE_BEGINNING_PCT = 2.0
IGNORE_END_PCT = 8.0
AR_SECONDARY_DELTA = 0.15
PLAUSI_WIDTH_PCT = 50.0
PLAUSI_HEIGHT_PCT = 40.0  # TMM default 60; lowered to admit 3.00 and 4.00 AR crops
PLAUSI_WIDTH_DELTA_PCT = 1.5
PLAUSI_HEIGHT_DELTA_PCT = 2.0
DARK_LEVEL_PCT = 7.0
DARK_LEVEL_MAX_PCT = 13.0

# Temporal segment detection: consecutive samples whose AR readings differ by
# less than this count as the same AR segment. 0.075 = AR_SECONDARY_DELTA / 2
# — narrower than the inter-cluster suppression, wider than cropdetect jitter.
SEGMENT_AR_TOLERANCE = AR_SECONDARY_DELTA / 2

# A segment must contain at least this many consecutive samples to count as
# a real AR (vs. isolated cropdetect noise on scene transitions).
MIN_SEGMENT_SAMPLES = 2

# A real AR change keeps either the full width or the full height of the
# film's frame: wider → letterboxed, narrower → pillarboxed. A segment whose
# crop is smaller than the primary segment's on BOTH axes (beyond cropdetect
# jitter) is therefore an inset — windowboxed archival footage, a film's
# "small frame" sequences, or a dark frame where only a bright patch was
# found — and is not a ratio the film is presented in.
INSET_TOLERANCE_PCT = 1.0

# The primary AR is the one to lock the presentation to. For a multi-AR film
# that's the frame every section fits inside, when some section actually
# fills that frame (The French Dispatch is 87% 1.37, but the 1.37 and 2.40
# sections are both inside a 1.85 letterbox that a 4% section fills — lock
# 1.85). When no section encloses the others (a 2.00 show with full-height
# 1.33 flashbacks) fall back to the AR with the most runtime.
#
# An enclosing section that is the full container is suspect: a title card
# or a bright frame reads as full-frame, and two such samples would make
# every 2.40 show "1.78". Require that much share before trusting it.
FRAME_MIN_PCT = 10.0

# Minority-AR recheck (see the section of that name). Luma constants
# are in 8-bit units and scale by 2^(bit_depth-8) at use.
#
# An outer strip whose p90-p10 is at most this is flat: a bar at some level,
# whatever that level is.
FLAT_SPREAD = 2
# Added over the bar level to call a pixel bright; the margin parse_dark_level
# adds over YLOW.
EDGE_MARGIN = 2
# A flat strip is a lifted bar only below this share of full scale, the same
# ceiling as DARK_LEVEL_MAX_PCT.
LIFTED_BAR_MAX_PCT = 13.0
# Lines in an outer strip.
OUTER_LINES = 4
# A bar thinner than max(4, this share of the dimension) is a residual sliver
# beside a picture edge, not a bar to judge.
MIN_BAR_PCT = 0.5
# Distance past the edge at which the plateau is read, per 1920 px of width,
# capped at half the span between the two edges; a span under FAR_MIN_LINES
# is inconclusive.
FAR_LINES_PER_1920 = 24
FAR_MIN_LINES = 8
# A matte edge is uniform along the line, so the bright fraction jumps from
# nothing to its plateau within a line or two even when the matte is blurred;
# a film edge, iris or vignette ramps. Measured mattes step 0.57-1.51 of the
# plateau, film edges and vignettes 0.07-0.27. Compare, never clamp: a step
# can exceed 1 when the picture dims again past the edge.
STEP_HARD = 0.4
# Minimum plateau for a result: narrowing a side needs an unmistakable
# picture past the new edge, rejecting a sample only something to measure.
A_MIN_PLATEAU = 0.5
B_MIN_PLATEAU = 0.10

# A frame is monochrome iff its peak chroma is low AND the chroma is
# distributed uniformly across the frame.
#
# * SATMAX < threshold rules out anything with at least one saturated patch
#   (e.g. the red coat in an otherwise B&W frame).
# * (SATMAX - SATAVG) < spread guards against heavily desaturated *color*
#   footage (dim sepia-graded night scenes etc.), where peak chroma sits
#   in the 15-25 range but most pixels are near-neutral so the average is
#   far below the peak. Uniform monochrome — true B&W or actual sepia —
#   has every pixel sharing the same chroma offset, so peak ≈ average.
MONOCHROME_SATMAX_THRESHOLD = 25.0
MONOCHROME_SPREAD_MAX = 10.0

# Frames dimmer than this carry no usable chroma signal — SATMAX and SATAVG
# both sit near zero regardless of the underlying content. Excluded from the
# color/mono tally so scene-cut black frames don't get charged as monochrome.
CHROMA_YAVG_MIN = 20.0

# parsing regexes
P_YLOW = re.compile(r"lavfi\.signalstats\.YLOW=([0-9]*)")
P_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")
P_SATAVG = re.compile(r"lavfi\.signalstats\.SATAVG=([0-9.]+)")
P_SATMAX = re.compile(r"lavfi\.signalstats\.SATMAX=([0-9.]+)")
P_SAMPLE = re.compile(
    r"x1:([0-9]*)\sx2:([0-9]*)\sy1:([0-9]*)\sy2:([0-9]*)\sw:([0-9]*)\sh:([0-9]*)\sx:"
)
# Full-decode variant captures the t: timestamp so we can place samples on the
# timeline without an external -ss reference.
P_FULL_SAMPLE = re.compile(
    r"x1:([0-9]+)\sx2:([0-9]+)\sy1:([0-9]+)\sy2:([0-9]+)"
    r"\sw:([0-9]+)\sh:([0-9]+)\sx:[0-9]+\sy:[0-9]+"
    r"\spts:-?[0-9]+\st:(-?[0-9.]+)"
)
# metadata=print's frame line; with -noaccurate_seek the decoded keyframe sits
# at or before the requested time and pts_time is its offset from it.
P_PTS_TIME = re.compile(r"pts_time:(-?[0-9.]+)")
P_DUR = re.compile(r"Duration:\s(\d\d:\d\d:\d\d\.\d\d),")

Sample = tuple[int, float, int, int]


def java_round(x: float) -> int:
    """Java Math.round on a float: half-away-from-zero for positives."""
    return math.floor(x + 0.5)


@dataclass
class DetectedAR:
    aspect: float  # snapped to ASPECT_RATIOS
    percentage: float
    # From the largest segment that snapped to `aspect`: its median AR and the
    # most common crop in pixels.
    measured: float
    width: int
    height: int


@dataclass
class DetectionResult:
    primary_aspect: float  # the AR to lock to; see FRAME_MIN_PCT
    widest_aspect: float  # widest post-snap AR
    detected: list[DetectedAR]
    duration: float
    sar: float
    # The per-sample timeline as stored, see timeline_json.
    timeline: dict
    # Fraction of samples that looked like color (SATMAX ≥ threshold). 1.0
    # = all color; 0.0 = pure monochrome; mid-range = mixed (e.g. a B&W
    # episode with a colored studio bumper). None when no sample produced
    # a usable SATMAX reading.
    color_pct: float | None = None


@dataclass
class MediaInfo:
    """Caller-supplied metadata; obtained here via ffprobe."""

    width: int
    height: int
    duration: float
    bit_depth: int
    pixel_aspect_ratio: float


@dataclass
class VideoInfo:
    width: int = 0
    height: int = 0
    duration: int = 0
    bit_depth: int = 0
    dark_level: int = 0

    sample_count: int = 0
    ar_sample: float = 0.0

    # Color-vs-monochrome tallies. `color_samples` counts samples classified
    # as color; `chroma_samples` counts samples that produced a usable
    # chroma reading at all (the denominator).
    color_samples: int = 0
    chroma_samples: int = 0

    # (timestamp_sec, ar_calculated, crop_width, crop_height) for every sample
    # that passed plausibility, in sampling order (refinement passes append
    # samples out of timestamp order). Consumers that need chronological order
    # must sort this first. Seek sampling drops a sample whose frame time is
    # already present; the full-decode pass may record one time twice.
    timeline: list[Sample] = field(default_factory=list)
    # What the minority recheck changed: the reading each narrowed sample had
    # before, keyed by its timestamp, and the readings it rejected.
    narrowed: dict[int, Sample] = field(default_factory=dict)
    rejected: list[Sample] = field(default_factory=list)


# --------------------------------------------------------------------------
# ffprobe — caller-supplied MediaInfo equivalent
# --------------------------------------------------------------------------


async def ffprobe_media_info(path: Path) -> MediaInfo | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "format=duration:"
        "stream=width,height,sample_aspect_ratio,bits_per_raw_sample,"
        "bits_per_sample,pix_fmt,codec_type,disposition",
        "-select_streams",
        "v",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    try:
        info = json.loads(stdout)
        streams = info.get("streams") or []
        # Skip embedded cover art (attached_pic); pick the first real video.
        s = next(
            (
                s
                for s in streams
                if (s.get("disposition") or {}).get("attached_pic") != 1
            ),
            None,
        )
        if s is None:
            return None
        width = int(s.get("width") or 0)
        height = int(s.get("height") or 0)
        duration = float(info.get("format", {}).get("duration") or 0.0)
        sar_raw = s.get("sample_aspect_ratio") or "1:1"
        par = parse_ratio(sar_raw)
        bit_depth = parse_bit_depth(s)
    except KeyError, ValueError, TypeError:
        return None
    return MediaInfo(
        width=width,
        height=height,
        duration=duration,
        bit_depth=bit_depth,
        pixel_aspect_ratio=par,
    )


def parse_bit_depth(stream: dict) -> int:
    """Derive luma bit depth from ffprobe fields, preferring pix_fmt.

    Many remuxes don't populate bits_per_raw_sample, so pix_fmt
    (e.g. yuv420p10le → 10) is the most reliable source. Catches
    planar (yuv*p10le, gbrp10le), packed (p010le, p016le), and 12/14/16-bit.
    """
    pix_fmt = str(stream.get("pix_fmt") or "").lower()
    markers = (
        ("p16", 16),
        ("16le", 16),
        ("16be", 16),
        ("p14", 14),
        ("14le", 14),
        ("14be", 14),
        ("p12", 12),
        ("12le", 12),
        ("12be", 12),
        ("p10", 10),
        ("10le", 10),
        ("10be", 10),
    )
    for marker, depth in markers:
        if marker in pix_fmt:
            return depth
    bd = stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")
    try:
        parsed = int(bd) if bd else 0
    except TypeError, ValueError:
        parsed = 0
    return parsed if parsed > 0 else 8


def parse_ratio(s: str) -> float:
    try:
        a, b = s.split(":", 1)
        num, den = float(a), float(b)
        if num <= 0 or den <= 0:
            return 1.0
        return num / den
    except ValueError, ZeroDivisionError:
        return 1.0


# --------------------------------------------------------------------------
# FFmpeg invocations
# --------------------------------------------------------------------------


async def run_ffmpeg(
    argv: list[str],
    pass_label: str,
    timeout: float = 120.0,
) -> str:
    """ffmpeg's output as one string. Callers use `-f null`, which writes
    nothing to stdout, so this is ffmpeg's log."""
    stdout, stderr = await run_ffmpeg_split(argv, pass_label, timeout)
    return stdout.decode("utf-8", errors="replace") + stderr


async def run_ffmpeg_split(
    argv: list[str],
    pass_label: str,
    timeout: float = 120.0,
) -> tuple[bytes, str]:
    """Run ffmpeg, returning stdout as bytes and stderr as text."""
    logger.debug("%s: %s", pass_label, " ".join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        msg = f"ffmpeg timed out after {timeout}s"
        raise RuntimeError(msg) from None
    if proc.returncode != 0:
        msg = f"ffmpeg exit {proc.returncode}"
        raise RuntimeError(msg)
    return stdout, stderr.decode("utf-8", errors="replace")


async def scan_dark_level(path: Path, position: float = 0.0) -> str:
    # -ss before -i, -vframes 1, signalstats,metadata=print.
    return await run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-an",
            "-dn",
            "-sn",
            "-ss",
            repr(float(position)),
            "-i",
            str(path),
            "-vf",
            "signalstats,metadata=print",
            "-vframes",
            "1",
            "-f",
            "null",
            "pipe:1",
        ],
        pass_label="0",
    )


async def scan_sample(
    path: Path,
    start: int,
    dark_level: int,
    pass_label: str,
) -> str:
    # TMM uses `-t <duration>` (2s) and takes the
    # first cropdetect line, wasting ~47 frames of decode per sample. We use
    # `-vframes 1` + `skip=0` to evaluate exactly one frame (the keyframe
    # ffmpeg lands on with -noaccurate_seek). Content-identical to TMM's
    # first-match reading at a fraction of the decode cost.
    # signalstats+metadata=print piggybacks on the same decoded frame to
    # emit SATMAX for the color/monochrome classifier.
    return await run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-an",
            "-dn",
            "-sn",
            "-noaccurate_seek",
            "-ss",
            str(int(start)),
            "-i",
            str(path),
            "-vf",
            (
                f"cropdetect=limit={int(dark_level)}:round=2:skip=0,"
                "signalstats,metadata=print"
            ),
            "-vframes",
            "1",
            "-f",
            "null",
            "pipe:1",
        ],
        pass_label=pass_label,
    )


async def scan_plane(
    path: Path,
    start: int,
    dark_level: int,
    pass_label: str,
) -> tuple[bytes, str]:
    # The same seek as scan_sample, so the same keyframe is scored, with that
    # frame's luma plane on stdout and cropdetect's line on stderr.
    # -fps_mode passthrough keeps the frames before `start` that
    # -noaccurate_seek decodes with negative timestamps; the default sync
    # drops them and the plane would be a later frame than the one cropdetect
    # scored. extractplanes=y hands the plane over as is, gray8 or gray10le
    # with no range conversion (format=gray rescales limited to full range).
    return await run_ffmpeg_split(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-an",
            "-dn",
            "-sn",
            "-noaccurate_seek",
            "-ss",
            str(int(start)),
            "-i",
            str(path),
            "-fps_mode",
            "passthrough",
            "-vf",
            f"cropdetect=limit={int(dark_level)}:round=2:skip=0,extractplanes=y",
            "-vframes",
            "1",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        pass_label=pass_label,
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_video_meta(buf: str, mi: MediaInfo, vi: VideoInfo) -> None:
    m = P_DUR.search(buf)
    if m is not None:
        try:
            h, mnt, s_cs = m.group(1).split(":")
            sec, _ = s_cs.split(".")
            # truncate, do not round.
            vi.duration = int(h) * 3600 + int(mnt) * 60 + int(sec)
        except ValueError, AttributeError:
            vi.duration = int(mi.duration)
    else:
        vi.duration = int(mi.duration)

    # Deviate from TMM: we get width/height/SAR from ffprobe (which
    # filters out attached_pic cover-art streams); TMM parses ffmpeg's
    # banner, which sometimes matches cover art first and poisons the
    # plausibility checks. Skip the banner regexes entirely.
    vi.width = mi.width
    vi.height = mi.height
    sar = mi.pixel_aspect_ratio
    # SAR ≤ 0.5 → force 1.0 (same rule for fallback).
    if sar <= 0.5:
        sar = 1.0
    vi.ar_sample = sar


def parse_dark_level(buf: str, vi: VideoInfo) -> None:
    m = P_YLOW.search(buf)
    if m is not None and m.group(1):
        try:
            ylow = int(m.group(1))
            # darkLevel = YLOW + 2^(bitDepth-7).
            vi.dark_level = ylow + (1 << (vi.bit_depth - 7))
            return
        except ValueError:
            pass
    # sentinel: 9999 → always forces fallback branch.
    vi.dark_level = 9999


def classify_is_color(
    satmax: float | None,
    satavg: float | None,
    yavg: float | None,
) -> bool | None:
    """Color iff peak chroma is high OR chroma is non-uniform across the
    frame. Returns None when no SATMAX reading is available, or when the
    frame is too dark to carry reliable chroma.
    """
    if satmax is None:
        return None
    if yavg is not None and yavg < CHROMA_YAVG_MIN:
        return None
    if satmax >= MONOCHROME_SATMAX_THRESHOLD:
        return True
    if satavg is None:
        return False
    return (satmax - satavg) >= MONOCHROME_SPREAD_MAX


def count_chroma(vi: VideoInfo, is_color: bool | None) -> None:
    if is_color is None:
        return
    vi.chroma_samples += 1
    if is_color:
        vi.color_samples += 1


def calculated_ar(width: int, height: int, sar: float) -> tuple[float, float]:
    """The crop's own ratio and the ratio after the sample aspect ratio."""
    ar_measured = (width / height) if height > 0 else 9.99
    # 10E5 in Java is 1e6 — round to 6 decimals.
    ar_calculated = java_round(ar_measured * sar * 1_000_000) / 1_000_000
    return ar_measured, ar_calculated


def bar_widths(
    x1: int, x2: int, y1: int, y2: int, vi: VideoInfo
) -> tuple[int, int, int, int]:
    """Left, right, top and bottom bars of a crop, in pixels."""
    return x1, abs(vi.width - x2 - 1), y1, abs(vi.height - y2 - 1)


def plausible_crop(
    x1: int,
    x2: int,
    y1: int,
    y2: int,
    width: int,
    height: int,
    vi: VideoInfo,
    pass_label: str,
) -> bool:
    """Bars symmetric within tolerance and a crop above the size floor."""
    black_left, black_right, black_top, black_bottom = bar_widths(x1, x2, y1, y2, vi)
    if abs(black_left - black_right) > vi.width * PLAUSI_WIDTH_DELTA_PCT / 100:
        logger.debug(
            "%s: reject: |blackLeft-blackRight| exceeds width delta", pass_label
        )
        return False
    if abs(black_top - black_bottom) > vi.height * PLAUSI_HEIGHT_DELTA_PCT / 100:
        logger.debug(
            "%s: reject: |blackTop-blackBottom| exceeds height delta", pass_label
        )
        return False
    if vi.width * PLAUSI_WIDTH_PCT / 100 >= width:
        logger.debug("%s: reject: crop width too narrow", pass_label)
        return False
    if vi.height * PLAUSI_HEIGHT_PCT / 100 >= height:
        logger.debug("%s: reject: crop height too short", pass_label)
        return False
    return True


def record_sample(
    x1: int,
    x2: int,
    y1: int,
    y2: int,
    width: int,
    height: int,
    t_sec: int,
    vi: VideoInfo,
    pass_label: str,
    is_color: bool | None = None,
) -> bool:
    """Run plausibility checks; on pass, append to vi.timeline.

    Shared by per-sample input-seek scans and the full-decode fallback.
    Chroma classification is accumulated independently of the AR
    plausibility result so we still get a color classification on frames
    whose crop reading was rejected.
    """
    count_chroma(vi, is_color)
    black_left, black_right, black_top, black_bottom = bar_widths(x1, x2, y1, y2, vi)
    ar_measured, ar_calculated = calculated_ar(width, height, vi.ar_sample)

    logger.debug(
        "%s: t=%ds sample: w=%d h=%d bL=%d bR=%d bT=%d bB=%d"
        " arMeasured=%.5f arCalc=%.6f",
        pass_label,
        t_sec,
        width,
        height,
        black_left,
        black_right,
        black_top,
        black_bottom,
        ar_measured,
        ar_calculated,
    )

    if not plausible_crop(x1, x2, y1, y2, width, height, vi, pass_label):
        return False

    vi.timeline.append((t_sec, ar_calculated, width, height))
    vi.sample_count += 1
    logger.debug(
        "%s: accept: t=%ds sampleCount=%d ar=%s",
        pass_label,
        t_sec,
        vi.sample_count,
        ar_calculated,
    )
    return True


def parse_float(pattern: re.Pattern[str], buf: str) -> float | None:
    m = pattern.search(buf)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_sample(
    buf: str,
    t_sec: int,
    vi: VideoInfo,
    pass_label: str,
) -> None:
    # use the FIRST match — the first decoded frame in the window.
    m = P_SAMPLE.search(buf)
    satmax = parse_float(P_SATMAX, buf)
    satavg = parse_float(P_SATAVG, buf)
    yavg = parse_float(P_YAVG, buf)
    is_color = classify_is_color(satmax, satavg, yavg)
    logger.debug(
        "%s: t=%ds chroma: SATMAX=%s SATAVG=%s YAVG=%s is_color=%s",
        pass_label,
        t_sec,
        satmax,
        satavg,
        yavg,
        is_color,
    )
    if m is None:
        logger.debug("%s: sample: no cropdetect match in output", pass_label)
        # Still count the chroma reading even when cropdetect rejected the
        # frame — color classification is independent of plausibility.
        count_chroma(vi, is_color)
        return
    frame_sec = frame_time(buf, t_sec)
    if any(entry[0] == frame_sec for entry in vi.timeline):
        logger.debug(
            "%s: t=%ds landed on the frame at %ds, already sampled",
            pass_label,
            t_sec,
            frame_sec,
        )
        return
    record_sample(
        x1=int(m.group(1)),
        x2=int(m.group(2)),
        y1=int(m.group(3)),
        y2=int(m.group(4)),
        width=int(m.group(5)),
        height=int(m.group(6)),
        t_sec=frame_sec,
        vi=vi,
        pass_label=pass_label,
        is_color=is_color,
    )


def frame_time(buf: str, t_sec: int) -> int:
    """The time of the decoded frame: the requested time plus the pts_time
    offset metadata=print reports, or the requested time when there is none.
    Rounded up, so that seeking to it decodes the same keyframe again (the
    recheck depends on that); keyframes are at least a second apart."""
    offset = parse_float(P_PTS_TIME, buf)
    if offset is None:
        return t_sec
    return math.ceil(t_sec + offset)


# --------------------------------------------------------------------------
# Post-loop analysis — temporal segment detection
#
# A pure-histogram approach (TMM's, and our earlier versions) loses the
# temporal structure of samples. That makes it impossible to distinguish:
#   * Two 2.76 readings 30s apart → a real ~60s scope segment
#   * Two 2.76 readings at unrelated timestamps → likely noise
# So instead of extracting clusters from the bag-of-ARs histogram, we walk
# the chronologically-ordered `timeline` and identify runs of consecutive
# samples whose AR stays within SEGMENT_AR_TOLERANCE. A run of length
# ≥ MIN_SEGMENT_SAMPLES is a confirmed AR segment. A lone outlier reading
# flanked by different ARs on both sides is rejected as cropdetect noise
# regardless of how confident any single plausibility check looked.
# --------------------------------------------------------------------------


@dataclass
class Segment:
    start_sec: int  # first sample's timestamp
    end_sec: int  # last sample's timestamp (inclusive)
    ar_median: float
    width: int  # most common crop across the segment's samples
    height: int
    sample_count: int
    # The runtime the segment stands for, see with_spans. Zero until assigned.
    span_start_sec: int = 0
    span_end_sec: int = 0

    @property
    def span_sec(self) -> int:
        return self.span_end_sec - self.span_start_sec


def analysis_window(duration: int) -> tuple[int, int]:
    """The part of the file that is sampled, ignoring begin/end pct."""
    start = int(duration * IGNORE_BEGINNING_PCT / 100)
    end = int(duration * (1 - IGNORE_END_PCT / 100))
    return start, end


def with_spans(
    segments: list[Segment], window_start: int, window_end: int
) -> list[Segment]:
    """Copies of the segments with the runtime between them split at the
    midpoint of the gap between each pair of neighbours; the first starts at
    the window start and the last ends at the window end. Only the segments
    given take part, so a dropped inset's runtime goes to its neighbours."""
    if not segments:
        return []
    bounds = [min(window_start, segments[0].start_sec)]
    for previous, following in pairwise(segments):
        bounds.append((previous.end_sec + following.start_sec) // 2)
    bounds.append(max(window_end, segments[-1].end_sec))
    return [
        replace(seg, span_start_sec=start, span_end_sec=end)
        for seg, (start, end) in zip(segments, pairwise(bounds), strict=True)
    ]


def detect_segments(vi: VideoInfo) -> list[Segment]:
    """Walk vi.timeline, return confirmed (≥MIN_SEGMENT_SAMPLES) AR segments."""
    segments: list[Segment] = []
    if not vi.timeline:
        return segments
    tl = sorted(vi.timeline)  # timeline is in sampling order, not timestamp order

    i = 0
    n = len(tl)
    while i < n:
        j = i
        while j + 1 < n and abs(tl[j + 1][1] - tl[j][1]) < SEGMENT_AR_TOLERANCE:
            j += 1
        count = j - i + 1
        if count >= MIN_SEGMENT_SAMPLES:
            ars = [tl[k][1] for k in range(i, j + 1)]
            ars.sort()
            median = ars[len(ars) // 2]
            crops = Counter((tl[k][2], tl[k][3]) for k in range(i, j + 1))
            width, height = crops.most_common(1)[0][0]
            segments.append(
                Segment(
                    start_sec=tl[i][0],
                    end_sec=tl[j][0],
                    ar_median=median,
                    width=width,
                    height=height,
                    sample_count=count,
                ),
            )
        i = j + 1

    return segments


def drop_insets(segments: list[Segment]) -> list[Segment]:
    """Remove segments windowboxed inside the primary segment's crop."""
    primary = max(segments, key=lambda seg: seg.sample_count)
    max_w = primary.width * (1 - INSET_TOLERANCE_PCT / 100)
    max_h = primary.height * (1 - INSET_TOLERANCE_PCT / 100)
    kept: list[Segment] = []
    for seg in segments:
        if seg.width < max_w and seg.height < max_h:
            logger.debug(
                "inset: dropping %dx%d segment at %ds (primary %dx%d)",
                seg.width,
                seg.height,
                seg.start_sec,
                primary.width,
                primary.height,
            )
            continue
        kept.append(seg)
    return kept


def frame_aspect(
    detected: list[DetectedAR],
    container_width: int,
    container_height: int,
) -> float | None:
    """The AR whose crop encloses every other detected crop, or None."""
    tol = 1 + INSET_TOLERANCE_PCT / 100
    for d in sorted(detected, key=lambda d: -d.percentage):
        if not all(
            o.width <= d.width * tol and o.height <= d.height * tol for o in detected
        ):
            continue
        is_container = (
            d.width * tol >= container_width and d.height * tol >= container_height
        )
        if is_container and d.percentage * 100 < FRAME_MIN_PCT:
            logger.debug(
                "frame: ignoring full-container %dx%d at %.0f%%",
                d.width,
                d.height,
                d.percentage * 100,
            )
            return None
        return d.aspect
    return None


# --------------------------------------------------------------------------
# roundAR
# --------------------------------------------------------------------------


def round_ar(ar: float) -> float:
    aspect_ratios = ASPECT_RATIOS
    for i in range(len(aspect_ratios) - 1):
        threshold = math.sqrt(aspect_ratios[i] * aspect_ratios[i + 1])
        if ar < threshold:
            return aspect_ratios[i]
    return aspect_ratios[-1]


@dataclass
class Summary:
    segments: list[Segment]  # after drop_insets, with spans
    rounded: dict[float, int]  # runtime seconds per snapped AR
    detected: list[DetectedAR]
    widest_aspect: float
    primary_aspect: float


def summarize_segments(segments: list[Segment], vi: VideoInfo) -> Summary:
    """Drop insets, snap each segment's median AR to ASPECT_RATIOS and pick
    the primary and widest."""
    segments = with_spans(drop_insets(segments), *analysis_window(vi.duration))

    # Aggregate runtime across segments that snap to the same AR, and keep
    # the longest contributing segment so the UI can show what was actually
    # measured.
    rounded: Counter[float] = Counter()
    largest: dict[float, Segment] = {}
    for seg in segments:
        snapped = round_ar(seg.ar_median)
        rounded[snapped] += seg.span_sec
        if snapped not in largest or seg.span_sec > largest[snapped].span_sec:
            largest[snapped] = seg

    total_runtime = sum(rounded.values())
    widest_aspect = max(rounded)
    detected = [
        DetectedAR(
            aspect=ar,
            percentage=seconds / total_runtime,
            measured=largest[ar].ar_median,
            width=largest[ar].width,
            height=largest[ar].height,
        )
        for ar, seconds in sorted(rounded.items(), key=lambda kv: -kv[0])
    ]
    frame = frame_aspect(detected, vi.width, vi.height)
    primary_aspect = (
        frame if frame is not None else max(rounded, key=lambda k: rounded[k])
    )
    return Summary(
        segments=segments,
        rounded=rounded,
        detected=detected,
        widest_aspect=widest_aspect,
        primary_aspect=primary_aspect,
    )


# --------------------------------------------------------------------------
# Minority-AR recheck
#
# cropdetect reduces each line to one integer mean and compares it with one
# absolute limit per file. Two things that discards:
#   * A bar is flat whatever its level. A teal-graded night scene lifts a
#     pillarbox from 16 to 19-20, over the film's dark level, and the sample
#     reads wider than the film (EO: 1.78 inside 1.43).
#   * A matte edge is uniform along the line; a film edge, iris or vignette
#     is not. Flat black beside a Super 8 insert's ragged edge reads as a
#     bar and the sample reads narrower than the film (Paris, Texas: 1.54
#     inside 1.78).
# Both are minority readings, so once a file reads as multi-AR the samples
# of its minority ARs are re-decoded with the luma plane and each side is
# rechecked. Where cropdetect found no bar, the lifted-bar check looks for
# one: outer lines that are flat and dark, with a hard step to the picture,
# move the edge in. Where cropdetect found a bar, the matte check measures
# the step at its edge: a ramp means the dark region is content, and the
# sample is rejected, unless the opposite side of the same axis is a hard
# matte. One mask cuts both sides of a pillarbox or letterbox, so a hard
# matte opposite means the ramp is dark picture beside the same mask, not a
# film edge; a film edge, iris or vignette ramps on both sides.
# Majority samples are never re-read, so a correct majority cannot be
# damaged, and single-AR files never reach this code.
#
# Per side, with bf(i) the fraction of line i brighter than the bar level
# plus EDGE_MARGIN and e the edge under test:
#     plateau(e) = bf(e + FAR) - bf(e - 1)
#     step(e)    = (max(bf(e), bf(e + 1)) - bf(e - 1)) / plateau(e)
# --------------------------------------------------------------------------

SIDES = ("L", "R", "T", "B")
OPPOSITE = {"L": "R", "R": "L", "T": "B", "B": "T"}


class LumaPlane:
    """A decoded luma plane read the way cropdetect scans it: whole rows or
    whole columns, counted inward from one side."""

    def __init__(self, raw: bytes, width: int, height: int, bit_depth: int) -> None:
        self.width = width
        self.height = height
        self.unit = 1 << (bit_depth - 8)
        self.full_scale = 1 << bit_depth
        count = width * height
        self.data: bytes | array[int]
        if bit_depth > 8:
            self.data = array("H")
            self.data.frombytes(raw[: count * 2])
        else:
            self.data = raw[:count]

    def dim(self, side: str) -> int:
        return self.width if side in "LR" else self.height

    def line(self, side: str, i: int) -> bytes | array[int]:
        w, h = self.width, self.height
        if side == "L":
            return self.data[i::w]
        if side == "R":
            return self.data[w - 1 - i :: w]
        if side == "T":
            return self.data[i * w : (i + 1) * w]
        return self.data[(h - 1 - i) * w : (h - i) * w]


def nearest_rank(ordered: list[int], p: float) -> int:
    return ordered[min(len(ordered) - 1, int(p * len(ordered)))]


def first_bright_line(plane: LumaPlane, side: str, limit: int) -> int | None:
    """cropdetect's scan: the first line before the midpoint whose integer
    mean exceeds `limit`."""
    for i in range(plane.dim(side) // 2):
        line = plane.line(side, i)
        if sum(line) // len(line) > limit:
            return i
    return None


def bright_fraction(plane: LumaPlane, side: str, i: int, threshold: int) -> float:
    if i < 0 or i >= plane.dim(side):
        return 0.0
    line = plane.line(side, i)
    return sum(1 for v in line if v > threshold) / len(line)


@dataclass
class EdgeProfile:
    bf: tuple[float, ...]  # at e-1, e, e+1, e+far
    plateau: float
    step: float


def edge_profile(
    plane: LumaPlane, side: str, edge: int, threshold: int, far: int
) -> EdgeProfile:
    bf = tuple(
        bright_fraction(plane, side, i, threshold)
        for i in (edge - 1, edge, edge + 1, edge + far)
    )
    plateau = bf[3] - bf[0]
    step = (max(bf[1], bf[2]) - bf[0]) / plateau if plateau > 0 else math.nan
    return EdgeProfile(bf=bf, plateau=plateau, step=step)


@dataclass
class SideResult:
    edge: int | None  # the edge to use; None rejects the sample
    hard_matte: bool = False  # cropdetect's bar ends in a hard edge


def recheck_side(
    plane: LumaPlane,
    side: str,
    edge: int,
    opposite_edge: int,
    t_sec: int,
    pass_label: str,
) -> SideResult:
    """Recheck one side of a cropdetect reading against the plane. The result
    specifies the edge to use: `edge` to keep the side as read, further in when
    a lifted bar is found past it, or None when the bar's edge is content, not
    a matte."""
    dim = plane.dim(side)
    u = plane.unit
    min_bar = max(4, round(MIN_BAR_PCT / 100 * dim))
    far_nominal = round(FAR_LINES_PER_1920 * plane.width / 1920)
    strip: list[int] = []
    for i in range(OUTER_LINES):
        strip.extend(plane.line(side, i))
    strip.sort()
    p10 = nearest_rank(strip, 0.10)
    p90 = nearest_rank(strip, 0.90)
    threshold = p90 + EDGE_MARGIN * u
    prefix = f"{pass_label}: t={t_sec}s {side}: edge={edge} level={p90 / u:.1f}"

    if edge < min_bar:
        # Lifted-bar check: cropdetect found no bar, or a sliver. If the outer
        # lines are flat and dark they are a bar whatever its level; scan again
        # with their own level as the limit and demand a hard step onto a real
        # plateau.
        if p90 - p10 > FLAT_SPREAD * u:
            logger.debug(
                "%s spread=%.1f lifted-bar: not flat, keep", prefix, (p90 - p10) / u
            )
            return SideResult(edge)
        if p90 > LIFTED_BAR_MAX_PCT / 100 * plane.full_scale:
            logger.debug("%s lifted-bar: not dark, keep", prefix)
            return SideResult(edge)
        cand = first_bright_line(plane, side, threshold)
        if cand is None:
            logger.debug("%s lifted-bar: no edge before midpoint, keep", prefix)
            return SideResult(edge)
        far = min(far_nominal, (dim - cand - opposite_edge) // 2)
        if far < FAR_MIN_LINES:
            logger.debug(
                "%s lifted-bar: candidate=%d far=%d inconclusive, keep",
                prefix,
                cand,
                far,
            )
            return SideResult(edge)
        ep = edge_profile(plane, side, cand, threshold, far)
        if ep.plateau < A_MIN_PLATEAU or ep.step < STEP_HARD:
            decision = "keep"
        elif cand < min_bar:
            decision = "sliver, keep"
        else:
            decision = "narrow"
        logger.debug(
            "%s lifted-bar: candidate=%d bf=%s plateau=%.2f step=%.2f far=%d %s",
            prefix,
            cand,
            [round(b, 3) for b in ep.bf],
            ep.plateau,
            ep.step,
            far,
            decision,
        )
        return SideResult(cand if decision == "narrow" else edge)

    # Matte check: cropdetect found a bar. Its edge steps if it is a matte and
    # ramps if the dark region is content, in which case the true edge is
    # unknowable from this frame; never widen to the container, a matte
    # could hide in the flat black.
    far = min(far_nominal, (dim - edge - opposite_edge) // 2)
    if far < FAR_MIN_LINES:
        logger.debug("%s matte: far=%d inconclusive, keep", prefix, far)
        return SideResult(edge)
    ep = edge_profile(plane, side, edge, threshold, far)
    if ep.plateau < B_MIN_PLATEAU:
        decision = "inconclusive, keep"
    elif ep.step >= STEP_HARD:
        decision = "hard edge, keep"
    else:
        decision = "ragged edge, reject"
    logger.debug(
        "%s matte: bf=%s plateau=%.2f step=%.2f far=%d %s",
        prefix,
        [round(b, 3) for b in ep.bf],
        ep.plateau,
        ep.step,
        far,
        decision,
    )
    if decision == "ragged edge, reject":
        return SideResult(None)
    return SideResult(edge, hard_matte=decision == "hard edge, keep")


def judge_crop(
    plane: LumaPlane,
    x1: int,
    x2: int,
    y1: int,
    y2: int,
    t_sec: int,
    pass_label: str,
) -> tuple[int, int, int, int] | None:
    """Recheck all four sides of a raw cropdetect reading. Returns the
    edges to use, or None when a side rejects the sample and the opposite
    side is not a hard matte."""
    w, h = plane.width, plane.height
    edges = {"L": x1, "R": w - 1 - x2, "T": y1, "B": h - 1 - y2}
    results = {
        side: recheck_side(
            plane, side, edges[side], edges[OPPOSITE[side]], t_sec, pass_label
        )
        for side in SIDES
    }
    result: dict[str, int] = {}
    for side in SIDES:
        edge = results[side].edge
        if edge is None and results[OPPOSITE[side]].hard_matte:
            logger.debug(
                "%s: t=%ds %s: hard matte opposite, keep", pass_label, t_sec, side
            )
            edge = edges[side]
        if edge is None:
            return None
        result[side] = edge
    return (result["L"], w - 1 - result["R"], result["T"], h - 1 - result["B"])


def rounded_crop(x1: int, x2: int, y1: int, y2: int) -> tuple[int, int]:
    """cropdetect's round=2: x and y round up to even, w and h down."""
    x = (x1 + 1) & ~1
    y = (y1 + 1) & ~1
    return (x2 - x + 1) & ~1, (y2 - y + 1) & ~1


async def recheck_sample(
    path: Path,
    vi: VideoInfo,
    entry: Sample,
    pass_label: str,
) -> Sample | None:
    """Re-decode one minority sample with its luma plane and recheck each
    side. Returns the timeline entry to keep, narrowed when a side found a
    lifted bar, or None to reject it. Anything that stops the frame being
    re-read as it was first scored leaves the entry as it is."""
    t_sec, _, width, height = entry
    raw, stderr = await scan_plane(path, t_sec, vi.dark_level, pass_label)
    m = P_SAMPLE.search(stderr)
    if m is None:
        logger.debug("%s: t=%ds no cropdetect line, keep", pass_label, t_sec)
        return entry
    x1, x2, y1, y2, w, h = (int(g) for g in m.groups())
    if (w, h) != (width, height):
        logger.debug(
            "%s: t=%ds re-decode read %dx%d, sample was %dx%d, keep",
            pass_label,
            t_sec,
            w,
            h,
            width,
            height,
        )
        return entry
    if x1 > x2 or y1 > y2:
        return entry
    needed = vi.width * vi.height * (1 if vi.bit_depth == 8 else 2)
    if len(raw) < needed:
        logger.debug(
            "%s: t=%ds plane is %d bytes, need %d, keep",
            pass_label,
            t_sec,
            len(raw),
            needed,
        )
        return entry
    plane = LumaPlane(raw, vi.width, vi.height, vi.bit_depth)
    edges = judge_crop(plane, x1, x2, y1, y2, t_sec, pass_label)
    if edges is None:
        logger.debug("%s: t=%ds reject", pass_label, t_sec)
        return None
    if edges == (x1, x2, y1, y2):
        return entry
    nx1, nx2, ny1, ny2 = edges
    nw, nh = rounded_crop(nx1, nx2, ny1, ny2)
    if not plausible_crop(nx1, nx2, ny1, ny2, nw, nh, vi, pass_label):
        logger.debug("%s: t=%ds narrowed crop implausible, reject", pass_label, t_sec)
        return None
    _, ar_calculated = calculated_ar(nw, nh, vi.ar_sample)
    logger.debug(
        "%s: t=%ds narrow: %dx%d → %dx%d ar=%s",
        pass_label,
        t_sec,
        width,
        height,
        nw,
        nh,
        ar_calculated,
    )
    return (t_sec, ar_calculated, nw, nh)


async def recheck_minority(
    path: Path, vi: VideoInfo, summary: Summary
) -> tuple[int, int]:
    """Re-read every sample of every kept segment whose snapped AR is not the
    majority. Returns (decodes, samples narrowed or rejected)."""
    pass_label = "R"
    majority = max(summary.rounded, key=lambda ar: summary.rounded[ar])
    spans = [
        (seg.start_sec, seg.end_sec)
        for seg in summary.segments
        if round_ar(seg.ar_median) != majority
    ]
    indices = [
        i
        for i, entry in enumerate(vi.timeline)
        if any(start <= entry[0] <= end for start, end in spans)
    ]
    logger.info(
        "%s: rechecking %d minority sample(s) against majority %.2f",
        pass_label,
        len(indices),
        majority,
    )
    results: dict[int, Sample | None] = {}
    for i in indices:
        entry = vi.timeline[i]
        try:
            result = await recheck_sample(path, vi, entry, pass_label)
        except Exception as exc:
            logger.debug("%s: sample error at %ds: %s", pass_label, entry[0], exc)
            continue
        if result != entry:
            results[i] = result
    if results:
        timeline: list[Sample] = []
        for i, entry in enumerate(vi.timeline):
            if i not in results:
                timeline.append(entry)
                continue
            result = results[i]
            if result is None:
                vi.sample_count -= 1
                vi.rejected.append(entry)
            else:
                timeline.append(result)
                vi.narrowed[entry[0]] = entry
        vi.timeline = timeline
    return len(indices), len(results)


def summarize_after_recheck(vi: VideoInfo, before: Summary) -> Summary:
    """Rebuild runs from the adjusted timeline. A recheck can remove or
    merge ARs, never introduce one: a rebuilt segment whose snapped AR was
    absent before (two lone same-AR samples bracketing a fully rejected
    run) is dropped."""
    kept: list[Segment] = []
    for seg in detect_segments(vi):
        snapped = round_ar(seg.ar_median)
        if snapped in before.rounded:
            kept.append(seg)
        else:
            logger.debug(
                "R: dropping new %.2f segment at %ds (%d samples)",
                snapped,
                seg.start_sec,
                seg.sample_count,
            )
    if not kept:
        logger.warning("R: no segment survived the recheck; keeping first result")
        return before
    return summarize_segments(kept, vi)


# --------------------------------------------------------------------------
# Stored timeline
#
# Every sample is kept in the ardetector row as JSON so segments, insets,
# snapping and the primary can be rebuilt from the database by the functions
# above without decoding the file again. Samples the recheck narrowed carry
# their first reading under "orig"; samples it rejected are kept with their
# reading and "rejected": true, and every rebuild skips them.
# --------------------------------------------------------------------------


def timeline_json(vi: VideoInfo) -> dict:
    samples: list[dict] = []
    for t, ar, w, h in vi.timeline:
        sample: dict = {"t": t, "ar": ar, "w": w, "h": h}
        if t in vi.narrowed:
            _, orig_ar, orig_w, orig_h = vi.narrowed[t]
            sample["orig"] = {"ar": orig_ar, "w": orig_w, "h": orig_h}
        samples.append(sample)
    samples.extend(
        {"t": t, "ar": ar, "w": w, "h": h, "rejected": True}
        for t, ar, w, h in vi.rejected
    )
    samples.sort(key=lambda s: s["t"])
    return {
        "width": vi.width,
        "height": vi.height,
        "duration": vi.duration,
        "sar": vi.ar_sample,
        "bit_depth": vi.bit_depth,
        "dark_level": vi.dark_level,
        "samples": samples,
    }


def timeline_from_json(data: dict) -> VideoInfo:
    """A VideoInfo whose timeline holds the stored samples that were not
    rejected, enough for detect_segments and summarize_segments."""
    return VideoInfo(
        width=data["width"],
        height=data["height"],
        duration=data["duration"],
        bit_depth=data["bit_depth"],
        dark_level=data["dark_level"],
        ar_sample=data["sar"],
        timeline=[
            (s["t"], s["ar"], s["w"], s["h"])
            for s in data["samples"]
            if not s.get("rejected")
        ],
    )


@dataclass(frozen=True)
class TimelineSegment:
    start_sec: int
    end_sec: int
    aspect: float  # snapped
    measured: float
    width: int
    height: int
    inset: bool
    rejected: bool  # a run of samples the recheck rejected

    @property
    def duration_sec(self) -> int:
        return self.end_sec - self.start_sec


# How a run in the timeline table relates to the ratio list: a presented
# run counts toward it, an inset or a rejected run is shown but does not.
PRESENTED, INSET, REJECTED = "presented", "inset", "rejected"


def merge_same_aspect(
    runs: list[tuple[Segment, str]],
) -> list[tuple[Segment, str]]:
    """Join neighbouring runs of the same kind that snap to the same AR: a
    lone reading between two runs of one ratio is not a boundary. The joined
    run keeps the measurement of the larger run."""
    merged: list[tuple[Segment, str]] = []
    for seg, kind in runs:
        if merged:
            previous, previous_kind = merged[-1]
            if previous_kind == kind and round_ar(previous.ar_median) == round_ar(
                seg.ar_median
            ):
                larger = max(previous, seg, key=lambda run: run.sample_count)
                merged[-1] = (
                    replace(
                        larger,
                        start_sec=previous.start_sec,
                        end_sec=seg.end_sec,
                        sample_count=previous.sample_count + seg.sample_count,
                    ),
                    kind,
                )
                continue
        merged.append((seg, kind))
    return merged


def stored_segments(data: dict) -> list[TimelineSegment]:
    """The stored timeline rebuilt into runtime-ordered segments, each
    spanning the runtime it stands for. Insets and runs of rejected samples
    are included and marked, so an insert the ratio list leaves out is still
    on the page."""
    vi = timeline_from_json(data)
    segments = detect_segments(vi)
    if not segments:
        return []
    kept = {id(seg) for seg in drop_insets(segments)}
    rejected = detect_segments(
        replace(
            vi,
            timeline=[
                (s["t"], s["ar"], s["w"], s["h"])
                for s in data["samples"]
                if s.get("rejected")
            ],
        )
    )
    runs = merge_same_aspect(
        sorted(
            [(seg, PRESENTED if id(seg) in kept else INSET) for seg in segments]
            + [(seg, REJECTED) for seg in rejected],
            key=lambda run: run[0].start_sec,
        )
    )
    spans = with_spans([seg for seg, _ in runs], *analysis_window(vi.duration))
    return [
        TimelineSegment(
            start_sec=span.span_start_sec,
            end_sec=span.span_end_sec,
            aspect=round_ar(seg.ar_median),
            measured=seg.ar_median,
            width=seg.width,
            height=seg.height,
            inset=kind == INSET,
            rejected=kind == REJECTED,
        )
        for (seg, kind), span in zip(runs, spans, strict=True)
    ]


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def initial_sample_times(duration: int) -> list[int]:
    """~INITIAL_SAMPLE_COUNT uniform sample times ignoring begin/end pct."""
    start, end = analysis_window(duration)
    span = max(end - start, 0)
    if span <= 0:
        return []
    interval = span / INITIAL_SAMPLE_COUNT
    times = [start + round(i * interval) for i in range(INITIAL_SAMPLE_COUNT)]
    return [t for t in times if t < end]


def find_orphans(timeline: list[Sample]) -> list[int]:
    """Indices of samples whose AR differs from both neighbours (or the one
    neighbour they have, for endpoints). Orphans are candidates for
    bisect-around refinement — either a real brief AR segment or noise."""
    orphans: list[int] = []
    n = len(timeline)
    for i in range(n):
        prev_close = (
            i > 0 and abs(timeline[i][1] - timeline[i - 1][1]) < SEGMENT_AR_TOLERANCE
        )
        next_close = (
            i < n - 1
            and abs(timeline[i][1] - timeline[i + 1][1]) < SEGMENT_AR_TOLERANCE
        )
        if not prev_close and not next_close:
            orphans.append(i)
    return orphans


def boundary_midpoints(timeline: list[Sample], sampled: set[int]) -> set[int]:
    """One new time per adjacent pair of a sorted timeline whose ARs differ
    and which are more than BOUNDARY_GAP_SEC apart. Times already requested
    inside the gap (samples that failed plausibility) split it into
    stretches; the longest stretch is bisected if it is itself over the
    floor, so a rejected midpoint is followed by the quarter points."""
    midpoints: set[int] = set()
    for (t_before, ar_before, _, _), (t_after, ar_after, _, _) in pairwise(timeline):
        if (
            abs(ar_after - ar_before) < SEGMENT_AR_TOLERANCE
            or t_after - t_before <= BOUNDARY_GAP_SEC
        ):
            continue
        inside = sorted(t for t in sampled if t_before < t < t_after)
        start, end = max(
            pairwise([t_before, *inside, t_after]),
            key=lambda stretch: stretch[1] - stretch[0],
        )
        if end - start > BOUNDARY_GAP_SEC:
            midpoints.add((start + end) // 2)
    return midpoints


async def sample_at(
    path: Path,
    vi: VideoInfo,
    times: list[int],
    sampled: set[int],
    pass_label: str,
) -> int:
    """Sample at the given timestamps, skipping any already in `sampled`."""
    attempts = 0
    for t in times:
        if t in sampled:
            continue
        sampled.add(t)
        attempts += 1
        try:
            t_clamped = min(t, vi.duration - SAMPLE_DURATION)
            result = await scan_sample(path, t_clamped, vi.dark_level, pass_label)
            parse_sample(result, t_clamped, vi, pass_label)
        except Exception as exc:
            logger.debug("%s: sample error at %ds: %s", pass_label, t, exc)
    return attempts


async def full_decode_pass(path: Path, vi: VideoInfo) -> int:
    """Decode the file end-to-end, parsing one cropdetect line per second.

    Used when input-seek sampling produces no AR segment — typically because
    the container's seek index lands ffmpeg mid-NAL-unit, returning concealed
    frames whose cropdetect output is garbage. Decoding linearly avoids the
    seek entirely, at the cost of a full decode (≈45s for a 22-min 720p ep,
    proportional to runtime).
    """
    pass_label = "F"
    # framestep operates after decode, so it doesn't speed up the work — it
    # only thins the cropdetect output we have to parse. ~24 = 1 sample/sec
    # at typical frame rates.
    framestep = 24
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-an",
        "-dn",
        "-sn",
        "-i",
        str(path),
        "-vf",
        (
            # framestep before cropdetect: cropdetect logs metadata for every
            # frame it sees, so thin the input first.
            f"framestep={framestep},"
            f"cropdetect=limit={int(vi.dark_level)}:round=2:skip=0,"
            "signalstats,metadata=print"
        ),
        "-f",
        "null",
        "pipe:1",
    ]
    # Generous: a 2h movie can take a few minutes of wall time to decode.
    timeout = max(600.0, float(vi.duration))
    buf = await run_ffmpeg(argv, pass_label=pass_label, timeout=timeout)

    parsed = 0
    pending: re.Match[str] | None = None
    pending_satmax: float | None = None
    pending_satavg: float | None = None
    pending_yavg: float | None = None

    def emit() -> None:
        nonlocal parsed, pending, pending_satmax, pending_satavg, pending_yavg
        if pending is None:
            return
        crop_match = pending
        is_color = classify_is_color(pending_satmax, pending_satavg, pending_yavg)
        t_raw = float(crop_match.group(7))
        logger.debug(
            "%s: t=%.1fs chroma: SATMAX=%s SATAVG=%s YAVG=%s is_color=%s",
            pass_label,
            t_raw,
            pending_satmax,
            pending_satavg,
            pending_yavg,
            is_color,
        )
        pending = None
        pending_satmax = None
        pending_satavg = None
        pending_yavg = None
        parsed += 1
        if t_raw < 0:
            return
        record_sample(
            x1=int(crop_match.group(1)),
            x2=int(crop_match.group(2)),
            y1=int(crop_match.group(3)),
            y2=int(crop_match.group(4)),
            width=int(crop_match.group(5)),
            height=int(crop_match.group(6)),
            t_sec=int(t_raw),
            vi=vi,
            pass_label=pass_label,
            is_color=is_color,
        )

    # Walk lines so we can pair each cropdetect frame with the signalstats
    # metadata block that follows it for the same frame. The signalstats
    # keys can arrive in any order within the block.
    for line in buf.splitlines():
        crop = P_FULL_SAMPLE.search(line)
        if crop is not None:
            # Flush the previous frame (with whatever chroma readings arrived).
            emit()
            pending = crop
            continue
        if pending is None:
            continue
        sm = P_SATMAX.search(line)
        if sm is not None:
            try:
                pending_satmax = float(sm.group(1))
            except ValueError:
                pending_satmax = None
            continue
        sa = P_SATAVG.search(line)
        if sa is not None:
            try:
                pending_satavg = float(sa.group(1))
            except ValueError:
                pending_satavg = None
            continue
        ya = P_YAVG.search(line)
        if ya is not None:
            try:
                pending_yavg = float(ya.group(1))
            except ValueError:
                pending_yavg = None
    emit()
    logger.info(
        "%s: full-decode parsed=%d valid=%d",
        pass_label,
        parsed,
        vi.sample_count,
    )
    return parsed


async def detect(path: Path) -> DetectionResult:
    """Detect aspect ratio(s) via temporal-segment clustering. Raise on abort."""
    # TMM skips ISOs, but ffmpeg happily reads most DVD ISOs and modern
    # Blu-ray ISOs (when compiled with libbluray). Let ffprobe decide: it
    # errors out for unopenable files like any other failed probe.

    if IGNORE_BEGINNING_PCT + IGNORE_END_PCT > 90:
        msg = "ignore pct sum > 90"
        raise RuntimeError(msg)

    mi = await ffprobe_media_info(path)
    if mi is None:
        msg = "ffprobe failed"
        raise RuntimeError(msg)

    vi = VideoInfo(bit_depth=mi.bit_depth)

    dark_buf = await scan_dark_level(path, 0.0)
    parse_video_meta(dark_buf, mi, vi)
    parse_dark_level(dark_buf, vi)

    # dark-level cap → fallback.
    bit_depth_max = 1 << vi.bit_depth
    if vi.dark_level * 100 / bit_depth_max > DARK_LEVEL_MAX_PCT:
        vi.dark_level = java_round(bit_depth_max * DARK_LEVEL_PCT / 100)
        logger.debug(
            "0: dark_level fallback → %d (bit_depth=%d)",
            vi.dark_level,
            vi.bit_depth,
        )
    else:
        logger.debug(
            "0: dark_level first-frame → %d (bit_depth=%d)",
            vi.dark_level,
            vi.bit_depth,
        )

    if vi.duration <= 30:
        msg = f"duration too short ({vi.duration}s)"
        raise RuntimeError(msg)

    logger.debug(
        "0: resolution=%dx%d dur=%ds sar=%.4f bit_depth=%d dark_level=%d",
        vi.width,
        vi.height,
        vi.duration,
        vi.ar_sample,
        vi.bit_depth,
        vi.dark_level,
    )

    # Initial coarse pass.
    sampled: set[int] = set()
    pass_label = "1"
    initial_times = initial_sample_times(vi.duration)
    logger.info(
        "%s: initial sampling %d points over %ds",
        pass_label,
        len(initial_times),
        vi.duration,
    )
    sample_counter = await sample_at(path, vi, initial_times, sampled, pass_label)
    logger.info(
        "%s: initial done: attempts=%d valid=%d",
        pass_label,
        sample_counter,
        vi.sample_count,
    )

    # Bisect around orphans until resolved, gaps are too tight to refine,
    # or we hit the sample cap. Skipped when the initial pass yielded nothing —
    # bisection can't refine an empty timeline; the fallback handles it below.
    refine_pass = 1
    while vi.sample_count > 0 and vi.sample_count < SAMPLE_COUNT_MAX:
        timeline = sorted(vi.timeline)
        orphans = find_orphans(timeline)
        if not orphans:
            break

        midpoints: set[int] = set()
        for idx in orphans:
            t = timeline[idx][0]
            # Proximity probes at ±MIN_REFINEMENT_GAP_SEC — catches brief (≥30s)
            # segments that bisection alone would miss when the orphan is
            # flanked by distant neighbours.
            for offset in (-MIN_REFINEMENT_GAP_SEC, MIN_REFINEMENT_GAP_SEC):
                pt = t + offset
                if 0 < pt < vi.duration - SAMPLE_DURATION:
                    midpoints.add(pt)
            # Bisection of larger gaps.
            for neighbour_idx in (idx - 1, idx + 1):
                if 0 <= neighbour_idx < len(timeline):
                    t_other = timeline[neighbour_idx][0]
                    gap = abs(t_other - t)
                    if gap >= MIN_REFINEMENT_GAP_SEC * 2:
                        midpoints.add((t + t_other) // 2)
        midpoints -= sampled
        if not midpoints:
            break

        refine_pass += 1
        pass_label = str(refine_pass)
        budget = SAMPLE_COUNT_MAX - vi.sample_count
        sorted_midpoints = sorted(midpoints)[:budget]
        logger.info(
            "%s: bisecting around %d orphan(s): %d new sample(s)",
            pass_label,
            len(orphans),
            len(sorted_midpoints),
        )
        sample_counter += await sample_at(
            path,
            vi,
            sorted_midpoints,
            sampled,
            pass_label,
        )
        logger.info(
            "%s: pass done: total_valid=%d",
            pass_label,
            vi.sample_count,
        )

    # Place each AR change to within BOUNDARY_GAP_SEC by bisecting between
    # every adjacent pair of samples that disagree. Only files that already
    # read as multi-AR pay for this; single-AR files never enter.
    if len(detect_segments(vi)) > 1:
        while vi.sample_count < SAMPLE_COUNT_MAX:
            midpoints = boundary_midpoints(sorted(vi.timeline), sampled)
            if not midpoints:
                break
            refine_pass += 1
            pass_label = str(refine_pass)
            budget = SAMPLE_COUNT_MAX - vi.sample_count
            sorted_midpoints = sorted(midpoints)[:budget]
            logger.info(
                "%s: bisecting %d boundary gap(s): %d new sample(s)",
                pass_label,
                len(midpoints),
                len(sorted_midpoints),
            )
            sample_counter += await sample_at(
                path,
                vi,
                sorted_midpoints,
                sampled,
                pass_label,
            )
            logger.info(
                "%s: pass done: total_valid=%d",
                pass_label,
                vi.sample_count,
            )

    segments = detect_segments(vi)
    seek_unstable = not segments
    if not segments:
        # Some containers/encodes (notably certain Bluray-720p anime sources)
        # confuse ffmpeg's seek index — every -ss lands mid-NAL-unit and the
        # decoder emits concealed frames whose cropdetect output is junk.
        # Decode linearly instead.
        logger.info(
            "fallback: input-seek produced no segment (samples=%d);"
            " running full-decode pass",
            vi.sample_count,
        )
        sample_counter += await full_decode_pass(path, vi)
        segments = detect_segments(vi)
        if not segments:
            msg = (
                "no AR segment after full-decode fallback"
                if vi.sample_count > 0
                else "no valid samples even after full-decode"
            )
            raise RuntimeError(msg)

    summary = summarize_segments(segments, vi)
    if len(summary.rounded) > 1:
        if seek_unstable:
            # The samples' timestamps are from the linear decode, so seeking
            # to one does not land on the frame it scored.
            logger.info("R: skipped, samples came from the full-decode pass")
        else:
            decodes, changed = await recheck_minority(path, vi, summary)
            sample_counter += decodes
            if changed:
                summary = summarize_after_recheck(vi, summary)
    segments = summary.segments
    detected = summary.detected
    primary_aspect = summary.primary_aspect
    widest_aspect = summary.widest_aspect

    logger.debug(
        "segments (raw): %s",
        [
            (
                s.start_sec,
                s.end_sec,
                f"{s.ar_median:.6f}",
                f"{s.width}x{s.height}",
                s.sample_count,
            )
            for s in segments
        ],
    )
    logger.info(
        "detect %s: primary=%.2f widest=%.2f ARs=%d segments=%d"
        " samples=%d/%d passes=%d sar=%.4f",
        path,
        primary_aspect,
        widest_aspect,
        len(detected),
        len(segments),
        vi.sample_count,
        sample_counter,
        refine_pass,
        vi.ar_sample,
    )

    color_pct = vi.color_samples / vi.chroma_samples if vi.chroma_samples > 0 else None
    if color_pct is not None:
        logger.info(
            "detect %s: color=%.0f%% mono=%.0f%% (samples=%d)",
            path,
            color_pct * 100,
            (1 - color_pct) * 100,
            vi.chroma_samples,
        )

    return DetectionResult(
        primary_aspect=primary_aspect,
        widest_aspect=widest_aspect,
        detected=detected,
        duration=float(vi.duration),
        sar=vi.ar_sample,
        timeline=timeline_json(vi),
        color_pct=color_pct,
    )


def to_ardetector_row(path: Path, result: DetectionResult) -> Ardetector:
    return Ardetector.model_validate(
        {
            "video_path": str(path),
            "aspect_primary": result.primary_aspect,
            "aspect_widest": result.widest_aspect,
            "aspect_samples": json.dumps([asdict(d) for d in result.detected]),
            "color_pct": result.color_pct,
            "timeline": json.dumps(result.timeline),
        }
    )
