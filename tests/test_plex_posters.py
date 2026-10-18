"""Plex poster rendering and the stamping pass against a fake Plex."""

import asyncio
import io
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from PIL import Image

from usharr import database, http, plex, plex_posters, queries
from usharr.config import Config, PlexConfig, PosterStyleConfig
from usharr.models import VideoFile

EDITION = "Director's Cut"
STYLE = PosterStyleConfig()


def poster_bytes(fmt: str = "WEBP", size: tuple[int, int] = (1280, 1962)) -> bytes:
    image = Image.new("RGB", size, (40, 90, 160))
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


# --- rendering -------------------------------------------------------------


def test_render_marks_the_jpeg_and_keeps_the_size():
    source = poster_bytes()
    assert plex_posters.stamped_edition(source) is None

    jpeg = plex_posters.render(source, EDITION, STYLE)
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.format == "JPEG"
        assert image.size == (1280, 1962)
    assert plex_posters.stamped_edition(jpeg) == EDITION


def test_band_spans_the_full_width_of_a_non_2_3_poster():
    jpeg = plex_posters.render(poster_bytes("PNG", (1000, 1400)), EDITION, STYLE)
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.size == (1000, 1400)
        # Mid-band row; the source is a flat (40, 90, 160) fill.
        band_row = 1400 - STYLE.back_height * 1400 // 1500 // 2
        for x in (5, 995):
            pixel = image.getpixel((x, band_row))
            assert isinstance(pixel, tuple)
            assert pixel < (30, 60, 110)


def test_marker_records_the_exact_edition():
    jpeg = plex_posters.render(poster_bytes("PNG", (600, 900)), "Theatrical", STYLE)
    assert plex_posters.stamped_edition(jpeg) == "Theatrical"
    assert plex_posters.stamped_edition(jpeg) != "theatrical"


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("<<edition>>", "director's cut"),
        ("<<editionU>>", "DIRECTOR'S CUT"),
        ("<<editionL>>", "director's cut"),
        ("<<editionP>>", "Director's Cut"),
        ("Edition: <<editionU>>", "Edition: DIRECTOR'S CUT"),
    ],
)
def test_edition_text_variants(template: str, expected: str):
    assert plex_posters.edition_text(template, "director's cut") == expected


@pytest.mark.parametrize(
    "text", ["Cut", "A" * 40, "Ultimate Collector's Extended Edition 4K"]
)
def test_text_fits_inside_the_band(text: str):
    path = plex_posters.font_path(STYLE.font)
    font = plex_posters.fit_font(path, text, STYLE.font_size, STYLE.back_width)
    assert font.getlength(text) <= STYLE.back_width
    assert font.size >= plex_posters.MIN_FONT_SIZE


def test_short_text_keeps_the_configured_size():
    path = plex_posters.font_path(STYLE.font)
    font = plex_posters.fit_font(path, "Cut", STYLE.font_size, STYLE.back_width)
    assert font.size == STYLE.font_size


# --- the pass --------------------------------------------------------------


class FakePlex:
    """Enough of a Plex server for the pass: metadata, thumb bytes, uploads."""

    def __init__(self) -> None:
        self.thumbs: dict[str, str] = {}
        self.posters: dict[str, bytes | int] = {}
        self.requests: list[tuple[str, str]] = []
        self.uploads: dict[str, bytes] = {}

    def add(self, rating_key: str, poster: bytes | int) -> None:
        self.thumbs[rating_key] = f"/library/metadata/{rating_key}/thumb/100"
        self.posters[rating_key] = poster

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        parts = path.split("/")
        rating_key = parts[3]
        if request.method == "POST" and path.endswith("/posters"):
            self.uploads[rating_key] = request.content
            self.thumbs[rating_key] = f"/library/metadata/{rating_key}/thumb/200"
            self.posters[rating_key] = request.content
            return httpx.Response(200)
        if "/thumb/" in path:
            poster = self.posters[rating_key]
            if isinstance(poster, int):
                return httpx.Response(poster)
            return httpx.Response(200, content=poster)
        body = {"MediaContainer": {"Metadata": [{"thumb": self.thumbs[rating_key]}]}}
        return httpx.Response(200, json=body)

    def image_gets(self, rating_key: str) -> int:
        return sum(
            1
            for method, path in self.requests
            if method == "GET"
            and path.startswith(f"/library/metadata/{rating_key}/thumb/")
        )

    def posts(self, rating_key: str) -> int:
        return self.requests.count(("POST", f"/library/metadata/{rating_key}/posters"))


