# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
