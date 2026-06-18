# m3uproxy — Roadmap & Progress

Living tracker for the improvement work. **✅ = shipped, ☐ = remaining.** Each PR should
check off the item(s) it completes here and add a `CHANGELOG.md` entry. Shipped detail lives
in `CHANGELOG.md`; this file is the forward-looking plan.

**Current state:** healthy and fast (playlist ~37 ms, tune ~170 ms, full HLS chain works).
Released **v1.7.0**. The substantive review work (correctness, performance, observability,
resilience, release automation) is done; what remains is mostly deployment hardening and
optional defense-in-depth.

> Conventions for anyone picking this up: work on a branch → PR (never commit to `main`);
> keep the CI `test` gate green (`py_compile` + `ruff` + `unittest`); keep everything
> **channel/provider-agnostic** (generic placeholders only) in code, PRs, commits, and docs;
> the app runs on the NAS, so integration/acceptance checks need a deploy (can't be verified
> locally). Do one coherent PR at a time and stop for review.

---

## ✅ Shipped

- **Correctness + test/CI baseline** (PRs #1, #2): `global _header` EPG fix, post-redirect
  base URLs, `#EXT-X-KEY`/`MAP`/`MEDIA` URI rewriting, redirect-exhaustion error, no
  double-response, `#EXTGRP` preserved; unit-test suite + `ruff` + CI gate; `urllib3` pinned.
- **Performance** (#3, #4): chunklist micro-cache (`CHUNKLIST_TTL`); HTTP/1.1 keep-alive with
  framing guards + `CLIENT_TIMEOUT`; configurable pool (`POOL_MAXSIZE`/`POOL_NUM_POOLS`);
  opt-in concurrency cap (`MAX_CONCURRENT`).
- **Observability** (#5, #6, #7): `GET /health` (status + `stream/fetch` counters); upstream
  errors surfaced (no silent 502s) with `last_request_error`; structured logging +
  `LOG_LEVEL`; env-var validation; opt-in `GET /logs`; Docker `HEALTHCHECK`; compose log
  rotation; request-line query-string scrubbing.
- **Resilience** (#12): `SEGMENT_TIMEOUT` (bounds upstream-read time per segment — fixes the
  trickle-freeze); `CHUNKLIST_STALE_TTL` serve-stale on transient upstream error;
  `fetch_stale` counter.
- **Upstream signed-token injection** (#8): config-driven `TOKEN_INJECT` (provider-neutral).
- **Release automation** (#13) + **v1.6 / v1.7.0** releases: tag → versioned images
  (`:X.Y.Z`/`:X.Y`/`:X`) + auto GitHub Release; compose pinned to `:1`.
- **Agnostic cleanup** (#10) + commit-history scrub: no channel/provider names anywhere.

---

## ☐ Remaining

Suggested order: top to bottom (lowest-risk/highest-value first). Pick one coherent PR at a time.

### Container & deploy hardening (M5)

- [ ] **5.5 — Suppress the `Server` version banner.** `proxy.py`: set `ProxyHandler` class
  attrs `server_version = "m3uproxy"` and `sys_version = ""`. *(S)* — Accept: `curl -sI`
  shows no `Python/3.12.x`.
- [ ] **5.1 — Run as non-root.** `Dockerfile`: after `COPY proxy.py .`, `RUN adduser -D -u
  10001 app` + `USER 10001`. *(S)* — Accept: `docker exec m3uproxy id` shows uid 10001;
  `/playlist.m3u` still 200. (Needs NAS deploy to confirm.)
- [ ] **5.3 — Resource limits + container hardening.** `docker-compose.yml`: `mem_limit:
  256m`, `cpus: 1.0`, `read_only: true`, `security_opt: ["no-new-privileges:true"]`,
  `cap_drop: ["ALL"]`. *(S)* — Accept: `docker inspect` shows `ReadonlyRootfs=true`, no caps;
  memory bounded; still serves. (App writes nothing to disk, so `read_only` is safe.)
- [ ] **5.6 — Graceful shutdown + non-blocking startup.** `proxy.py`: SIGTERM/SIGINT handler
  → `server.shutdown()`; move the synchronous first `refresh_playlist()` off the startup path
  (daemon thread) so the server binds immediately and serves `503` until the playlist loads.
  *(M)* — Accept: `docker stop` returns in ~2 s (no SIGKILL fallback); a slow upstream doesn't
  hang startup.
- [ ] **5.4 — Cap buffered response size.** `proxy.py`: `MAX_PLAYLIST_BYTES` (default 8 MB);
  in `_fetch` and the m3u8 branch of `_proxy_segment`, read via `r.stream()` into a bounded
  buffer, abort with `OSError` past the cap. Do **not** cap the TS streaming branch. Guards
  against a misclassified direct/long stream exhausting RAM on `/stream`. *(M, touches the
  fetch path — review carefully.)* — Accept: a large non-m3u8 body on `/stream` → 502, RSS
  bounded.

### CI / supply chain (M5)

- [ ] **5.8 — Multi-arch images + build cache.** `docker.yml`: add `setup-qemu-action` +
  `setup-buildx-action`; `platforms: linux/amd64,linux/arm64`; `cache-from: type=gha` +
  `cache-to: type=gha,mode=max`. *(S–M)* — Accept: `docker buildx imagetools inspect …:1`
  lists both arches; a second push shows the pip layer `CACHED`. (Future-proofs an ARM NAS.)
- [ ] **5.7 — Pin base image by digest + vulnerability scan.** `Dockerfile`: `FROM
  python:3.12-alpine@sha256:<digest>`. `docker.yml`: report-only Trivy step (HIGH,CRITICAL,
  `exit-code: 0`) and `provenance: true` + `sbom: true` on the build-push step. Optionally a
  `.github/dependabot.yml` docker entry. *(M)* — Accept: `FROM` carries `@sha256`; Trivy
  output appears in the Actions log without failing the build.

### Refactor (M4.6, optional)

- [ ] **4.6 — Extract small shared helpers.** `proxy.py`: `SNIFF_BYTES = 512` const +
  `_looks_like_m3u8(head) -> b"#EXT" in head[:SNIFF_BYTES]`, used at the `/stream` and
  `_proxy_segment` sniff sites; make the `/fetch` `CHUNK`/`SEG` log label use the sniff result
  rather than the `.m3u8` extension guess. *(S, low value — only if touching nearby code.)*

### Optional defense-in-depth (M6 — gated by LAN-only today; do before any exposure)

- [ ] **6.1 — SSRF guard on `/fetch`.** `proxy.py`: `_url_allowed(url)` using
  `urllib.parse.urlsplit` (reject non-`http(s)` schemes) + the `ipaddress` module (reject
  hosts resolving to private/loopback/link-local/reserved, incl. `169.254.169.254`). Call it
  in `_proxy_segment` and on each redirect hop in `_fetch`; return `403` on rejection.
  *(M)* — Accept: `/fetch?url=http://127.0.0.1:7654/…` and `…169.254.169.254/…` return `403`;
  a normal upstream still proxies.
- [ ] **6.2 — Optional shared-secret auth.** `proxy.py`: `PROXY_TOKEN` env; when set, require a
  matching `?token=`/`X-Proxy-Token` on `/stream` and `/fetch` (`401` otherwise) and embed it
  in the URLs `build_playlist`/`rewrite_m3u8` emit; `/health` and `/playlist.m3u` stay open;
  when unset, current behavior + a one-line startup warning. *(M)* — Accept: with the token
  set, `/fetch` without it → `401`; rewritten playlist URLs include it and channels still play.
- [ ] **6.3 — Keep injected headers server-side.** `proxy.py`: emit `/fetch?url=…&cid=<id>`
  instead of round-tripping `Referer`/`Origin`/`User-Agent` in the query, and look the header
  set up from `_channels` by `cid` (mirroring `/stream`). *(M)* — Accept: served chunklist URLs
  no longer contain `Referer=`/`Origin=`; segments still play. (Log-side scrubbing already
  shipped in #6.)
