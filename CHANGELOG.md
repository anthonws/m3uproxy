# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security
- Suppress Python runtime version from the `Server` response header. `ProxyHandler` now
  sets `server_version = "m3uproxy"` and `sys_version = ""`, so the header reads `Server:
  m3uproxy` rather than `Server: BaseHTTP/0.6 Python/3.12.x`.

## [1.7.0] - 2026-06-17

### Added
- Release automation: pushing a `vX.Y.Z` tag now builds versioned images (`:X.Y.Z`, `:X.Y`,
  `:X`) and auto-creates a GitHub Release (notes generated from merged PRs). `:latest` and
  `:<sha>` continue to track `main`; `docker-compose.yml` pins a release line so deploys
  follow tagged releases rather than every merge.
- `SEGMENT_TIMEOUT` (default 20s): bounds the time spent waiting on the **upstream** for a
  single segment. The per-read timeout (`STREAM_TIMEOUT`) only catches a fully stalled
  connection; a slow-trickling upstream could dodge it and hang for ~a minute, freezing the
  player. The proxy now aborts and closes the connection once accumulated upstream-read time
  exceeds the budget, so the client retries instead of stalling. Client backpressure (a slow
  player) is excluded, so a slow-but-healthy client isn't falsely aborted. Set `0` to disable.
- `CHUNKLIST_STALE_TTL` (default 15s): on a transient upstream error (e.g. a `503` burst),
  serve the last-good chunklist for a short grace window past its TTL instead of dropping
  the stream. Set `0` to disable. (Can't help when the whole CDN is down — no fresh
  segments exist.)
- `/health` gains a `fetch_stale` counter; aborted segments count as `fetch_err` and served-
  stale chunklists as `fetch_stale` (with `last_request_error` set), so flapping upstreams
  are visible instead of showing all-green.

## [1.6] - 2026-06-17

First tagged GitHub release. Captures all work since `v1.3`: silent-failure correctness
fixes, a performance cache, HTTP/1.1 keep-alive, observability, and signed-token injection.

### Added
- Optional upstream signed-token injection (`TOKEN_INJECT`): for providers whose playlist
  URLs need a token minted at a separate endpoint, the proxy fetches that token (cached
  `TOKEN_TTL` seconds, default 30) and adds it to matching upstream requests. Rules are
  `host-glob|token-endpoint|query-param` (`;;`-separated). Server-side only — the token
  never reaches the client. Failing token endpoints are negatively cached and implausible
  responses (HTML/JSON error pages) are rejected rather than cached. Off by default.
- Chunklist micro-cache (`CHUNKLIST_TTL`, default 2s): concurrent viewers of the same
  channel share one upstream chunklist fetch per TTL window instead of re-fetching on every
  poll. Raw bytes are cached and the per-request rewrite still runs on a hit; TS segments
  are never cached. Set `CHUNKLIST_TTL=0` to disable.
- HTTP/1.1 client keep-alive: the media server reuses one TCP connection for a stream's
  many segment requests. All response paths are framed (Content-Length, or
  `Connection: close` for length-less/aborted streams) so a kept-alive socket cannot
  desync; idle connections are reaped after `CLIENT_TIMEOUT` (default 30s).
- Configurable connection pool (`POOL_MAXSIZE` default 32, `POOL_NUM_POOLS` default 20)
  with tuning guidance in the README.
- Optional cap on concurrently-processing requests (`MAX_CONCURRENT`, default 0 =
  unlimited): a coarse load-shed returning a fast `503` past the limit.
- `GET /health` endpoint returning JSON (`ok`, `version`, `channels`, `cache_age_s`,
  `last_refresh_ok_age_s`, `last_refresh_error`, `last_request_error`, and `stream_ok/err`
  + `fetch_ok/err` counters); `503` when the cache is empty so it doubles as a container
  healthcheck; exempt from `MAX_CONCURRENT`.
- Opt-in `GET /logs` endpoint (`LOGS_ENDPOINT=1`, off by default): the last `LOG_RING_MAX`
  (300) log lines as plain text, with `?tail=N`. Query strings are stripped from logged
  request lines so injected header tokens are never recorded.
- Docker `HEALTHCHECK` probing `/health`, and log rotation in `docker-compose.yml`
  (`json-file`, `max-size: 10m`, `max-file: 3`).
- Structured stdlib logging (timestamp + level) on stdout, `LOG_LEVEL` env (default
  `INFO`), and a startup config banner (the upstream URL is logged by host only).
- Integer env vars are validated at startup — a bad value exits with a clear `FATAL`
  message instead of a traceback.
- Unit test suite under `tests/` and a CI `test` job (py_compile + Ruff + unit tests) that
  gates the Docker build; a `pyproject.toml` Ruff config; and a `__version__` constant
  surfaced in the startup log.

### Changed
- Benign idle keep-alive disconnects are logged at `DEBUG` (`idle connection closed`)
  instead of as alarming `Request timed out` lines; genuine handler errors log at
  `WARNING`.
- Pinned `urllib3` to `>=2.0,<3`; removed an unused `gzip` import.

### Fixed
- Preserve `#EXTM3U` header attributes (e.g. `url-tvg`) in the proxied playlist — a missing
  `global _header` meant the served header was always a bare `#EXTM3U`, so EPG mapping
  never took effect.
- Resolve relative HLS URLs against the **post-redirect** URL, so channels whose upstream
  302-redirects no longer emit 404 segment/variant URLs.
- Rewrite `URI="…"` attributes inside `#EXT-X-KEY`, `#EXT-X-MAP` and `#EXT-X-MEDIA` so AES
  keys, fMP4 init segments and alternate renditions get header injection.
- Surface upstream errors instead of a silent `502`: a `4xx`/`5xx` from an origin is logged
  with its status + host and counted as `fetch_err`.
- Return an error instead of a bogus empty `200` when redirects are exhausted.
- Never write a second HTTP status line over an in-flight response; client disconnects are
  handled cleanly.
- Preserve non-VLCOPT per-channel tags such as `#EXTGRP` in the proxied playlist.

## [1.3] — previous releases
- See git tags `v1` … `v1.3` for prior history (redirect handling, EPG mapping
  attempt, `.env` configuration, GHCR image build).
