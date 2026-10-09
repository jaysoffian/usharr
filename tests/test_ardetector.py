"""End-to-end aspect ratio detection on rendered videos.

Each test draws a 40 second 640x360 video with ffmpeg's generators: a
textured picture with black bars (luma 16) laid out by formula, so the
geometry and the bar levels are exact. The video is then run through
detect() the same way a library file is.
"""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from usharr import ardetector

PICTURE = "60+mod(X*3+Y*7,20)"
# 1.43 inside 640x360: 516 wide, 62 px bars each side.
PILLARBOX = "lt(X,62)+gte(X,578)"
# 2.40 inside 640x360: 268 tall, 46 px bars top and bottom.
LETTERBOX = "lt(Y,46)+gte(Y,314)"


def render(path: Path, luma: str, bit_depth: int = 8) -> Path:
    """Render 40 s at 2 fps with each pixel's luma given by an ffmpeg
    expression over X, Y and T (seconds). Lossless, so bar levels survive,
    and every frame a keyframe, so a seek lands on the second it asks for."""
    pix_fmt = "yuv420p" if bit_depth == 8 else "yuv420p10le"
    mid = 1 << (bit_depth - 1)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "nullsrc=s=640x360:r=2:d=40",
            "-vf",
            f"format={pix_fmt},geq=lum='{luma}':cb={mid}:cr={mid}",
            "-c:v",
            "ffv1",
            "-g",
            "1",
            str(path),
        ],
        check=True,
    )
    return path


def detect(path: Path) -> ardetector.DetectionResult:
    return asyncio.run(ardetector.detect(path))


def aspects(result: ardetector.DetectionResult) -> list[float]:
    return [d.aspect for d in result.detected]


def test_single_ar_is_never_rechecked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def fail(*args: object):
        raise AssertionError("recheck ran on a single-AR file")

    monkeypatch.setattr(ardetector, "recheck_minority", fail)
    video = render(tmp_path / "pillarbox.mkv", f"if({PILLARBOX}, 16, {PICTURE})")
    result = detect(video)
    assert aspects(result) == [1.43]
    assert result.primary_aspect == 1.43


# A pillarboxed film whose bars lift from 16 to 20 for six seconds, as a
# graded night scene does. cropdetect reads those frames as full width.
LIFTED = f"if({PILLARBOX}, if(between(T,20,26), 20, 16), {PICTURE})"


def test_lifted_bars_are_still_bars(tmp_path: Path):
    result = detect(render(tmp_path / "lifted.mkv", LIFTED))
    assert aspects(result) == [1.43]
    assert result.primary_aspect == 1.43


def test_lifted_bars_at_10_bit(tmp_path: Path):
    luma = f"if({PILLARBOX}, if(between(T,20,26), 80, 64), 240+mod(X*3+Y*7,80))"
    result = detect(render(tmp_path / "lifted10.mkv", luma, bit_depth=10))
    assert aspects(result) == [1.43]


# A 16:9 film with six seconds of footage whose left and right edges wander
# by up to 40 px from row to row, like film edges, with flat black outside.
RAGGED = (
    "if(between(T,20,26),"
    f" if(gte(X,70+mod(Y*37,41))*lt(X,570-mod(Y*53,41)), {PICTURE}, 16),"
    f" {PICTURE})"
)


def test_ragged_edges_are_not_bars(tmp_path: Path):
    result = detect(render(tmp_path / "ragged.mkv", RAGGED))
    assert aspects(result) == [1.78]


def test_dark_picture_beside_a_matte_is_kept(tmp_path: Path):
    # A 1.78 film with six seconds of 1.37 pillarboxed footage whose picture
    # is dark along its left edge (most rows at 17, just under the bar level
    # plus margin), as old film clips and interviews often are. The left edge
    # measures as a ramp; the right is a hard matte, and that keeps it.
    luma = (
        "if(between(T,20,26),"
        f" if(lt(X,73)+gte(X,567), 16, if(lt(X,78)*gte(mod(Y,10),3), 17, {PICTURE})),"
        f" {PICTURE})"
    )
    result = detect(render(tmp_path / "darkedge.mkv", luma))
    assert aspects(result) == [1.78, 1.37]
    assert result.primary_aspect == 1.78


