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
FINGERPRINT = plex_posters.style_fingerprint(STYLE)


def poster_bytes(
    fmt: str = "WEBP",
    size: tuple[int, int] = (1280, 1962),
    fill: tuple[int, int, int] = (40, 90, 160),
) -> bytes:
    image = Image.new("RGB", size, fill)
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

TMDB_A = "https://image.tmdb.org/t/p/original/a.jpg"
TMDB_B = "https://image.tmdb.org/t/p/original/b.jpg"
UPLOAD_PATH = "/library/metadata/1/file?url=upload%3A%2F%2Fposters%2F0123abcd"
BASE_FILL = (200, 30, 30)
NO_CANDIDATE = "no usable poster candidate; select a poster in Plex"


class FakePlex:
    """Enough of a Plex server for the pass: metadata, posters, candidates, uploads."""

    def __init__(self) -> None:
        self.thumbs: dict[str, str] = {}
        self.posters: dict[str, bytes | int] = {}
        self.candidates: dict[str, list[dict]] = {}
        # Candidate bytes (or an HTTP status) by `key`: a server path or URL.
        self.files: dict[str, bytes | int] = {}
        self.requests: list[tuple[str, str]] = []
        self.request_headers: dict[str, httpx.Headers] = {}
        self.uploads: dict[str, bytes] = {}

    def add(self, rating_key: str, poster: bytes | int) -> None:
        self.thumbs[rating_key] = f"/library/metadata/{rating_key}/thumb/100"
        self.posters[rating_key] = poster

    def add_candidate(
        self,
        rating_key: str,
        key: str,
        content: bytes | int,
        *,
        candidate_key: str | None = None,
    ) -> None:
        self.candidates.setdefault(rating_key, []).append(
            {"ratingKey": candidate_key or key, "key": key, "selected": False}
        )
        self.files[key] = content

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.host != "plex.test":
            self.requests.append((request.method, url))
            self.request_headers[url] = request.headers
            return self.file(url)
        path = request.url.path
        self.requests.append((request.method, path))
        target = request.url.raw_path.decode()  # path plus query
        self.request_headers[target] = request.headers
        if target in self.files:
            return self.file(target)
        rating_key = path.split("/")[3]
        if request.method == "POST" and path.endswith("/posters"):
            self.uploads[rating_key] = request.content
            self.thumbs[rating_key] = f"/library/metadata/{rating_key}/thumb/200"
            self.posters[rating_key] = request.content
            return httpx.Response(200)
        if path.endswith("/posters"):
            body = {"MediaContainer": {"Metadata": self.candidates.get(rating_key, [])}}
            return httpx.Response(200, json=body)
        if "/thumb/" in path:
            poster = self.posters[rating_key]
            if isinstance(poster, int):
                return httpx.Response(poster)
            return httpx.Response(200, content=poster)
        body = {"MediaContainer": {"Metadata": [{"thumb": self.thumbs[rating_key]}]}}
        return httpx.Response(200, json=body)

    def file(self, key: str) -> httpx.Response:
        content = self.files[key]
        if isinstance(content, int):
            return httpx.Response(content)
        return httpx.Response(200, content=content)

    def fetched(self, key: str) -> bool:
        return key in self.request_headers

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


async def seed_stamped(
    fake_plex: FakePlex,
    edition: str = EDITION,
    *,
    style: str | None = FINGERPRINT,
    thumb: str | None = None,
) -> None:
    await seed_movie("1", "Alpha")
    await queries.upsert_plex_poster(
        rating_key="1",
        edition=edition,
        thumb=thumb or fake_plex.thumbs["1"],
        style=style,
    )


