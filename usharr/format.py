"""Pure format helpers for UI rendering — video/audio/sub summary strings + ARs."""

import os
import re
from functools import lru_cache
from pathlib import Path

import langcodes

from usharr.db import AudioTrackRow, MediainfoRow, SubtitleTrackRow
from usharr.langs import to_english_name

YEAR_RE = re.compile(r"\((\d{4})\)")
EDITION_RE = re.compile(r"\{edition-([^}]+)\}")


def year_from_path(path: str) -> int | None:
    """Parse `(YYYY)` from filename or folder; return the first plausible year."""
    for m in YEAR_RE.finditer(path):
        y = int(m.group(1))
        if 1900 <= y <= 2099:
            return y
    return None


def edition_from_path(path: str) -> str | None:
    """Parse `{edition-NAME}` (Plex/Jellyfin convention) from the path."""
    m = EDITION_RE.search(path)
    return m.group(1).strip() if m else None


def format_video(
    video_width: int | None,
    video_height: int | None,
    video_hdr: str | None,
) -> str:
    """e.g. '4K DV(7)/HDR10' or '1080p SDR'.

    Codec and profile live on the detail page. When we have a resolution
    bucket, always label the HDR state so SDR files show 'SDR'
    explicitly rather than an unlabelled gap.
    """
    parts: list[str] = []
    bucket = resolution_bucket(video_width, video_height)
    if bucket:
        parts.append(bucket)
    if video_hdr:
        parts.append(video_hdr)
    elif bucket:
        parts.append("SDR")
    return " ".join(parts)


# (width_cap, height_cap, label) — ordered; first match wins. Caps get a
# 1% tolerance via blur so encoders that shave a few rows (e.g. 1916x1076
# WEB-DL) still land in the right bucket. Order matters: PAL 776x592 must
# be checked before 960x544, etc. Ported from tinyMediaManager's
# MediaFileHelper.getVideoFormat (itself an XBMC/Kodi port).
RESOLUTION_LADDER: tuple[tuple[int, int, str], ...] = (
    (128, 96, "SD"),
    (160, 120, "SD"),
    (176, 144, "SD"),
    (256, 144, "SD"),
    (320, 240, "SD"),
    (352, 240, "SD"),
    (426, 240, "SD"),
    (480, 272, "SD"),
    (480, 360, "SD"),
    (640, 360, "SD"),
    (640, 480, "480p"),
    (720, 480, "480p"),
    (800, 480, "480p"),
    (853, 480, "480p"),
    (776, 592, "480p"),  # 720x576 PAL (handbrake up to 776x592)
    (960, 544, "480p"),  # PSVita 540p
    (912, 384, "480p"),  # cropped 540p
    (1024, 576, "480p"),
    (1332, 720, "720p"),  # 1.85:1 upper bound
    (1998, 1080, "1080p"),  # 1.85:1 upper bound
    (2664, 1440, "1440p"),  # 1.85:1 upper bound
    (3840, 2160, "4K"),
    (3840, 1600, "4K"),  # 4K ultra-wide
    (4096, 2160, "4K"),  # DCI 4K
    (4096, 1716, "4K"),  # DCI 4K CinemaScope
    (3996, 2160, "4K"),  # DCI 4K flat
)


def blur(value: int) -> int:
    return value + value // 100


def resolution_bucket(w: int | None, h: int | None) -> str | None:
    if not h or not w:
        return None
    for w_cap, h_cap, label in RESOLUTION_LADDER:
        if w <= blur(w_cap) and h <= blur(h_cap):
            return label
    return "8K"


def lang_name(code: str | None) -> str:
    """English display name for a language code; empty when missing."""
    if not code:
        return ""
    return to_english_name(code)