def test_real_ar_change_is_kept(tmp_path: Path):
    # A 16:9 film with eight seconds letterboxed to 2.40 behind hard mattes.
    luma = f"if(between(T,20,28)*({LETTERBOX}), 16, {PICTURE})"
    result = detect(render(tmp_path / "var.mkv", luma))
    assert aspects(result) == [2.40, 1.78]
    assert result.primary_aspect == 1.78
    assert result.widest_aspect == 2.40


# --- pure functions over the sample timeline --------------------------------


def sample_output(x1: int, x2: int, y1: int, y2: int, pts_time: float) -> str:
    w, h = x2 - x1 + 1, y2 - y1 + 1
    return (
        f"[Parsed_cropdetect_0 @ 0x1] x1:{x1} x2:{x2} y1:{y1} y2:{y2} w:{w} h:{h}"
        f" x:{x1} y:{y1} pts:0 t:0.0 limit:24.0 crop={w}:{h}:{x1}:{y1}\n"
        f"[Parsed_metadata_2 @ 0x2] frame:1    pts:0    pts_time:{pts_time}\n"
        "[Parsed_metadata_2 @ 0x2] lavfi.signalstats.YAVG=90.0\n"
        "[Parsed_metadata_2 @ 0x2] lavfi.signalstats.SATMAX=40.0\n"
    )


def test_sample_time_is_the_decoded_frame_time():
    vi = ardetector.VideoInfo(width=640, height=360, ar_sample=1.0)
    ardetector.parse_sample(sample_output(0, 639, 46, 313, -1.75), 1850, vi, "1")
    assert vi.timeline == [(1849, 2.38806, 640, 268)]
    assert ardetector.frame_time("no frame line", 1850) == 1850


def test_second_request_on_the_same_frame_is_dropped():
    vi = ardetector.VideoInfo(width=640, height=360, ar_sample=1.0)
    ardetector.parse_sample(sample_output(0, 639, 0, 359, -1.2), 1850, vi, "1")
    ardetector.parse_sample(sample_output(0, 639, 0, 359, -2.2), 1851, vi, "2")
    assert vi.timeline == [(1849, 1.777778, 640, 360)]
    assert vi.sample_count == 1
    # The dropped duplicate counts for nothing, chroma included.
    assert vi.chroma_samples == 1


def test_boundary_midpoints():
    timeline = [
        (100, 1.78, 640, 360),
        (160, 1.78, 640, 360),  # same AR as its neighbour: no midpoint
        (200, 2.39, 640, 268),  # differs, 40 s apart: 180
        (209, 1.78, 640, 360),  # differs, 9 s apart: at the floor already
        (300, 2.39, 640, 268),  # differs, 91 s apart: 254
        (400, 2.39, 640, 268),
        (424, 1.78, 640, 360),  # differs, 24 s apart: 412
    ]
    assert ardetector.boundary_midpoints(timeline, set()) == {180, 254, 412}
    # 254 was requested and rejected: bisect the longer half, 254-300, next.
    # 408 and 416 were too: every stretch of 400-424 is under the floor.
    assert ardetector.boundary_midpoints(timeline, {254, 408, 416}) == {180, 277}


def segment(start: int, end: int, ar: float, w: int, h: int, n: int):
    return ardetector.Segment(
        start_sec=start, end_sec=end, ar_median=ar, width=w, height=h, sample_count=n
    )


def test_shares_come_from_runtime_not_sample_counts():
    # A 1.78 film with a 2.40 letterbox from ~1000 s to ~1200 s; the boundary
    # stage put many samples around the change, and an 8-sample windowboxed
    # inset splits the first 1.78 run.
    segments = [
        segment(100, 380, 1.777778, 640, 360, 10),
        segment(400, 500, 1.333333, 400, 300, 8),
        segment(520, 980, 1.777778, 640, 360, 10),
        segment(1000, 1200, 2.38806, 640, 268, 12),
        segment(1220, 3600, 1.777778, 640, 360, 20),
    ]
    vi = ardetector.VideoInfo(width=640, height=360, duration=4000)
    summary = ardetector.summarize_segments(segments, vi)
    spans = [(s.span_start_sec, s.span_end_sec) for s in summary.segments]
    # Window is 2 %..92 % of the duration; the inset's runtime goes to the
    # 1.78 runs on either side of it.
    assert spans == [(80, 450), (450, 990), (990, 1210), (1210, 3680)]
    assert [d.aspect for d in summary.detected] == [2.40, 1.78]
    shares = {d.aspect: round(d.percentage, 4) for d in summary.detected}
    assert shares == {2.40: round(220 / 3600, 4), 1.78: round(3380 / 3600, 4)}
    assert summary.rounded == {1.78: 3380, 2.40: 220}
    assert summary.primary_aspect == 1.78
    assert summary.widest_aspect == 2.40


