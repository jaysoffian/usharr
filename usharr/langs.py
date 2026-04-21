"""ISO 639 language code normalization + English display names.

Backed by the `langcodes` package (with `language_data` for the CLDR
name tables). Handles every 2- and 3-letter ISO 639 code including
regional variants and uncommon languages like Malayalam (ml →
Malayalam) that a hand-curated table would miss.

Results are memoized — libraries rarely have more than a few dozen
distinct language codes, and the library page hits these hundreds of
times per render.
"""

from functools import lru_cache

import langcodes


@lru_cache(maxsize=512)
def norm_lang(v: str | None) -> str | None:
    """Normalize to an ISO 639-2/3 three-letter code (lowercase), or None."""
    if not v:
        return None
    s = v.strip().lower()
    if not s:
        return None
    try:
        return langcodes.Language.get(s).to_alpha3()
    except Exception:
        return None


@lru_cache(maxsize=512)
def to_english_name(code: str | None) -> str:
    """Return the English display name for a language code.

    Handles both 2- and 3-letter codes, ISO 639-2/B (``fre``) and 639-2/T
    (``fra``) variants, plus regional tags. Falls back to a titlecased
    version of the input when langcodes can't resolve it.
    """
    if not code:
        return "Unknown"
    try:
        return langcodes.Language.get(code).display_name("en")
    except Exception:
        return code.title()