def subtitle_file_exts(media_path: str, subs: list[SubtitleTrackRow]) -> list[str]:
    """Per-track filename suffix after the longest base shared by the
    media file and any external subtitle files.

    For an internal track the row's file is the media file itself, so
    the suffix reduces to the container ext (e.g. ``.mkv``). External
    subs typically show the varying part (``.en.srt``). If the paths
    don't share a useful base, the suffix falls back to the basename.
    """
    paths = [media_path]
    paths.extend(t.file_path for t in subs if t.source == "external" and t.file_path)
    cp = os.path.commonprefix(paths)
    dot = cp.rfind(".")
    if dot != -1:
        base = cp[:dot]
    else:
        slash = cp.rfind("/")
        base = cp[: slash + 1] if slash != -1 else ""
    out: list[str] = []
    for t in subs:
        p = t.file_path if t.source == "external" and t.file_path else media_path
        out.append(p[len(base) :] if base and p.startswith(base) else p)
    return out


def mediainfo_badges(
    mi: MediainfoRow | None,
    audio_tracks: list[AudioTrackRow],
) -> list[dict]:
    """Pick brand-mark badges (DV / HDR10+ / Atmos / DD+ / ...) for the nav.

    HDR badges are driven by the ``video_hdr`` token list. Audio badges
    look at the primary track: immersive formats (Atmos, DTS:X) render
    next to the base codec (DD+, TrueHD, DTS-HD MA, ...). Basic lossy
    codecs (AAC, MP3, Opus) and uncompressed (PCM, FLAC) don't get a
    badge — the kv tables already cover them.

    Returns ``[{"src": "/static/mediainfo/...svg", "alt": "..."}]``.
    Returns ``[]`` when no mediainfo row exists yet (file is a stub).
    """
    badges: list[dict] = []
    if mi is None:
        return badges
    if resolution_bucket(mi.video_width, mi.video_height) == "4K":
        badges.append(badge("video/4k.svg", "4K"))
    hdr = (mi.video_hdr or "").upper()
    if "DV" in hdr:
        badges.append(badge("video/dolby_vision.svg", "Dolby Vision"))
    if "HDR10+" in hdr:
        badges.append(badge("video/hdr10+.svg", "HDR10+"))
    elif "HDR10" in hdr:
        badges.append(badge("video/hdr10.svg", "HDR10"))

    primary = pick_primary_audio(audio_tracks)
    if primary:
        codec = (primary.codec or "").upper()
        # Base codec first (DD+ / TrueHD / DTS-HD MA / ...), then the
        # immersive-format badge to its right — reads as "the underlying
        # codec, plus the object layer on top".
        base = audio_base_badge(codec)
        if base:
            badges.append(base)
        if "ATMOS" in codec:
            badges.append(badge("audio/codec/atmos.svg", "Dolby Atmos"))
        if "DTS:X" in codec:
            badges.append(badge("audio/codec/dts-x.svg", "DTS:X"))
    return badges


def badge(rel: str, alt: str) -> dict:
    return {"src": f"/static/mediainfo/{rel}", "alt": alt}


def audio_base_badge(codec: str) -> dict | None:
    if "EAC3" in codec:
        return badge("audio/codec/eac3.svg", "Dolby Digital Plus")
    if codec.startswith("TRUEHD"):
        return badge("audio/codec/truehd.svg", "Dolby TrueHD")
    if codec == "AC3":
        return badge("audio/codec/ac3.svg", "Dolby Digital")
    if "DTS-HD MA" in codec:
        return badge("audio/codec/dtshd-ma.svg", "DTS-HD MA")
    if "DTS-HD HRA" in codec:
        return badge("audio/codec/dtshd-hra.svg", "DTS-HD HRA")
    if "DTS-ES" in codec:
        return badge("audio/codec/dts-es.svg", "DTS-ES")
    # DTS:X already rendered above; plain DTS gets its own badge
    if codec.startswith("DTS") and "DTS:X" not in codec:
        return badge("audio/codec/dts.svg", "DTS")
    if codec == "AAC":
        return badge("audio/codec/aac.svg", "AAC")
    if codec == "FLAC":
        return badge("audio/codec/flac.svg", "FLAC")
    if codec == "LPCM":
        return badge("audio/codec/lpcm.svg", "LPCM")
    if codec == "PCM":
        return badge("audio/codec/pcm.svg", "PCM")
    if codec == "MP3":
        return badge("audio/codec/mp3.svg", "MP3")
    if codec == "OPUS":
        return badge("audio/codec/opus.svg", "Opus")
    return None


