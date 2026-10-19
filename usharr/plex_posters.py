"""Stamp the edition name onto the Plex poster of every movie that has one."""

import hashlib
import io
import logging
import string
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from PIL import Image, ImageColor, ImageDraw, ImageFont

from usharr import http, plex, queries
from usharr.config import PosterStyleConfig, get_config
from usharr.models import PlexItem, PlexPoster

logger = logging.getLogger(__name__)

MARKER_PREFIX = "usharr-edition:"
REFERENCE_WIDTH = 1000
REFERENCE_HEIGHT = 1500
JPEG_QUALITY = 92
MIN_FONT_SIZE = 8
FONTS_DIR = Path(__file__).parent / "fonts"


# --- marker ---------------------------------------------------------------


def marker(edition: str) -> str:
    return f"{MARKER_PREFIX}{edition}"


def read_marker(image: Image.Image) -> str | None:
    """The edition a poster was stamped with, or None for an unstamped one."""
    comment = image.info.get("comment")
    if isinstance(comment, bytes):
        comment = comment.decode("utf-8", "replace")
    if isinstance(comment, str) and comment.startswith(MARKER_PREFIX):
        return comment.removeprefix(MARKER_PREFIX)
    return None


def stamped_edition(image_bytes: bytes) -> str | None:
    with Image.open(io.BytesIO(image_bytes)) as image:
        return read_marker(image)


# --- rendering ------------------------------------------------------------


def edition_text(template: str, edition: str) -> str:
    return (
        template.replace("<<editionU>>", edition.upper())
        .replace("<<editionL>>", edition.lower())
        .replace("<<editionP>>", string.capwords(edition))
        .replace("<<edition>>", edition)
    )


def font_path(name: str) -> Path:
    path = Path(name)
    return path if path.is_absolute() else FONTS_DIR / name


def fit_font(
    path: Path, text: str, size: int, max_width: int
) -> ImageFont.FreeTypeFont:
    """The font at ``size``, shrunk until ``text`` fits in ``max_width``."""
    while True:
        font = ImageFont.truetype(path, size)
        if font.getlength(text) <= max_width or size <= MIN_FONT_SIZE:
            return font
        size -= 1


def place(align: str, extent: int, size: int, offset: int) -> int:
    """Origin of a ``size``-long box inside ``extent`` for a Kometa alignment."""
    if align in ("left", "top"):
        return offset
    if align in ("right", "bottom"):
        return extent - size - offset
    return (extent - size) // 2 + offset