async def run_sync(
    setup: Callable[[], Awaitable[None]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    style: PosterStyleConfig | None = STYLE,
) -> dict[str, tuple[str, str, str | None]]:
    """Run plex_posters.sync() against the fake and return the cache rows."""
    config = Config(plex=PlexConfig(posters=style))
    monkeypatch.setattr(plex_posters, "get_config", lambda: config)
    await database.connect()
    try:
        await plex.save_auth("tok", "http://plex.test:32400", "Test")
        await setup()
        await plex_posters.sync()
        rows = await queries.plex_posters_by_rating_key()
        return {k: (r.edition, r.thumb, r.style) for k, r in rows.items()}
    finally:
        await http.close()
        await database.close()


def sample_outside_band(jpeg: bytes) -> tuple[int, ...]:
    with Image.open(io.BytesIO(jpeg)) as image:
        pixel = image.getpixel((image.width // 2, image.height // 4))
    assert isinstance(pixel, tuple)
    return pixel


def near(pixel: tuple[int, ...], colour: tuple[int, int, int]) -> bool:
    return all(abs(a - b) <= 6 for a, b in zip(pixel, colour, strict=True))


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.message for r in caplog.records]


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.message for r in caplog.records if r.levelname == "WARNING"]


STAMPED = {"1": (EDITION, "/library/metadata/1/thumb/200", FINGERPRINT)}
UNCHANGED = {"1": (EDITION, "/library/metadata/1/thumb/100", FINGERPRINT)}


def test_provider_poster_is_stamped_and_uploaded(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", poster_bytes())

    async def setup() -> None:
        await seed_movie("1", "Alpha")

    rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 1
    assert plex_posters.stamped_edition(fake_plex.uploads["1"]) == EDITION
    assert rows == STAMPED
    assert ("GET", "/library/metadata/1/posters") not in fake_plex.requests


def test_cached_thumb_skips_the_image_download(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", poster_bytes())

    async def setup() -> None:
        await seed_stamped(fake_plex)

    rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.image_gets("1") == 0
    assert fake_plex.posts("1") == 0
    assert rows == UNCHANGED


def test_marked_poster_with_a_stale_thumb_refreshes_the_row(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", plex_posters.render(poster_bytes(), EDITION, STYLE))

    def boom(*args, **kwargs):
        msg = "render must not run on a stamped poster"
        raise AssertionError(msg)

    monkeypatch.setattr(plex_posters, "render", boom)

    async def setup() -> None:
        await seed_stamped(fake_plex, thumb="/library/metadata/1/thumb/50")

    rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 0
    assert rows == UNCHANGED


def test_marked_poster_without_a_row_refills_the_row(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", plex_posters.render(poster_bytes(), EDITION, STYLE))

    async def setup() -> None:
        await seed_movie("1", "Alpha")

    rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 0
    assert rows == UNCHANGED


def test_missing_poster_is_stamped_from_the_first_candidate(
    fake_plex: FakePlex,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    fake_plex.add("1", 404)
    fake_plex.add_candidate(
        "1", UPLOAD_PATH, poster_bytes(), candidate_key="upload://posters/0123abcd"
    )
    fake_plex.add_candidate("1", TMDB_A, 404)
    fake_plex.add_candidate("1", TMDB_B, poster_bytes(fill=BASE_FILL))

    async def setup() -> None:
        await seed_stamped(fake_plex, thumb="/library/metadata/1/thumb/50")

    with caplog.at_level("INFO", logger="usharr.plex_posters"):
        rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 1
    assert plex_posters.stamped_edition(fake_plex.uploads["1"]) == EDITION
    assert near(sample_outside_band(fake_plex.uploads["1"]), BASE_FILL)
    assert rows == STAMPED
    assert not fake_plex.fetched(UPLOAD_PATH)
    assert "X-Plex-Token" not in fake_plex.request_headers[TMDB_B]
    assert warnings_of(caplog) == []
    assert (
        f"plex_posters: Alpha (1): stamped Plex's first candidate {TMDB_B}"
        in messages(caplog)
    )


def test_edition_change_is_stamped_from_the_first_candidate(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", plex_posters.render(poster_bytes(), "Theatrical", STYLE))
    fake_plex.add_candidate("1", TMDB_A, poster_bytes(fill=BASE_FILL))

    async def setup() -> None:
        await seed_stamped(fake_plex, "Theatrical")

    rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 1
    assert plex_posters.stamped_edition(fake_plex.uploads["1"]) == EDITION
    assert near(sample_outside_band(fake_plex.uploads["1"]), BASE_FILL)
    assert rows == STAMPED


def test_server_side_candidate_is_fetched_with_plex_headers(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", plex_posters.render(poster_bytes(), "Theatrical", STYLE))
    path = "/library/metadata/1/file?url=metadata%3A%2F%2Fposters%2Fabc"
    fake_plex.add_candidate(
        "1", path, poster_bytes(fill=BASE_FILL), candidate_key="metadata://posters/abc"
    )

    async def setup() -> None:
        await seed_stamped(fake_plex, "Theatrical")

    rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 1
    assert "X-Plex-Token" in fake_plex.request_headers[path]
    assert rows == STAMPED


def test_style_change_is_stamped_from_the_first_candidate(
    fake_plex: FakePlex, monkeypatch: pytest.MonkeyPatch
):
    fake_plex.add("1", plex_posters.render(poster_bytes(), EDITION, STYLE))
    fake_plex.add_candidate("1", TMDB_A, poster_bytes(fill=BASE_FILL))

    async def setup() -> None:
        await seed_stamped(fake_plex, style="0ld5tyle")

    rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 1
    assert near(sample_outside_band(fake_plex.uploads["1"]), BASE_FILL)
    assert rows == STAMPED


def test_no_downloadable_candidate_is_a_logged_failure(
    fake_plex: FakePlex,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    fake_plex.add("1", plex_posters.render(poster_bytes(), "Theatrical", STYLE))
    fake_plex.add_candidate("1", TMDB_A, 404)
    fake_plex.add_candidate(
        "1", UPLOAD_PATH, poster_bytes(), candidate_key="upload://posters/0123abcd"
    )

    async def setup() -> None:
        await seed_stamped(fake_plex, "Theatrical")

    with caplog.at_level("WARNING", logger="usharr.plex_posters"):
        rows = asyncio.run(run_sync(setup, monkeypatch))
    assert fake_plex.posts("1") == 0
    assert rows == {"1": ("Theatrical", "/library/metadata/1/thumb/100", FINGERPRINT)}
    assert len(warnings_of(caplog)) == 1
    assert NO_CANDIDATE in warnings_of(caplog)[0]


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
    warnings = warnings_of(caplog)
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


def test_fingerprint_tracks_the_style():
    assert (
        plex_posters.style_fingerprint(PosterStyleConfig(font_size=40)) != FINGERPRINT
    )
    assert plex_posters.style_fingerprint(PosterStyleConfig()) == FINGERPRINT
