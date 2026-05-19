# Usharr

Usharr is a web application and API that complements Plex Media Server, Sonarr, Radarr, Bazarr and Tautulli. It scans your video files, extracting their metadata via `mediainfo` and determining their _actual_ aspect ratio(s) by sampling each video with `ffmpeg cropdetect`.

For each file, the web UI presents its aspect ratio(s), video info (SDR, HDR, DV, etc), audio info (language, codec, channels, surround sound type), and subtitle info (language, type).

It links from each file directly to its entry in your Plex Media Server, Sonarr, Radarr, Bazarr and Tautulli installations.

Usharr operates solely offline, extracting the information already in your video files. It does *not* organize your files nor does it connect to any online server (TMDb, IMDb, etc). There are already better programs that fill those roles. Usharr's purpose is to capture and present both via web UI and API the technical details of your media files.

## Configuration

Copy [`config.yaml.example`](./config.yaml.example) to `config.yaml` and edit.

Paths are scanned recursively for files with a video extension (`.avi`, `.iso`, `.m2ts`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.ts`, `.webm`). A full scan runs at startup and once an hour thereafter. Files whose size + mtime haven't changed are skipped; `mediainfo` and `ffmpeg cropdetect` are re-run only when needed.


## Web UI

### Application menu

-  **Scan Library**: Scan for added/removed files and synchronize with Plex and *arr integrations. Run mediainfo and AR analysis on new files.
-  **Refresh Library**: Re-run `mediainfo` on every file.
-  **Analyze Library…**: Re-run `mediainfo` and perform fresh AR analysis on every file.

### Library pages

One page per entry in `library` with a table on each page with one row per media item and the following columns: Title, Year, AR, Video, Audio, Eng Subs, Linksa

Title gets an edition badge when the filename carries the Plex/Jellyfin `{edition-NAME}` tag; year falls back to a parsed `(YYYY)` when Plex has no entry.

Rows whose last probe errored are highlighted red.

TV libraries roll episodes up under show + season header rows — click a show header to toggle all of its seasons, click a season header for just that one, or click the table header to flip the whole library.

Files living under a Plex-style extras subfolder (`Extras`, `Interviews`, `Featurettes`, `Deleted Scenes`, `Trailers`, …) are hidden in the table but show up under their parent media item's detail page.

### Detail pages

The detail pages contain sections for video, audio, subtitles and aspect ratio.

- Video: codec, profile, resolution, bit depth, HDR, frame rate, bit rate, container, duration.
- Audio: track #, language, title, default, forced, technical details
- Subtitles: subtitle #, format, language, title, default, forced, SDH, filename extension.
- Aspect ratio: AR and runtime percentage.

### Integration buttons

There are buttons on all pages which link directly to the media item in any configured integrations: Plex, Tautulli, Radarr or Sonarr and Bazarr. This allows you to quickly jump from Usharr to one of the integrations to get additional details not already presented by Usharr.


## HTTP API

- `/api/` JSON API, documented at `/docs` (Swagger UI) and `/redoc` (ReDoc)
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
  "detected_at": "2026-04-22T06:10:09Z",
  "error": null,
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

### Triggers

The web UI calls various task APIs:

```bash
# Incremental scan — picks up new files, re-probes changed ones.
curl -X POST http://usharr:8555/api/task/scan

# Force-refresh mediainfo on every file. Cached AR data preserved.
curl -X POST http://usharr:8555/api/task/refresh

# Force re-probe everything, including ardetector cropdetect. Slow.
curl -X POST http://usharr:8555/api/task/analyze

# Single-file variants. /scan/<path> is idempotent — it enqueues only
# when the DB row isn't already fresh (useful from a Sonarr/Radarr
# "on import" hook). /refresh and /analyze always force.
curl -X POST 'http://usharr:8555/api/task/scan/media/Movies/Dune%202021.mkv'
curl -X POST 'http://usharr:8555/api/task/refresh/media/Movies/Dune%202021.mkv'
curl -X POST 'http://usharr:8555/api/task/analyze/media/Movies/Dune%202021.mkv'

# Trigger sync with integrations all or individually:
curl -X POST http://usharr:8555/api/task/sync
curl -X POST http://usharr:8555/api/task/sync/plex
curl -X POST http://usharr:8555/api/task/sync/bazarr
curl -X POST http://usharr:8555/api/task/sync/radarr
curl -X POST http://usharr:8555/api/task/sync/sonarr
```

### Webhooks

Point Plex Server → Settings → Webhooks at `http://usharr:8555/api/webhook`. On `library.new` / `library.update` / `library.on.deck`, Usharr refreshes the referenced `plex_item` and enqueues a probe on the resolved local path.

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

Usharr matches media files against Plex's reported paths by longest path suffix.

## CLI

The container image exposes a CLI:

```bash
usharr probe /media/Movies/Dune\ 2021.mkv   # probe one file; print JSON; no DB writes
usharr scan                                  # run one full-scan pass and exit
usharr get /media/Movies/Dune\ 2021.mkv     # print the DB row, if any
usharr auth                                  # link to Plex (see above)
```

## Podman

```bash
# Build the image:
make build

# Build + export an OCI tarball to `usharr.tar` (for `docker load` on a remote host):
make image
```

The `/config` volume expects a directory containing `config.yaml`; `usharr.db` is created alongside it. Media tree mounts should be read-only.

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
- id: '1234567890'
  alias: Theater Curtains Match Plex
  description: ''
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
      value_template: '{{ trigger.entity_id == ''input_boolean.theater_curtains_match_plex''
        }}'
    then:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.theater_curtains_match_plex
    - variables:
        player: "{{ ['media_player.theater_plex_infuse',\n    'media_player.theater_plex_kodi']\n
          \  | select('is_state', 'playing') | list | first | default('') }}"
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
      value_template: "{{ states(player) == 'playing'\n   and state_attr(player, 'media_content_id')
        == content_id }}"
  - condition: template
    value_template: '{{ content_id is not none }}'
  - action: rest_command.usharr_info_by_content_id
    data:
      content_id: '{{ content_id }}'
    response_variable: usharr
  - condition: template
    value_template: '{{ usharr.status == 200 }}'
  - variables:
      ar: '{{ usharr["content"]["aspect"]["widest"] | float(0) }}'
  - action: lumagen.show_osd_message
    data:
      entity_id: remote.theater_lumagen
      line_one: Aspect {{ "%.2f" | format(ar) }}
      duration: 1
  - action: script.set_theater_curtains_for_aspect
    data:
      aspect: '{{ ar }}'
  mode: single

```

This automation looks up the aspect ratio of what's currently playing (as reported by the [Plex Media Server HA integration](https://www.home-assistant.io/integrations/plex/)) and then adjusts my theater screen curtains to match. It simultaneously displays the AR using my [Lumagen Radiance Pro HA integration(https://github.com/jaysoffian/ha-lumagen).

The automation triggers either when one of my media players (Infuse on Apple TV or Kodi on Ugoos AM6B+) starts playing, or when I manually trigger it using the `theater_curtains_match_plex` input boolean (which is exposed to HomeKit via [HomeKit Bridge](https://www.home-assistant.io/integrations/homekit/) so I can activate it via Siri).

## Development

Requires [mise-en-place](https://mise.jdx.dev) and [Podman](https://podman.io).

```bash
mise install              # install pinned tools (uv, prek)
make serve                # uvicorn against a read-only copy of usharr.db
mise x -- prek -a         # run all pre-commit checks
```

## License

[MIT](./LICENSE)
