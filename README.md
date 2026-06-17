# m3uproxy

A lightweight HLS proxy for M3U playlists that injects per-channel HTTP headers.

## The Problem

Some IPTV streams require specific HTTP headers (e.g. `Referer`, `Origin`) to play.
M3U playlists encode these via `#EXTVLCOPT` directives, which most media servers
(Emby, Jellyfin, Plex) ignore — they only support a single `Referer` per tuner.

## The Solution

`m3uproxy` sits between your M3U source and your media server:

1. Fetches your M3U playlist
2. Parses `#EXTVLCOPT` headers per channel
3. Rewrites all stream URLs (including HLS chunklists and segments) to go through itself
4. Injects the correct headers at every level of the HLS chain

## Quick Start

```bash
# Clone the repo
git clone https://github.com/anthonws/m3uproxy
cd m3uproxy

# Create your config from the sample and fill in M3U_URL and TZ
cp .env.example .env

docker compose up -d
```

Then point your media server tuner to:
```
http://<your-host>:7654/playlist.m3u
```

### Running without Docker

```bash
pip install -r requirements.txt
# Set variables in your environment or export them manually
export M3U_URL="https://your-m3u-provider.example.com/playlist.m3u"
python proxy.py
```

## Configuration

Copy `.env.example` to `.env` and edit it. Quote `M3U_URL` and `DEFAULT_UA`
with double quotes — Docker Compose's `.env` parser treats `#` as a comment
delimiter, which can silently truncate unquoted values.

## Environment Variables

| Variable           | Default | Description                                         |
|--------------------|---------|-----------------------------------------------------|
| `M3U_URL`          | *(required)* | URL of your M3U playlist                       |
| `PROXY_HOST`       | `0.0.0.0` | Bind address                                      |
| `PROXY_PORT`       | `7654`  | Port to listen on                                   |
| `DEFAULT_UA`       | Firefox Linux UA | Default User-Agent                         |
| `PLAYLIST_TTL`     | `86400` | Seconds between background playlist refreshes       |
| `CONNECT_TIMEOUT`  | `5`     | TCP connect timeout (seconds)                       |
| `STREAM_TIMEOUT`   | `10`    | Per-segment read timeout (seconds)                  |
| `PLAYLIST_TIMEOUT` | `20`    | Playlist fetch timeout (seconds)                    |
| `FETCH_RETRIES`    | `2`     | Retries on transient upstream errors                |
| `CHUNKLIST_TTL`    | `2`     | Seconds to cache HLS chunklists; `0` disables        |
| `POOL_MAXSIZE`     | `32`    | Keep-alive connections kept per upstream host       |
| `POOL_NUM_POOLS`   | `20`    | Number of distinct upstream hosts pooled            |
| `MAX_CONCURRENT`   | `0`     | Cap on in-flight requests; `0` = unlimited          |
| `CLIENT_TIMEOUT`   | `30`    | Seconds an idle keep-alive client connection is held |
| `LOG_LEVEL`        | `INFO`  | Log verbosity (`DEBUG`/`INFO`/`WARNING`/`ERROR`)    |
| `LOGS_ENDPOINT`    | `0`     | Set `1` to expose recent logs at `GET /logs`        |
| `LOG_RING_MAX`     | `300`   | Recent log lines kept in memory for `/logs`         |

### Tuning for larger deployments

The defaults target a home setup feeding a single media server (a handful of concurrent
streams). They are safe to leave as-is — `POOL_MAXSIZE=32` already covers far more
concurrency than a few users generate.

If you serve **many** concurrent streams (e.g. sharing with friends, or one channel pulled
by many clients at once):

- **`POOL_MAXSIZE`** — raise it if many streams hit the *same* upstream host simultaneously;
  it bounds how many keep-alive connections are reused per host before extras are opened and
  discarded. A rough guide: set it at or above your expected peak concurrent streams per host.
- **`MAX_CONCURRENT`** — set a positive cap (e.g. `100`) to bound how many requests are
  *processed* at once: requests past the cap get a fast `503` (and the connection is closed)
  instead of all being served concurrently. Leave `0` to disable. Note: this caps in-flight
  requests, not open connections — idle keep-alive connections are bounded by `CLIENT_TIMEOUT`.
- **`CLIENT_TIMEOUT`** — lower it if idle clients hold connections open too long; raise it if
  clients reconnect more often than you'd like.

These are I/O-light: each idle pooled connection is a socket plus small buffers, so even
generous values cost only kilobytes.

## Monitoring

`GET /health` returns JSON for liveness/readiness checks and a quick operational glance:

```json
{
  "ok": true, "version": "1.5", "channels": 1037, "cache_age_s": 12.3,
  "last_refresh_ok_age_s": 12.3, "last_refresh_error": null,
  "stream_ok": 42, "stream_err": 0, "fetch_ok": 980, "fetch_err": 3
}
```

It returns `200` when channels are loaded and `503` when the cache is empty (e.g. the
first playlist fetch failed), so it doubles as a container healthcheck. It is exempt from
`MAX_CONCURRENT` so monitoring stays truthful under load. Logs are structured (timestamp,
level, message) on stdout; raise detail with `LOG_LEVEL=DEBUG`.

The container ships a Docker `HEALTHCHECK` that probes `/health`, so `docker ps` and the
Synology Container Manager show health status.

### Reading logs

Logs go to stdout — read them with `docker logs m3uproxy --tail 100 -f`, or in Synology
Container Manager under the container's **Log** tab.

For convenience you can also expose the most recent log lines over HTTP by setting
`LOGS_ENDPOINT=1`:

```
GET /logs            # last LOG_RING_MAX lines (plain text)
GET /logs?tail=50    # last 50 lines
```

Query strings are stripped from logged request lines, so the per-channel `Referer`/
`Origin`/`User-Agent` values are not recorded. **`/logs` is still unauthenticated like the
rest of the proxy — keep it LAN-only.** It is disabled by default (`404`).

## Supported Headers

Reads the following `#EXTVLCOPT` directives from the M3U:

- `#EXTVLCOPT:http-referrer=`
- `#EXTVLCOPT:http-user-agent=`
- `#EXTVLCOPT:http-origin=`

## Tested With

- Emby Media Server

## Security

**Do not expose m3uproxy directly to the internet.** The proxy has no authentication and will relay requests to your upstream M3U source on behalf of anyone who can reach it. It is intended to run on a private network, accessible only to your local media server.

If you need remote access, place it behind a reverse proxy (e.g. Nginx, Caddy, Traefik) with authentication enabled.

Binding to `PROXY_HOST=127.0.0.1` restricts the proxy to localhost only, which is the safest option if your media server runs on the same host.

## Disclaimer

This project is provided as-is, without warranty of any kind. The author takes no responsibility for any damages, data loss, legal issues, or any other consequences arising from the use of this software.

**This tool is intended for use with legal, licensed IPTV services and publicly available streams only. The author does not endorse or support piracy or the use of unauthorised IPTV services.** It is your responsibility to ensure that the content you access complies with the laws and regulations of your country and the terms of service of your provider.

## License

MIT
