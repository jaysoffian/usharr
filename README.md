# Usharr

Usharr is a web application and API that complements Plex Media Server, Sonarr, Radarr, Bazarr and Tautulli. It scans your video files, extracting their metadata via `mediainfo` and determining their _actual_ aspect ratio(s) by sampling each video with `ffmpeg cropdetect`.

For each file, [the web UI](https://github.com/jaysoffian/usharr/wiki/Web-UI) presents its aspect ratio(s), video info (SDR, HDR, DV, color or monochrome, etc), audio info (language, codec, channels, surround sound type), and subtitle info (language, type).

It links from each file directly to its entry in your Plex Media Server, Sonarr, Radarr, Bazarr and Tautulli installations.

Usharr operates entirely offline, extracting the information already in your video files. It does *not* organize your files nor does it connect to any online service (TMDb, IMDb, etc). There are already better programs that fill those roles. Usharr's purpose is to capture and present both via web UI and API the technical details of your media files.

## Configuration

Copy [`config.yaml.example`](./config.yaml.example) to `config.yaml` and edit.

Paths are scanned recursively for files with a video extension (`.avi`, `.iso`, `.m2ts`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.ts`, `.webm`). A full scan runs at startup and once an hour thereafter. Files whose size + mtime haven't changed are skipped; `mediainfo` and `ffmpeg cropdetect` are re-run only when needed.

### Integrations and Path Matching

The configuration file allows for integrations with Plex, Bazarr, Radarr, Sonarr, and Tautulli. Plex, Radarr, and Sonarr require Usharr to match the paths it discovers against the paths reported to it by the integrations; if these paths are not the same for any given integration, you must configure `path_map` for that integration. e.g.

```yaml
plex:
  url: https://plex.example.com
  path_map:
    "/media/Movies": "/mnt/Movies"
```

This would allow Usharr to match `/media/Movies/Brazil (1985)/Brazil (1985).mkv` (as exposed to Usharr) to `/mnt/Movies/Brazil (1985)/Brazil (1985).mkv` (as exposed to Plex).

Tautulli and Bazarr need no path matching: Tautulli links by Plex rating key, and Bazarr links reuse the Radarr movie id / Sonarr series id Usharr already holds (so Bazarr needs only a `url` plus `link_movies` / `link_series` flags, no API key).

## Web UI

See https://github.com/jaysoffian/usharr/wiki/Web-UI for screenshots.

### Application menu

-  **Scan Library**: Scan for added/removed/changed files. Sync with integrations. Mediainfo and aspect-ratio detection are run on new or changed files.
-  **Refresh Library**: Re-run `mediainfo` on every file.
-  **Analyze Library…**: Re-run `mediainfo` and aspect-ration detecion on every file.

### Library pages

One page per entry in `library` with a table on each page with one row per media item and the following columns: Title, Year, AR, Video, Audio, Eng Subs, Integrations.

Title gets an edition badge when the filename carries an `{edition-NAME}` tag; year falls back to a parsed `(YYYY)` when Plex has no entry.

Rows highlighted red failed when running `mediainfo` on them.

TV libraries roll episodes up under show + season header rows — click a show header to toggle all of its seasons, click a season header for just that one, or click the table header to flip the whole library.

Files living under a Plex-style extras subfolder (`Extras`, `Interviews`, `Featurettes`, `Deleted Scenes`, `Trailers`, …) are hidden in the table but show up under their parent media item's detail page.

### Detail pages

The detail pages contain sections for video, audio, subtitles and aspect ratio.

- Video: codec, profile, resolution, bit depth, HDR, Color (Color, % Color w/monochrome scenes, % Monochrome w/color scenes, or Monochrome), frame rate, bit rate, container, duration.
- Audio: track #, language, title, default, forced, technical details
- Subtitles: subtitle #, format, language, title, default, forced, SDH, filename extension.
- Aspect ratio: AR and runtime percentage.

### Integration buttons

Integration buttons link directly to the media item in any configured integrations: Plex, Tautulli, Radarr or Sonarr and Bazarr. This allows you to quickly jump from Usharr to one of the integrations to get additional details not already present in Usharr.

## HTTP API

- `/api` JSON API, documented at `/docs` (Swagger UI) and `/redoc` (ReDoc)
- `/health` for monitoring

Example queries:

```bash
# Look up by local path (the path as usharr sees it in its own mounts).
# Everything after `/api/info/` is the absolute file path.
curl 'http://usharr:8555/api/info/media/Movies/Dune%202021.mkv'

# Look up by Plex content_id (requires `usharr auth` first; see below).
curl http://usharr:8555/api/info/by-content-id/12345

curl http://usharr:8555/health
```

Example response:

```json
{
  "path": "/media/Movies/Dune 2021.mkv",
  "discovered_at": "2026-04-22T06:10:09Z",
  "mediainfo_probed_at": "2026-04-22T06:10:12Z",
  "ardetector_probed_at": "2026-04-22T06:13:48Z",
  "mediainfo_error": null,
  "ardetector_error": null,
  "container": "Matroska",
  "duration": 9534.2,
  "video": {
    "codec": "HEVC",
    "profile": "Main 10@L5.1@High",
    "width": 3840,
    "height": 2160,
    "bit_depth": 10,
    "hdr": "DV HDR10",
    "hdr_format": "Dolby Vision, Version 1.0, dvhe.07.06, BL+EL+RPU, HDR10 compatible",
    "frame_rate": 23.976
  },
  "aspect": {
    "primary": 2.39,
    "widest": 2.39,
    "samples": [
      {"aspect": 2.39, "percentage": 0.93},
      {"aspect": 1.90, "percentage": 0.07}
    ]
  },
  "audio": [
    {"idx": 0, "codec": "TrueHD Atmos", "channels": 8, "layout": "7.1",
     "language": "eng", "is_default": true, "is_forced": false,
     "bit_rate": 4500000, "sample_rate": 48000, "bit_depth": 24}
  ],
  "subtitles": [
    {"idx": 0, "source": "internal", "codec": "PGS", "language": "eng",
     "is_default": true, "is_forced": false, "is_sdh": false}
  ]
}
```

### Webhooks

Point Plex Server → Settings → Webhooks at `http://usharr:8555/api/webhook`. On `library.new`, Usharr does a scan for new items.

## Plex

Plex integration allows clients to query `/api/info/by-content-id/{id}` where `{id}` is Plex's `ratingKey`. Usharr must first be authenticated to Plex:

```bash
# From inside the container (or anywhere the DB is writable):
usharr auth

# Open the printed URL, click Allow, come back. usharr auto-discovers your
# owned server and stores the token in /config/usharr.db.
usharr auth --status   # show the link state
usharr auth --reset    # forget the stored token
```

## Podman

```bash
# Build the image:
make build

# Build + export an OCI tarball to `usharr.tar` (for `docker load` on a remote host):
make image
```

The `/config` volume expects a directory containing `config.yaml`; `usharr.db` is created alongside it. Media tree mounts can be read-only.

Sample compose file:

```yaml
services:
  usharr:
    container_name: usharr
    hostname: usharr
    image: localhost/usharr:latest
    pull_policy: never
    environment:
      TZ: UTC
    ports:
      - "8555:8555"
    read_only: true
    restart: unless-stopped
    security_opt:
      - no-new-privileges
    volumes:
      - /mnt/pool0/usharr/config:/config
      - /mnt/pool0/media/Movies:/media/Movies:ro
      - /mnt/pool0/media/Documentaries:/media/Documentaries:ro
      - /mnt/pool0/media/TV Shows:/media/TV Shows:ro
      - type: tmpfs
        target: /tmp
```

## Home Assistant

Sample automation:

```yaml
###
### configuration.yaml
###
rest_command:
  usharr_info_by_content_id:
    url: "https://usharr.example.com/api/info/by-content-id/{{ content_id }}"
    method: GET
    timeout: 10

automation: !include automations.yaml
script: !include scripts.yaml

###
### scripts.yaml
###
set_theater_curtains_for_aspect:
  alias: Set Theater Curtains for Aspect
  fields:
    aspect:
      description: Aspect ratio as a float (e.g. 2.39). Linearly mapped 1.33 -> 0, 2.40 -> 100, clamped.
      example: 2.39
      required: true
  variables:
    ar: '{{ aspect | float(0) }}'
    raw: '{{ ((ar - 1.33) / 1.07 * 100) | round | int }}'
    position: '{{ 0 if raw < 0 else (100 if raw > 100 else raw) }}'
  sequence:
    - action: cover.set_cover_position
      target:
        entity_id: cover.theater_curtains
      data:
        position: '{{ position | int }}'
###
### automations.yaml
###
- alias: Theater Curtains Match Plex
  triggers:
  - trigger: state
    entity_id:
    - media_player.theater_plex_kodi
    - media_player.theater_plex_infuse
    to: playing
    not_from: paused
  - trigger: state
    entity_id: input_boolean.theater_curtains_match_plex
    to: 'on'
  actions:
  - if:
    - condition: template
      value_template: '{{ trigger.entity_id == ''input_boolean.theater_curtains_match_plex' }}'
    then:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.theater_curtains_match_plex
    - variables:
        player: "{{ ['media_player.theater_plex_infuse', 'media_player.theater_plex_kodi'] | select('is_state', 'playing') | list | first | default('') }}"
    - condition: template
      value_template: '{{ player != "" }}'
    - variables:
        content_id: '{{ state_attr(player, "media_content_id") }}'
    else:
    - variables:
        player: '{{ trigger.entity_id }}'
        content_id: '{{ trigger.to_state.attributes.media_content_id }}'
    - delay:
        hours: 0
        minutes: 0
        seconds: 3
        milliseconds: 0
    - condition: template
      value_template: "{{ states(player) == 'playing'\n   and state_attr(player, 'media_content_id') == content_id }}"
  - condition: template
    value_template: '{{ content_id is not none }}'
  - action: rest_command.usharr_info_by_content_id
    data:
      content_id: '{{ content_id }}'
    response_variable: response
  - condition: template
    value_template: '{{ response.status == 200 }}'
  - variables:
      aspect_ratio: '{{ response["content"]["aspect"]["widest"] | float(0) }}'
  - action: lumagen.show_osd_message
    data:
      entity_id: remote.theater_lumagen
      line_one: Aspect {{ "%.2f" | format(aspect_ratio) }}
      duration: 1
  - action: script.set_theater_curtains_for_aspect
    data:
      aspect: '{{ aspect_ratio }}'
  mode: single

```

This automation looks up the aspect ratio of what's currently playing (as reported by the [Plex Media Server HA integration](https://www.home-assistant.io/integrations/plex/)) and then adjusts my theater screen curtains to match. It simultaneously displays the AR using my [Lumagen Radiance Pro HA integration](https://github.com/jaysoffian/ha-lumagen).

The automation runs either when one of my media players (Infuse on Apple TV or Kodi on Ugoos AM6B+) starts playing, or when I manually trigger it using the `theater_curtains_match_plex` input boolean (which is exposed to HomeKit via [HomeKit Bridge](https://www.home-assistant.io/integrations/homekit/) so I can activate it via Siri).

## Development

Requires [mise-en-place](https://mise.jdx.dev) and [Podman](https://podman.io).

```bash
mise install              # install pinned tools (uv, prek)
make serve                # uvicorn against a read-only copy of usharr.db
mise x -- prek -a         # run all pre-commit checks
```

## License

[MIT](./LICENSE)
