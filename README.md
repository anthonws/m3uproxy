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

## Supported Headers

Reads the following `#EXTVLCOPT` directives from the M3U:

- `#EXTVLCOPT:http-referrer=`
- `#EXTVLCOPT:http-user-agent=`
- `#EXTVLCOPT:http-origin=`

## Tested With

- Emby Media Server

## License

MIT