def test_timeline_json_round_trip():
    vi = ardetector.VideoInfo(
        width=1920,
        height=1080,
        duration=5400,
        bit_depth=8,
        dark_level=24,
        ar_sample=1.0,
        timeline=[(200, 1.777778, 1920, 1080), (100, 1.433333, 1548, 1080)],
        narrowed={200: (200, 2.0, 1920, 960)},
        rejected=[(150, 1.54, 1664, 1080)],
    )
    stored = ardetector.timeline_json(vi)
    assert stored["samples"] == [
        {"t": 100, "ar": 1.433333, "w": 1548, "h": 1080},
        {"t": 150, "ar": 1.54, "w": 1664, "h": 1080, "rejected": True},
        {
            "t": 200,
            "ar": 1.777778,
            "w": 1920,
            "h": 1080,
            "orig": {"ar": 2.0, "w": 1920, "h": 960},
        },
    ]
    back = ardetector.timeline_from_json(json.loads(json.dumps(stored)))
    assert back.timeline == [(100, 1.433333, 1548, 1080), (200, 1.777778, 1920, 1080)]
    assert (back.width, back.height, back.duration) == (1920, 1080, 5400)
    assert (back.bit_depth, back.dark_level, back.ar_sample) == (8, 24, 1.0)


def test_stored_segments_skip_rejected_and_mark_insets():
    def sample(t: int, ar: float, w: int, h: int, **extra: object) -> dict:
        return {"t": t, "ar": ar, "w": w, "h": h, **extra}

    stored = {
        "width": 1920,
        "height": 1080,
        "duration": 1000,
        "sar": 1.0,
        "bit_depth": 8,
        "dark_level": 24,
        "samples": [
            sample(20, 1.777778, 1920, 1080),
            sample(100, 1.777778, 1920, 1080),
            sample(200, 1.777778, 1920, 1080),
            sample(300, 1.333333, 1200, 900),
            sample(320, 1.333333, 1200, 900),
            # Two rejected 2.39 readings: a segment if they were counted.
            sample(400, 2.38806, 1920, 804, rejected=True),
            sample(420, 2.38806, 1920, 804, rejected=True),
            sample(500, 1.777778, 1920, 1080),
            sample(600, 1.777778, 1920, 1080),
        ],
    }
    segments = ardetector.stored_segments(stored)
    assert [(s.start_sec, s.end_sec, s.aspect, s.inset) for s in segments] == [
        (20, 250, 1.78, False),
        (250, 410, 1.33, True),
        (410, 920, 1.78, False),
    ]
    assert segments[1].duration_sec == 160
    assert (segments[1].width, segments[1].height) == (1200, 900)


def test_stored_segments_merge_runs_split_by_a_lone_reading():
    def sample(t: int, ar: float, w: int, h: int) -> dict:
        return {"t": t, "ar": ar, "w": w, "h": h}

    stored = {
        "width": 1920,
        "height": 1080,
        "duration": 1000,
        "sar": 1.0,
        "bit_depth": 8,
        "dark_level": 24,
        "samples": [
            sample(20, 1.777778, 1920, 1080),
            sample(100, 1.777778, 1920, 1080),
            sample(200, 1.54, 1650, 1080),  # lone: not a run, not a boundary
            sample(300, 1.777778, 1920, 1080),
            sample(400, 1.777778, 1920, 1080),
            sample(500, 1.777778, 1920, 1080),
            sample(600, 2.38806, 1920, 804),
            sample(700, 2.38806, 1920, 804),
        ],
    }
    segments = ardetector.stored_segments(stored)
    assert [(s.start_sec, s.end_sec, s.aspect, s.inset) for s in segments] == [
        (20, 550, 1.78, False),
        (550, 920, 2.40, False),
    ]