# --- audio title cleanup --------------------------------------------------
#
# DB titles are dirty: many duplicate codec/layout/rate info we already show
# in the Lang and Details columns ("MGVC JPN / FLAC / 2.0 / 48 kHz / 1440
# kbps / 24 bit", "DTS-HD Master Audio / 5.1 / 48 kHz / 3461 kbps / 24-bit",
# "English"). `clean_audio_title` strips that redundancy at render time —
# the DB stays unchanged.

AUDIO_TITLE_CODECS = [
    "DTS-HD Master Audio",
    "DTS:X Master Audio",
    "DTS-HD HRA",
    "DTS-HD M[0-9]+",
    "DTS-HD MA",
    "DTS-HD-MA",
    "DTSHD MA",
    "DTSHD-MA",
    "DTS-HD",
    "DTSHD",
    "DTS:X",
    "DTS Core",
    "Dolby Digital Plus",
    "Dolby Digital",
    "Dolby TrueHD",
    "Dolby Stereo",
    "TrueHD/Atmos Audio",
    "TrueHD Audio w/ Dolby Atmos",
    "FLAC Audio",
    "TrueHD",
    "EAC-?3",
    "E-AC-?3",
    "AC-?3",
    "DDP",
    r"DD\+?",
    "DTS",
    "FLAC",
    "LPCM",
    "PCM",
    "AAC",
    "MP3",
    "Opus",
    "Vorbis",
    "MLP FBA",
    "MLP",
    "HDMA",
]

AUDIO_TITLE_STRUCTURE = [
    r"\d+\.\d+(?:-EX)?(?:\s*\+\s*\d+(?:\s*Objects?)?)?",
    r"Lt/Rt",
    r"\d+(?:\.\d+)?\s*kHz",
    r"~?\d+(?:[ ,]\d+)*\s*Kbps",
    r"\d+\s*-?\s*bits?",
    r"DN\s*-?\d+\s*dB",
    r"Objects?",
]

AUDIO_TITLE_MISC = [r"@", r"w/", r"embedded", r"Kbps", r"kbps"]

# Brand fragments. Counted as tech for pure-tech detection AND folded into a
# strict-anchored edge-peel ("1.0 Dolby Digital Dubbing" → "Dubbing"). They
# do NOT extend a language-only peel — that's why "English Audio Commentary"
# becomes "Audio Commentary", not "Commentary".
AUDIO_TITLE_SOFT_BRAND = [
    "Dolby Atmos",
    "Atmos Audio",
    "Dolby",
    "Audio",
    "Master",
    "Plus",
    "Digital",
    "HD",
]

# Layout-style words. Counted as tech for pure-tech detection but never peel
# at an edge — they pair with descriptive nouns ("Surround Mix", "Stereo
# Remix", "Atmos Upmix", "Dual Mono") that we want to keep.
AUDIO_TITLE_SOFT_DESC = ["Atmos", "Stereo", "Mono", "Surround"]


def audio_title_alt(items: list[str]) -> str:
    return "|".join(re.sub(r"\s+", r"\\s+", x) for x in items)


AUDIO_TITLE_STRICT_SRC = (
    "(?:"
    + audio_title_alt(AUDIO_TITLE_CODECS + AUDIO_TITLE_STRUCTURE + AUDIO_TITLE_MISC)
    + ")"
)
AUDIO_TITLE_SOFT_BRAND_SRC = "(?:" + audio_title_alt(AUDIO_TITLE_SOFT_BRAND) + ")"
AUDIO_TITLE_SOFT_DESC_SRC = "(?:" + audio_title_alt(AUDIO_TITLE_SOFT_DESC) + ")"
AUDIO_TITLE_STRICT_RE = re.compile(AUDIO_TITLE_STRICT_SRC, re.IGNORECASE)
AUDIO_TITLE_BROAD_RE = re.compile(
    "|".join(
        [
            AUDIO_TITLE_STRICT_SRC,
            AUDIO_TITLE_SOFT_BRAND_SRC,
            AUDIO_TITLE_SOFT_DESC_SRC,
        ]
    ),
    re.IGNORECASE,
)