@pytest.fixture
def fake_plex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakePlex:
    fake = FakePlex()
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "usharr.db")
    monkeypatch.setattr(
        http,
        "shared_client",
        httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)),
    )
    return fake


async def seed_movie(
    rating_key: str, title: str, edition: str | None = EDITION
) -> None:
    path = f"/media/{title}.mkv"
    await VideoFile.objects.create(path=path, size_bytes=1, mtime_ns=1)
    await queries.upsert_plex_item(
        rating_key=rating_key,
        item_type="movie",
        title=title,
        edition=edition,
        local_path=path,
    )


async def run_sync(
    setup: Callable[[], Awaitable[None]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    style: PosterStyleConfig | None = STYLE,
) -> dict[str, tuple[str, str]]:
    """Run plex_posters.sync() against the fake and return the cache rows."""
    config = Config(plex=PlexConfig(posters=style))
    monkeypatch.setattr(plex_posters, "get_config", lambda: config)
    await database.connect()
    try:
        await plex.save_auth("tok", "http://plex.test:32400", "Test")
        await setup()
        await plex_posters.sync()
        rows = await queries.plex_posters_by_rating_key()
        return {key: (row.edition, row.thumb) for key, row in rows.items()}
    finally:
        await http.close()
        await database.close()


def test_provider_poster_is_stamped_and_uploaded(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", poster_bytes())

    async def setup() -> None:
        await seed_movie("1", "Alpha")

    rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 1
    assert plex_posters.stamped_edition(fake_plex.uploads["1"]) == EDITION
    assert rows == {"1": (EDITION, "/library/metadata/1/thumb/200")}


def test_marked_poster_is_not_uploaded_or_rendered(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", plex_posters.render(poster_bytes(), EDITION, STYLE))

    def boom(*args, **kwargs):
        msg = "render must not run on a stamped poster"
        raise AssertionError(msg)

    monkeypatch.setattr(plex_posters, "render", boom)

    async def setup() -> None:
        await seed_movie("1", "Alpha")

    rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 0
    assert rows == {"1": (EDITION, "/library/metadata/1/thumb/100")}


def test_cached_thumb_skips_the_image_download(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", poster_bytes())

    async def setup() -> None:
        await seed_movie("1", "Alpha")
        await queries.upsert_plex_poster(
            rating_key="1", edition=EDITION, thumb=fake_plex.thumbs["1"]
        )

    asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.image_gets("1") == 0
    assert fake_plex.posts("1") == 0


def test_changed_edition_invalidates_the_cache(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", poster_bytes())

    async def setup() -> None:
        await seed_movie("1", "Alpha")
        await queries.upsert_plex_poster(
            rating_key="1", edition="Theatrical", thumb=fake_plex.thumbs["1"]
        )

    rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 1
    assert rows["1"][0] == EDITION


def test_http_error_is_logged_and_the_pass_continues(
    fake_plex: FakePlex,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    fake_plex.add("1", 500)
    fake_plex.add("2", poster_bytes())

    async def setup() -> None:
        await seed_movie("1", "Alpha")
        await seed_movie("2", "Beta")

    with caplog.at_level("INFO", logger="usharr.plex_posters"):
        rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 0
    assert fake_plex.posts("2") == 1
    assert set(rows) == {"2"}
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "Alpha" in warnings[0]
    assert "(1)" in warnings[0]
    assert caplog.records[-1].message == "plex_posters: stamped=1 skipped=0 failed=1"


def test_movies_without_an_edition_are_ignored(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", poster_bytes())

    async def setup() -> None:
        await seed_movie("1", "Alpha", edition=None)

    assert asyncio.run(run_sync(setup, monkeypatch)) == {}
    assert fake_plex.requests == []


def test_absent_config_disables_the_pass(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", poster_bytes())

    async def setup() -> None:
        await seed_movie("1", "Alpha")

    assert asyncio.run(run_sync(setup, monkeypatch, style=None)) == {}
    assert fake_plex.requests == []
