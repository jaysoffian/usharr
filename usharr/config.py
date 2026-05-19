"""YAML config loading for usharr."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("USHARR_CONFIG", "config.yaml"))

# How often background loops (scan + sync) wake up to run another pass.
INTERVAL_SECONDS = 3600

_yaml = YAML()
_yaml.preserve_quotes = True

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

# Optional: Bazarr for subtitle deep-links. Requires an API key from
# Bazarr → Settings → General → Security. path_map (optional) maps
# usharr's mount prefixes to the prefixes the external service reports,
# for cases where the two aren't mounted identically.
# bazarr:
#   url: https://bazarr.home.example.com
#   api_key: <your-api-key>
#   path_map:
#     "/media/Movies": /some/where/else/Movies
#     "/media/TV Shows": /some/where/else/TV Shows

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


@dataclass
class PlexConfig:
    # Overrides the URL used in deep-links. Without this, usharr uses
    # the URL it auto-discovered during `usharr auth`, which is often
    # an ugly plex.direct subdomain. API calls always use the stored
    # (auto-discovered) URL so they can reach the server locally even
    # when the UI URL points through a reverse proxy.
    url: str | None = None
    # {local_prefix: remote_prefix} — rewrite Plex-reported paths so
    # they match usharr's mounts before suffix matching runs.
    path_map: dict[str, str] = field(default_factory=dict)


@dataclass
class TautulliConfig:
    url: str | None = None


@dataclass
class BazarrConfig:
    url: str | None = None
    api_key: str | None = None
    # {local_prefix: remote_prefix} — rewrite Bazarr-reported paths
    # so they match usharr's mounts.
    path_map: dict[str, str] = field(default_factory=dict)


@dataclass
class RadarrConfig:
    url: str | None = None
    api_key: str | None = None
    path_map: dict[str, str] = field(default_factory=dict)


@dataclass
class SonarrConfig:
    url: str | None = None
    api_key: str | None = None
    path_map: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    library: dict[str, list[str]] = field(default_factory=dict)
    plex: PlexConfig = field(default_factory=PlexConfig)
    tautulli: TautulliConfig = field(default_factory=TautulliConfig)
    bazarr: BazarrConfig = field(default_factory=BazarrConfig)
    radarr: RadarrConfig = field(default_factory=RadarrConfig)
    sonarr: SonarrConfig = field(default_factory=SonarrConfig)

    @property
    def all_paths(self) -> list[str]:
        return [p for paths in self.library.values() for p in paths]


def load_config(path: Path | None = None) -> Config:
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        logger.warning("Config %s not found, seeding defaults", config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(SEED_CONFIG)

    raw = _yaml.load(config_path) or {}
    library_raw = raw.get("library") or {}
    plex_raw = raw.get("plex") or {}
    tautulli_raw = raw.get("tautulli") or {}
    bazarr_raw = raw.get("bazarr") or {}
    radarr_raw = raw.get("radarr") or {}
    sonarr_raw = raw.get("sonarr") or {}
    return Config(
        library={str(k): list(v or []) for k, v in library_raw.items()},
        plex=PlexConfig(
            url=plex_raw.get("url") or None,
            path_map=dict(plex_raw.get("path_map") or {}),
        ),
        tautulli=TautulliConfig(
            url=tautulli_raw.get("url") or None,
        ),
        bazarr=BazarrConfig(
            url=bazarr_raw.get("url") or None,
            api_key=bazarr_raw.get("api_key") or None,
            path_map=dict(bazarr_raw.get("path_map") or {}),
        ),
        radarr=RadarrConfig(
            url=radarr_raw.get("url") or None,
            api_key=radarr_raw.get("api_key") or None,
            path_map=dict(radarr_raw.get("path_map") or {}),
        ),
        sonarr=SonarrConfig(
            url=sonarr_raw.get("url") or None,
            api_key=sonarr_raw.get("api_key") or None,
            path_map=dict(sonarr_raw.get("path_map") or {}),
        ),
    )
