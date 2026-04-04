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
git clone https://github.com/YOUR_USERNAME/m3uproxy
cd m3uproxy

# Edit docker-compose.yml and set your M3U_URL
docker compose up -d
```

Then point your media server tuner to:
```
http://<your-host>:7654/playlist.m3u
```

## Environment Variables

| Variable     | Default                                      | Description              |
|-------------|----------------------------------------------|--------------------------|
| `M3U_URL`   | *(required)*                                 | URL of your M3U playlist |
| `PROXY_HOST`| `0.0.0.0`                                    | Bind address             |
| `PROXY_PORT`| `7654`                                       | Port to listen on        |
| `DEFAULT_UA`| Firefox Linux UA                             | Default User-Agent       |

## Supported Headers

Reads the following `#EXTVLCOPT` directives from the M3U:

- `#EXTVLCOPT:http-referrer=`
- `#EXTVLCOPT:http-user-agent=`
- `#EXTVLCOPT:http-origin=`

## Tested With

- Emby Media Server

## License

MIT