# Glue numeric tech tokens to their units before tokenizing so a unit can't
# end up tokenized away from its number ("48 kHz" → "48kHz").
AUDIO_TITLE_PRE_GLUE = [
    (re.compile(r"(~?\d+(?:[ ,]\d+)*)\s+(Kbps)", re.IGNORECASE), r"\1\2"),
    (re.compile(r"(\d+(?:\.\d+)?)\s+(kHz)", re.IGNORECASE), r"\1\2"),
    (re.compile(r"(\d+)\s+(bits?)\b", re.IGNORECASE), r"\1\2"),
    (re.compile(r"(\d+)\s*-\s*(bits?)\b", re.IGNORECASE), r"\1\2"),
    (re.compile(r"\bDN\s*-?\s*(\d+)\s*(dB)", re.IGNORECASE), r"DN\1\2"),
    (
        re.compile(r"(\d+\.\d+)\s*\+\s*(\d+)\s*(Objects?)", re.IGNORECASE),
        r"\1+\2\3",
    ),
]

# Hyphens stay inside tokens (DTS-HD-MA, AC-3, 24-bit). Slashes stay too —
# Lt/Rt is one token, and the outer split already chunks the title on
# space-padded " / " section delimiters before this fires.
AUDIO_TITLE_TOKEN_SEP_RE = re.compile(r"([\s,]+)")


@lru_cache(maxsize=512)
def audio_title_lang_aliases(code: str | None) -> frozenset[str]:
    if not code:
        return frozenset()
    out: set[str] = {code.lower()}
    try:
        lang = langcodes.Language.get(code)
    except Exception:
        return frozenset(out)
    for fn in (lang.to_alpha3, lambda: lang.display_name("en"), lang.autonym):
        try:
            v = fn()
            if v:
                out.add(v.lower())
        except Exception:
            pass
    return frozenset(out)


def audio_title_pre_glue(s: str) -> str:
    for pat, rep in AUDIO_TITLE_PRE_GLUE:
        s = pat.sub(rep, s)
    return s


def audio_title_lang_alt(aliases: frozenset[str]) -> str:
    if not aliases:
        return ""
    return "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))


def audio_title_is_pure_tech(seg: str, aliases: frozenset[str]) -> bool:
    s = seg.strip()
    if not s:
        return True
    if s.lower() in aliases:
        return True
    leftover = AUDIO_TITLE_BROAD_RE.sub(" ", audio_title_pre_glue(s))
    if aliases:
        leftover = re.sub(
            r"\b(?:" + audio_title_lang_alt(aliases) + r")\b",
            " ",
            leftover,
            flags=re.IGNORECASE,
        )
    leftover = re.sub(r"[\s/()\[\]{},.:;\-+&|!]", "", leftover)
    return not leftover


def audio_title_classify(token: str, aliases: frozenset[str]) -> str:
    t = token.strip()
    if not t:
        return "plain"
    if t.lower() in aliases:
        return "lang"
    if re.fullmatch(AUDIO_TITLE_STRICT_SRC, t, re.IGNORECASE):
        return "strict"
    cleaned = AUDIO_TITLE_STRICT_RE.sub(" ", t)
    if cleaned != t and not re.search(r"\w", cleaned):
        return "strict"
    if re.fullmatch(AUDIO_TITLE_SOFT_BRAND_SRC, t, re.IGNORECASE):
        return "soft_brand"
    if re.fullmatch(AUDIO_TITLE_SOFT_DESC_SRC, t, re.IGNORECASE):
        return "soft_desc"
    return "plain"