def render(image_bytes: bytes, edition: str, style: PosterStyleConfig) -> bytes:
    """Composite the edition band onto a poster and return it as a stamped JPEG."""
    with Image.open(io.BytesIO(image_bytes)) as source:
        poster = source.convert("RGBA")

    x_scale = poster.width / REFERENCE_WIDTH
    y_scale = poster.height / REFERENCE_HEIGHT
    back_width = round(style.back_width * x_scale)
    back_height = round(style.back_height * y_scale)
    left = place(
        style.horizontal_align,
        poster.width,
        back_width,
        round(style.horizontal_offset * x_scale),
    )
    top = place(
        style.vertical_align,
        poster.height,
        back_height,
        round(style.vertical_offset * y_scale),
    )

    overlay = Image.new("RGBA", poster.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(
        (left, top, left + back_width - 1, top + back_height - 1),
        radius=round(style.back_radius * y_scale),
        fill=ImageColor.getcolor(style.back_color, "RGBA"),
    )

    text = edition_text(style.text, edition)
    font = fit_font(
        font_path(style.font),
        text,
        max(round(style.font_size * y_scale), MIN_FONT_SIZE),
        back_width,
    )
    draw.text(
        (left + back_width / 2, top + back_height / 2),
        text,
        font=font,
        fill=ImageColor.getcolor(style.font_color, "RGBA"),
        anchor="mm",
    )

    stamped = Image.alpha_composite(poster, overlay).convert("RGB")
    out = io.BytesIO()
    stamped.save(out, format="JPEG", quality=JPEG_QUALITY, comment=marker(edition))
    return out.getvalue()


# --- Plex calls -----------------------------------------------------------


@dataclass(frozen=True)
class Server:
    url: str
    headers: dict[str, str]

    @classmethod
    async def connect(cls) -> Self:
        token, server_url, _ = await plex.load_auth()
        client_id = await plex.get_or_create_client_id()
        return cls(server_url.rstrip("/"), plex.headers(client_id, token))

    async def thumb(self, rating_key: str) -> str:
        """The path of the poster Plex currently shows for an item."""
        endpoint = f"{self.url}/library/metadata/{rating_key}"
        r = await http.client().get(endpoint, headers=self.headers, timeout=30.0)
        if r.status_code != 200:
            msg = f"GET {endpoint} → {r.status_code}"
            raise plex.PlexError(msg)
        resp = plex.parse(plex.PlexMetadataResponse, r.content, f"GET {endpoint}")
        if not resp.container.metadata or not resp.container.metadata[0].thumb:
            msg = f"{endpoint}: no poster"
            raise plex.PlexError(msg)
        return resp.container.metadata[0].thumb

    async def poster(self, thumb: str) -> bytes | None:
        """The current poster's bytes, or None when Plex no longer serves it."""
        r = await http.client().get(
            f"{self.url}{thumb}", headers=self.headers, timeout=60.0
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            msg = f"GET {thumb} → {r.status_code}"
            raise plex.PlexError(msg)
        return r.content

    async def first_candidate(self, item: PlexItem) -> tuple[str, bytes]:
        """Plex's first downloadable provider poster for an item."""
        endpoint = f"{self.url}/library/metadata/{item.rating_key}/posters"
        r = await http.client().get(endpoint, headers=self.headers, timeout=30.0)
        if r.status_code != 200:
            msg = f"GET {endpoint} → {r.status_code}"
            raise plex.PlexError(msg)
        listing = plex.parse(plex.PlexPostersResponse, r.content, f"GET {endpoint}")
        for candidate in listing.container.metadata:
            if candidate.rating_key.startswith("upload://"):
                continue
            if candidate.key.startswith("/"):
                r = await http.client().get(
                    f"{self.url}{candidate.key}", headers=self.headers, timeout=30.0
                )
            else:
                # An external image host never gets the Plex token.
                r = await http.client().get(candidate.key, timeout=30.0)
            if r.status_code == 200:
                return candidate.rating_key, r.content
        msg = "no usable poster candidate; select a poster in Plex"
        raise plex.PlexError(msg)

    async def upload_poster(self, rating_key: str, jpeg: bytes) -> None:
        endpoint = f"{self.url}/library/metadata/{rating_key}/posters"
        r = await http.client().post(
            endpoint,
            headers={**self.headers, "Content-Type": "image/jpeg"},
            content=jpeg,
            timeout=60.0,
        )
        if r.status_code != 200:
            msg = f"POST {endpoint} → {r.status_code}"
            raise plex.PlexError(msg)


# --- the pass -------------------------------------------------------------


def style_fingerprint(style: PosterStyleConfig) -> str:
    return hashlib.sha256(style.model_dump_json().encode()).hexdigest()[:16]


async def stamp(
    server: Server, item: PlexItem, row: PlexPoster | None, style: PosterStyleConfig
) -> bool:
    """Stamp one movie's poster unless it already carries this edition and style."""
    edition = item.edition or ""
    fingerprint = style_fingerprint(style)
    # No row (a rebuilt DB) is taken as stamped with the current style.
    same_style = row is None or row.style == fingerprint
    thumb = await server.thumb(item.rating_key)
    if row is not None and row.edition == edition and row.thumb == thumb and same_style:
        return False
    poster = await server.poster(thumb)
    marked = stamped_edition(poster) if poster is not None else None
    if poster is not None and marked == edition and same_style:
        await queries.upsert_plex_poster(
            rating_key=item.rating_key, edition=edition, thumb=thumb, style=fingerprint
        )
        return False
    if poster is not None and marked is None:
        base = poster
    else:
        rating_key, base = await server.first_candidate(item)
        logger.info(
            "plex_posters: %s (%s): stamped Plex's first candidate %s",
            item.title,
            item.rating_key,
            rating_key,
        )
    await server.upload_poster(item.rating_key, render(base, edition, style))
    thumb = await server.thumb(item.rating_key)
    logger.info("plex_posters: stamped %s (%s)", item.title, item.rating_key)
    await queries.upsert_plex_poster(
        rating_key=item.rating_key, edition=edition, thumb=thumb, style=fingerprint
    )
    return True


async def sync() -> None:
    try:
        style = get_config().plex.posters
        if style is None:
            return
        try:
            server = await Server.connect()
        except plex.PlexNotLinkedError:
            logger.info("plex_posters: not linked; skipping")
            return
        cached = await queries.plex_posters_by_rating_key()
        counts: Counter[str] = Counter()
        for item in await queries.list_movies_with_edition():
            try:
                uploaded = await stamp(server, item, cached.get(item.rating_key), style)
            except Exception as exc:
                logger.warning(
                    "plex_posters: %s (%s) failed: %s", item.title, item.rating_key, exc
                )
                counts["failed"] += 1
            else:
                counts["stamped" if uploaded else "skipped"] += 1
        logger.info(
            "plex_posters: stamped=%d skipped=%d failed=%d",
            counts["stamped"],
            counts["skipped"],
            counts["failed"],
        )
    except Exception:
        logger.exception("plex_posters errored")
