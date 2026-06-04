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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from usharr.models import Ardetector

logger = logging.getLogger(__name__)

# All the aspect ratios I've ever come across in actual use.
DEFAULT_AR_LIST: tuple[float, ...] = (
    1.33,
    1.37,
    1.43,
    1.50,
    1.56,
    1.66,
    1.78,
    1.85,
    1.90,
    2.00,
    2.20,
    2.35,
    2.40,
    2.55,
    2.66,
    2.76,
    3.00,
    4.00,
)

# Sampling schedule — coarse initial pass, then bisect around orphans.
# Single-AR films converge at INITIAL_SAMPLE_COUNT samples. Multi-AR films
# densify adaptively around detected transitions/orphans, up to SAMPLE_COUNT_MAX.
INITIAL_SAMPLE_COUNT = 60
SAMPLE_COUNT_MAX = 360  # wall-clock ceiling for heavy multi-AR films
# Stop bisecting around an orphan once neighbouring samples are within this
# many seconds — further refinement can't resolve sub-15s blips.
MIN_REFINEMENT_GAP_SEC = 15
SAMPLE_DURATION = 1  # used only as an end-of-file safety margin

# global parameters
IGNORE_BEGINNING_PCT = 2.0
IGNORE_END_PCT = 8.0
AR_SECONDARY_DELTA = 0.15
PLAUSI_WIDTH_PCT = 50.0
PLAUSI_HEIGHT_PCT = 40.0  # TMM default 60; lowered to admit 3.00 and 4.00 AR crops
PLAUSI_WIDTH_DELTA_PCT = 1.5
PLAUSI_HEIGHT_DELTA_PCT = 2.0
ROUND_UP = False
ROUND_UP_THRESHOLD_PCT = 4.0
DARK_LEVEL_PCT = 7.0
DARK_LEVEL_MAX_PCT = 13.0

# Temporal segment detection: consecutive samples whose AR readings differ by
# less than this count as the same AR segment. 0.075 = AR_SECONDARY_DELTA / 2
# — narrower than the inter-cluster suppression, wider than cropdetect jitter.
SEGMENT_AR_TOLERANCE = AR_SECONDARY_DELTA / 2

# A segment must contain at least this many consecutive samples to count as
# a real AR (vs. isolated cropdetect noise on scene transitions).
MIN_SEGMENT_SAMPLES = 2

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
P_DUR = re.compile(r"Duration:\s(\d\d:\d\d:\d\d\.\d\d),")


def java_round(x: float) -> int:
    """Java Math.round on a float: half-away-from-zero for positives."""
    return math.floor(x + 0.5)


@dataclass
class DetectedAR:
    aspect: float
    percentage: float


@dataclass
class DetectionResult:
    primary_aspect: float  # most-sampled post-snap AR
    widest_aspect: float  # widest post-snap AR
    detected: list[DetectedAR]
    duration: float
    sar: float
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

    # (timestamp_sec, ar_calculated) for every sample that passed plausibility,
    # in chronological order. Temporal-adjacency clustering uses this directly.
    timeline: list[tuple[int, float]] = field(default_factory=list)


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
    logger.debug("%s: %s", pass_label, " ".join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        msg = f"ffmpeg timed out after {timeout}s"
        raise RuntimeError(msg) from None
    if proc.returncode != 0:
        msg = f"ffmpeg exit {proc.returncode}"
        raise RuntimeError(msg)
    return stdout.decode("utf-8", errors="replace")


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
    black_left = x1
    black_right = abs(vi.width - x2 - 1)
    black_top = y1
    black_bottom = abs(vi.height - y2 - 1)

    ar_measured = (width / height) if height > 0 else 9.99
    # 10E5 in Java is 1e6 — round to 6 decimals.
    ar_calculated = java_round(ar_measured * vi.ar_sample * 1_000_000) / 1_000_000

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

    vi.timeline.append((t_sec, ar_calculated))
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
    record_sample(
        x1=int(m.group(1)),
        x2=int(m.group(2)),
        y1=int(m.group(3)),
        y2=int(m.group(4)),
        width=int(m.group(5)),
        height=int(m.group(6)),
        t_sec=t_sec,
        vi=vi,
        pass_label=pass_label,
        is_color=is_color,
    )


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
    start_sec: int
    end_sec: int  # last sample's timestamp (inclusive)
    ar_median: float
    sample_count: int


def detect_segments(vi: VideoInfo) -> list[Segment]:
    """Walk vi.timeline, return confirmed (≥MIN_SEGMENT_SAMPLES) AR segments."""
    segments: list[Segment] = []
    if not vi.timeline:
        return segments
    tl = vi.timeline  # already chronologically ordered by the sampling loop

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
            segments.append(
                Segment(
                    start_sec=tl[i][0],
                    end_sec=tl[j][0],
                    ar_median=median,
                    sample_count=count,
                ),
            )
        i = j + 1

    return segments


# --------------------------------------------------------------------------
# roundAR
# --------------------------------------------------------------------------


def round_ar_nearest(ar: float, ar_list: tuple[float, ...]) -> float:
    if len(ar_list) == 1:
        return ar_list[0]
    for i in range(len(ar_list) - 1):
        threshold = math.sqrt(ar_list[i] * ar_list[i + 1])
        if ar < threshold:
            return ar_list[i]
    return ar_list[-1]


def round_ar(ar: float, ar_list: tuple[float, ...]) -> float:
    if not ar_list:
        return java_round(ar * 100) / 100
    if ROUND_UP:
        for provided in ar_list:
            if abs(provided - ar) <= ROUND_UP_THRESHOLD_PCT / 100:
                return round_ar_nearest(ar, ar_list)
        best_delta = 999.0
        rounded = 999.0
        for provided in ar_list:
            delta = provided - ar
            if delta >= 0 and delta < best_delta:
                best_delta = delta
                rounded = provided
        if rounded == 999.0:
            rounded = ar_list[-1]
        return rounded
    return round_ar_nearest(ar, ar_list)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def initial_sample_times(duration: int) -> list[int]:
    """~INITIAL_SAMPLE_COUNT uniform sample times ignoring begin/end pct."""
    start = int(duration * IGNORE_BEGINNING_PCT / 100)
    end = int(duration * (1 - IGNORE_END_PCT / 100))
    span = max(end - start, 0)
    if span <= 0:
        return []
    interval = span / INITIAL_SAMPLE_COUNT
    times = [start + round(i * interval) for i in range(INITIAL_SAMPLE_COUNT)]
    return [t for t in times if t < end]


def find_orphans(timeline: list[tuple[int, float]]) -> list[int]:
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

    ar_list = tuple(sorted(DEFAULT_AR_LIST))

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

    segments = detect_segments(vi)
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

    # Snap each segment's median AR to the standard list, aggregate sample counts
    # across segments that snap to the same AR.
    rounded: dict[float, int] = {}
    for seg in segments:
        snapped = round_ar(seg.ar_median, ar_list)
        rounded[snapped] = rounded.get(snapped, 0) + seg.sample_count

    total_confirmed = sum(rounded.values())
    primary_aspect = max(rounded, key=lambda k: rounded[k])
    widest_aspect = max(rounded)
    detected = [
        DetectedAR(aspect=ar, percentage=count / total_confirmed)
        for ar, count in sorted(rounded.items(), key=lambda kv: -kv[0])
    ]

    logger.debug(
        "segments (raw): %s",
        [
            (s.start_sec, s.end_sec, f"{s.ar_median:.6f}", s.sample_count)
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
        }
    )