def audio_title_peel_edge(seg: str, aliases: frozenset[str], from_left: bool) -> str:
    parts = AUDIO_TITLE_TOKEN_SEP_RE.split(seg)
    n = len(parts)
    token_indices = list(range(0, n, 2)) if from_left else list(range(n - 1, -1, -2))
    classes = {i: audio_title_classify(parts[i], aliases) for i in token_indices}

    innermost: int | None = None
    innermost_kind: str | None = None
    for i in token_indices:
        c = classes[i]
        if c in ("strict", "soft_brand", "soft_desc", "lang"):
            if c in ("strict", "lang"):
                innermost = i
                innermost_kind = c
        elif c == "plain":
            if parts[i].strip() == "":
                continue
            break
    if innermost is None:
        return seg

    extend_to = innermost
    if innermost_kind == "strict":
        if from_left:
            idx = innermost + 2
            while idx < n and classes.get(idx) == "soft_brand":
                extend_to = idx
                idx += 2
        else:
            idx = innermost - 2
            while idx >= 0 and classes.get(idx) == "soft_brand":
                extend_to = idx
                idx -= 2

    if from_left:
        return "".join(parts[extend_to + 2 :])
    cut = extend_to - 1
    return "".join(parts[: max(cut, 0)])


def audio_title_edge_peel(seg: str, aliases: frozenset[str]) -> str:
    prev = None
    s = seg.strip()
    while s != prev:
        prev = s
        s = audio_title_peel_edge(s, aliases, from_left=True).strip()
        s = audio_title_peel_edge(s, aliases, from_left=False).strip()
    return s.strip(" /,-+@")


def audio_title_strip_pure_tech_groups(s: str, aliases: frozenset[str]) -> str:
    while True:
        new = re.sub(
            r"\(([^()]*)\)",
            lambda m: (
                "" if audio_title_is_pure_tech(m.group(1), aliases) else m.group(0)
            ),
            s,
        )
        new = re.sub(
            r"\[([^\[\]]*)\]",
            lambda m: (
                "" if audio_title_is_pure_tech(m.group(1), aliases) else m.group(0)
            ),
            new,
        )
        if new == s:
            return s
        s = new


