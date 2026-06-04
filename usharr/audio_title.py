"""Sanitize dirty DB audio-track titles for display."""

import re
from functools import lru_cache

import langcodes

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
