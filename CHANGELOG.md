# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Docker `HEALTHCHECK` that probes `/health` (honors `PROXY_PORT`), so container health
  shows in `docker ps` / Synology Container Manager.
- Log rotation in `docker-compose.yml` (`json-file`, `max-size: 10m`, `max-file: 3`) so
  per-request logs can't fill the host disk over time.
- Opt-in `GET /logs` endpoint (`LOGS_ENDPOINT=1`, off by default) returning the last
  `LOG_RING_MAX` (300) log lines as plain text, with `?tail=N` support — useful when there
  is no shell on the host. Query strings are stripped from logged request lines so
  per-channel `Referer`/`Origin`/`User-Agent` tokens are not recorded; the `/fetch` error
  log records host + filename only.
- `GET /health` endpoint returning JSON (`ok`, `version`, `channels`, `cache_age_s`,
  `last_refresh_ok_age_s`, `last_refresh_error`, and `stream_ok/err` + `fetch_ok/err`
  counters). Returns `503` when the cache is empty so it doubles as a container
  healthcheck; exempt from `MAX_CONCURRENT` so monitoring stays truthful under load.
- Structured logging via the stdlib `logging` module (timestamp + level + message on
  stdout); `LOG_LEVEL` env var (default `INFO`). Replaces ad-hoc `print()` calls.
- Startup config banner logging effective settings; the upstream URL is logged by host
  only (never its path/query, which may carry tokens).
- Integer env vars are validated at startup — a non-numeric value exits with a clear
  `FATAL` message instead of an opaque traceback.
- HTTP/1.1 client keep-alive: the media server now reuses one TCP connection for the
  many segment requests of a stream instead of reconnecting each time. All response
  paths are framed (Content-Length, or `Connection: close` for length-less streamed
  segments) so a kept-alive socket cannot desync. If a streamed segment fails after its
  headers were sent (upstream drop, short read, client disconnect), the connection is
  closed rather than left promising bytes it can't deliver. Idle connections are reaped
  after `CLIENT_TIMEOUT` (default 30s) so they don't pin a thread.
- Configurable connection pool: `POOL_MAXSIZE` (default 32) and `POOL_NUM_POOLS`
  (default 20), with tuning guidance in the README for larger deployments.
- Optional cap on concurrently-processing requests `MAX_CONCURRENT` (default 0 = unlimited):
  a coarse load-shed that returns a fast `503` and closes the connection past the limit.
  (Idle keep-alive connections are bounded separately by `CLIENT_TIMEOUT`.)
- Chunklist micro-cache (`CHUNKLIST_TTL`, default 2s): concurrent viewers of the
  same channel now share one upstream chunklist fetch per TTL window instead of
  re-fetching on every poll. Raw bytes are cached and the per-request rewrite still
  runs on a hit, so injected headers stay correct; TS segments are never cached.
  Set `CHUNKLIST_TTL=0` to disable.

### Fixed
- Preserve `#EXTM3U` header attributes (e.g. `url-tvg`) in the proxied playlist.
  `refresh_playlist` declared `global _channels, _cache_ts` but assigned `_header`,
  creating a discarded local — so the served header was always a bare `#EXTM3U`
  and the EPG mapping added in `fb9050e` never took effect.
- Resolve relative HLS URLs against the **post-redirect** URL. `_fetch` now returns
  `(body, final_url)` and `_proxy_segment` uses `r.geturl()`, so channels whose
  upstream 302-redirects no longer emit 404 segment/variant URLs.
- Rewrite `URI="…"` attributes inside `#EXT-X-KEY`, `#EXT-X-MAP` and `#EXT-X-MEDIA`
  so AES keys, fMP4 init segments and alternate renditions get header injection
  instead of being fetched directly by the client.
- Return an error instead of a bogus empty `200` when redirects are exhausted.
- Never write a second HTTP status line over an in-flight response; client
  disconnects (`BrokenPipe`/`ConnectionReset`) are now swallowed cleanly.
- Preserve non-VLCOPT per-channel tags such as `#EXTGRP` in the proxied playlist.

### Added
- Unit test suite under `tests/` covering playlist parsing, header preservation,
  extra-tag preservation and m3u8 rewriting (including the `#EXT-X-KEY` case).
- `pyproject.toml` with a Ruff lint configuration.
- CI `test` job (py_compile + Ruff + unit tests) that gates the Docker build;
  pull requests now run the gate without publishing an image.
- `__version__` constant, surfaced in the startup log line.

### Changed
- Pinned `urllib3` to `>=2.0,<3` to avoid a silent 3.x major upgrade.
- Removed the unused `gzip` import.

## [1.3] — previous releases
- See git tags `v1` … `v1.3` for prior history (redirect handling, EPG mapping
  attempt, `.env` configuration, GHCR image build).
