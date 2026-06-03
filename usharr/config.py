"""YAML config loading for usharr."""

import logging
import os
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

yaml = YAML()
yaml.preserve_quotes = True

SEED_CONFIG = """\
library:
  Movies:
    - /media/Movies
  Documentaries:
    - /media/Documentaries
  Series:
    - /media/TV Shows

# Optional: override the URL used for Plex deep-links. Without this,
# usharr uses the URL auto-discovered during `usharr auth`, which is
# often an ugly plex.direct subdomain. Set this to the reverse-proxy
# URL you normally use to reach Plex — your cookies will match and
# you won't be re-prompted to log in. path_map (optional) works the
# same as on the *arr integrations: a local→remote prefix map applied
# to Plex-reported paths before suffix matching kicks in.
# plex:
#   url: https://plex.home.example.com
#   path_map:
#     "/media/Movies": /some/where/else/Movies

# Optional: Tautulli base URL for per-item deep-links.
# tautulli:
#   url: https://plexdash.home.example.com

# Optional: Bazarr subtitle deep-links. No API key needed — links reuse the
# Radarr movie id / Sonarr series id usharr already caches. Set the base URL
# and turn on whichever types you manage in Bazarr.
# bazarr:
#   url: https://bazarr.home.example.com
#   link_movies: true
#   link_series: true

# Optional: Radarr + Sonarr deep-links. API keys from the respective
# Settings → General → Security page.
# radarr:
#   url: https://radarr.home.example.com
#   api_key: <your-api-key>
#   path_map:
#     "/media/Movies": /some/where/else/Movies
# sonarr:
#   url: https://sonarr.home.example.com
#   api_key: <your-api-key>
#   path_map:
#     "/media/TV Shows": /some/where/else/TV Shows
"""


class StripNonesModel(BaseModel):
    """Drop None field values before validation so model defaults apply.

    YAML `foo:` with no children parses as None — without this, every
    optional subkey on every model would have to tolerate None explicitly.
    """

    @model_validator(mode="before")
    @classmethod
    def strip_nones(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data


class PlexConfig(StripNonesModel):
    # Overrides the URL used in deep-links. Without this, usharr uses
    # the URL it auto-discovered during `usharr auth`, which is often
    # an ugly plex.direct subdomain. API calls always use the stored
    # (auto-discovered) URL so they can reach the server locally even
    # when the UI URL points through a reverse proxy.
    url: str | None = None
    # {local_prefix: remote_prefix} — rewrite Plex-reported paths so
    # they match usharr's mounts before suffix matching runs.
    path_map: dict[str, str] = Field(default_factory=dict)


class TautulliConfig(StripNonesModel):
    url: str | None = None


class BazarrConfig(StripNonesModel):
    # Bazarr deep-links reuse the Radarr movie id / Sonarr series id usharr
    # already holds, so no API key is needed — just the base URL plus a flag
    # per type to say "I use Bazarr for these".
    url: str | None = None
    link_movies: bool = False
    link_series: bool = False


class RadarrConfig(StripNonesModel):
    url: str | None = None
    api_key: str | None = None
    path_map: dict[str, str] = Field(default_factory=dict)


class SonarrConfig(StripNonesModel):
    url: str | None = None
    api_key: str | None = None
    path_map: dict[str, str] = Field(default_factory=dict)


class Config(StripNonesModel):
    library: dict[str, list[str]] = Field(default_factory=dict)
    plex: PlexConfig = Field(default_factory=PlexConfig)
    tautulli: TautulliConfig = Field(default_factory=TautulliConfig)
    bazarr: BazarrConfig = Field(default_factory=BazarrConfig)
    radarr: RadarrConfig = Field(default_factory=RadarrConfig)
    sonarr: SonarrConfig = Field(default_factory=SonarrConfig)

    @property
    def all_paths(self) -> list[str]:
        return [p for paths in self.library.values() for p in paths]


@cache
def get_config() -> Config:
    path = Path(os.environ.get("USHARR_CONFIG", "config.yaml"))
    if not path.exists():
        logger.warning("Config %s not found, seeding defaults", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SEED_CONFIG)
    return Config.model_validate(yaml.load(path) or {})
