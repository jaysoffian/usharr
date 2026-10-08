"""End-to-end aspect ratio detection on rendered videos.

Each test draws a 40 second 640x360 video with ffmpeg's generators: a
textured picture with black bars (luma 16) laid out by formula, so the
geometry and the bar levels are exact. The video is then run through
detect() the same way a library file is.
"""

import asyncio
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
    expression over X, Y and T (seconds). Lossless, so bar levels survive."""
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
