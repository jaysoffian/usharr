"""YAML config loading for usharr."""

import logging
import os
import shutil
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

yaml = YAML()
yaml.preserve_quotes = True

CONFIG_EXAMPLE = Path(__file__).parent / "config.yaml.example"


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
        shutil.copyfile(CONFIG_EXAMPLE, path)
    return Config.model_validate(yaml.load(path) or {})