def audio_title_unwrap_outer(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == "(" and s[-1] == ")" and s.count("(") == 1:
        return s[1:-1].strip()
    if len(s) >= 2 and s[0] == "[" and s[-1] == "]" and s.count("[") == 1:
        return s[1:-1].strip()
    return s


def clean_audio_title(title: str | None, lang_code: str | None) -> str:
    """Strip codec/layout/rate/lang info that's already in the Lang and
    Details columns. Returns "" when only redundant info remained."""
    if not title:
        return ""
    aliases = audio_title_lang_aliases(lang_code)
    t = audio_title_pre_glue(title)
    t = audio_title_strip_pure_tech_groups(t, aliases)
    raw = [s.strip() for s in re.split(r"\s+/\s+", t) if s.strip()]
    kept = [s for s in raw if not audio_title_is_pure_tech(s, aliases)]
    kept = [audio_title_edge_peel(s, aliases) for s in kept]
    kept = [s for s in kept if s]
    result = " / ".join(kept).strip()
    result = audio_title_unwrap_outer(result)
    if result and audio_title_is_pure_tech(result, aliases):
        return ""
    return result


def format_audio_details(t: AudioTrackRow) -> str:
    """Compact per-track technical line: codec / layout / rate / bitrate / depth.

    Compression mode is intentionally omitted — it's redundant with the
    codec (TrueHD/DTS-HD MA/FLAC are lossless by definition, etc.).
    """
    parts: list[str] = []
    if t.codec:
        parts.append(t.codec)
    if t.layout:
        parts.append(t.layout)
    if t.sample_rate:
        parts.append(f"{t.sample_rate // 1000} kHz")
    if t.bit_rate:
        parts.append(f"{t.bit_rate // 1000} kbps")
    if t.bit_depth:
        parts.append(f"{t.bit_depth}-bit")
    return " / ".join(parts)


def format_audio(audio_tracks: list[AudioTrackRow]) -> str:
    """Primary track summary, e.g. 'English (EAC3 5.1)' or just '(EAC3 5.1)'."""
    primary = pick_primary_audio(audio_tracks)
    if primary is None:
        return ""
    codec = primary.codec or "?"
    layout = primary.layout
    inner = f"{codec} {layout}" if layout else codec
    lang_code = primary.language
    if lang_code:
        return f"{to_english_name(lang_code)} ({inner})"
    return f"({inner})"


def pick_primary_audio(tracks: list[AudioTrackRow]) -> AudioTrackRow | None:
    if not tracks:
        return None
    for t in tracks:
        if t.is_default:
            return t
    return tracks[0]


TEXT_SUB_FORMATS = frozenset({"SRT", "ASS", "SSA", "WEBVTT", "TX3G", "TELETEXT"})
RASTER_SUB_FORMATS = frozenset({"PGS", "VOBSUB", "DVB"})
# Within a tier, prefer the common/readable format so the chip label
# matches what the user expects to see.
SUB_FORMAT_PREF = {
    "SRT": 0,
    "WEBVTT": 1,
    "ASS": 2,
    "SSA": 3,
    "TX3G": 4,
    "TELETEXT": 5,
    "PGS": 0,
    "VOBSUB": 1,
    "DVB": 2,
}


def format_sub_chip(subtitle_tracks: list[SubtitleTrackRow]) -> dict | None:
    """The single best English-sub chip, or None if no English subs.

    Tier priority: text-based non-SDH > text-based SDH > raster
    non-SDH > raster SDH. Within a tier, SRT wins among text formats
    and PGS among raster; anything else falls back alphabetically.
    Shape: ``{"fmt": "SRT", "sdh": False}``.
    """

    def tier(t: SubtitleTrackRow) -> int:
        fmt = (t.codec or "").upper()
        if fmt in TEXT_SUB_FORMATS:
            return 1 if t.is_sdh else 0
        if fmt in RASTER_SUB_FORMATS:
            return 3 if t.is_sdh else 2
        return 4

    def within_tier(t: SubtitleTrackRow) -> tuple[int, str]:
        fmt = (t.codec or "").upper()
        return SUB_FORMAT_PREF.get(fmt, 99), fmt

    english = [t for t in subtitle_tracks if t.language == "eng" and t.codec]
    if not english:
        return None
    english.sort(key=lambda t: (tier(t), within_tier(t)))
    best = english[0]
    return {"fmt": best.codec, "sdh": best.is_sdh}


def format_aspects(aspect_set: list[dict] | None, primary: float | None) -> list[dict]:
    """Return [{ratio, is_primary}] sorted widest→narrowest."""
    if not aspect_set:
        if primary is None:
            return []
        return [{"ratio": primary, "is_primary": True}]
    out: list[dict] = []
    seen: set[float] = set()
    for entry in aspect_set:
        ar = entry.get("aspect")
        if ar is None or ar in seen:
            continue
        seen.add(ar)
        out.append({"ratio": ar, "is_primary": ar == primary})
    out.sort(key=lambda e: -e["ratio"])
    return out


def format_aspects_for_row(
    aspect_set: list[dict] | None,
    primary: float | None,
    max_items: int = 3,
) -> tuple[list[dict], bool]:
    """For library rows: keep widest + primary (deduped) + '…' when >max_items.

    Detail page still shows every AR in full via the ars-table.
    """
    full = format_aspects(aspect_set, primary)
    if len(full) <= max_items:
        return full, False
    widest = full[0]
    primary_entry = next((e for e in full if e["is_primary"]), None)
    items = [widest]
    if primary_entry is not None and primary_entry["ratio"] != widest["ratio"]:
        items.append(primary_entry)
    return items, True


def format_ratio(r: float) -> str:
    """'2.39' / '1.78'."""
    return f"{r:.2f}"


def format_color(color_pct: float | None) -> str:
    """Render ardetector color_pct as a Video-table value, paired with
    the "Color" row label. The parenthetical pct always refers to the
    dominant class. 5/95 buffer at the extremes absorbs detector noise.
      * color_pct <= 5%  → "Monochrome"
      * color_pct < 50%  → "Monochrome (Y%) w/color scenes"   (Y = mono pct)
      * color_pct < 95%  → "Color (X%) w/monochrome scenes"   (X = color pct)
      * color_pct >= 95% → "Color"
    """
    if color_pct is None:
        return "—"
    if color_pct <= 0.05:
        return "Monochrome"
    if color_pct >= 0.95:
        return "Color"
    if color_pct >= 0.5:
        return f"Color ({int(color_pct * 100)}%) w/monochrome scenes"
    return f"Monochrome ({int((1 - color_pct) * 100)}%) w/color scenes"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return ""
    total = round(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def format_season_episode(season: int | None, episode: int | None) -> str:
    if season is None and episode is None:
        return ""
    s = f"S{season:02d}" if season is not None else ""
    e = f"E{episode:02d}" if episode is not None else ""
    return f"{s}{e}"


SORT_ARTICLES = ("the ", "a ", "an ")


def sort_normalize(s: str) -> str:
    """Casefold and strip a leading English article for library sorting."""
    t = s.strip().casefold()
    for prefix in SORT_ARTICLES:
        if t.startswith(prefix):
            return t[len(prefix) :]
    return t


def format_display_title(
    media_file_path: str,
    plex_title: str | None,
    show_title: str | None,
) -> str:
    """Prefer Plex title (with show prefix for episodes); fall back to filename.

    Filename fallback strips `(YYYY)` and `{edition-...}` tags so those
    don't appear inline alongside the title — year and edition are
    rendered separately (year in its own column, edition as a badge).
    """
    if plex_title:
        if show_title:
            return f"{show_title} — {plex_title}"
        return plex_title
    stem = Path(media_file_path).stem
    stem = EDITION_RE.sub("", stem)
    stem = YEAR_RE.sub("", stem)
    return " ".join(stem.split()).strip(" -_.")


def plex_deeplink(
    server_url: str | None,
    machine_id: str | None,
    rating_key: str | None,
) -> str | None:
    """Deep-link into Plex web. Prefers the local server URL so the user's
    existing cookies carry over; app.plex.tv always forces a fresh login.
    """
    if not machine_id or not rating_key:
        return None
    if server_url:
        base = server_url.rstrip("/")
        return (
            f"{base}/web/index.html#!/server/{machine_id}"
            f"/details?key=%2Flibrary%2Fmetadata%2F{rating_key}"
        )
    return (
        f"https://app.plex.tv/desktop#!/server/{machine_id}"
        f"/details?key=%2Flibrary%2Fmetadata%2F{rating_key}"
    )


def tautulli_deeplink(base_url: str | None, rating_key: str | None) -> str | None:
    if not base_url or not rating_key:
        return None
    return f"{base_url.rstrip('/')}/info?rating_key={rating_key}"


def bazarr_movie_deeplink(base_url: str | None, radarr_id: int | None) -> str | None:
    if not base_url or radarr_id is None:
        return None
    return f"{base_url.rstrip('/')}/movies/{radarr_id}"


def bazarr_series_deeplink(base_url: str | None, sonarr_id: int | None) -> str | None:
    if not base_url or sonarr_id is None:
        return None
    return f"{base_url.rstrip('/')}/episodes/{sonarr_id}"


def radarr_deeplink(base_url: str | None, tmdb_id: int | None) -> str | None:
    if not base_url or tmdb_id is None:
        return None
    return f"{base_url.rstrip('/')}/movie/{tmdb_id}"


def sonarr_deeplink(base_url: str | None, title_slug: str | None) -> str | None:
    if not base_url or not title_slug:
        return None
    return f"{base_url.rstrip('/')}/series/{title_slug}"
