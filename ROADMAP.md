# m3uproxy — Improvement Roadmap

> Architecture review of the live service (`http://192.168.25.9:7654`) and source at commit `c62f8c6`.
> 47 findings were raised across 6 dimensions and each was adversarially verified against the source before inclusion.
> Tasks are written to be executed by smaller models: each names the exact file, the precise change, and an acceptance check.

## Health verdict

m3uproxy is healthy and performant today: /playlist.m3u returns 200 in ~37ms with all 1037 channels, /stream/1 tunes in ~169ms, and the single-file ThreadingHTTPServer + urllib3 design comfortably serves one Emby instance on a private LAN. There is no production fire. However, the codebase carries three silent-correctness bugs that defeat features or break specific channel types without any error signal: a missing `global _header` declaration (proxy.py:163) silently strips upstream EPG/url-tvg attributes and defeats the recent commit fb9050e; relative URLs in redirected playlists resolve against the stale pre-redirect base (proxy.py:274, 341), breaking playback for CDNs that 302; and #EXT-X-KEY/#EXT-X-MAP/#EXT-X-MEDIA URIs are never rewritten (proxy.py:149), so encrypted and fMP4 streams that need header injection fail with a black screen. These are the priority because they cause "channel is down" symptoms that look like an upstream problem. The single highest-leverage performance win is a 1-2s TTL micro-cache on chunklist m3u8 responses, which collapses repeated Emby polls into one upstream fetch. The strategic arc is: (1) fix the silent-failure correctness bugs first since they are cheap and high-impact; (2) land the chunklist cache plus the supporting keep-alive/pool tuning; (3) add a /health endpoint and structured logging so all subsequent changes are measurable; (4) establish a unit-test + lint + CI-gate baseline so the smaller models doing maintenance cannot reship the class of regression already in the tree; (5) harden the container and supply chain (non-root, healthcheck, resource/log limits, scan, pinning, semver image tags). An async/ASGI rewrite is explicitly out of scope — it adds a heavyweight dependency for no measurable benefit at this concurrency.

**Measured live health (all green):**

| Endpoint | Result |
|---|---|
| `GET /playlist.m3u` | 200 · 37 ms · 200 KB · 1037 channels |
| `GET /stream/1` (master m3u8) | 200 · 169 ms · rewritten correctly |
| `/fetch` chunklist | 200 · ~21 ms · re-fetched every call (no cache) |
| `/fetch` TS segment | 200 · 98 ms TTFB · 1.4 MB · valid MPEG-TS (`0x47`) |

**Verified findings:** 47 total — 4 high · 10 medium · 33 low. No critical issues; no production fire.

## Quick wins (do first)

| Task | Effort | Why |
|---|---|---|
| Add _header to the global declaration in refresh_playlist | S | One-word fix (proxy.py:163) that restores EPG/url-tvg attribute preservation — the feature commit fb9050e claimed to add but silently never took effect. Highest value-to-effort item in the entire backlog. |
| Suppress the Server version banner | S | Two class attributes on ProxyHandler (server_version='m3uproxy', sys_version='') stop leaking the exact Python/stdlib version for free CVE reconnaissance. Trivial and unambiguous. |
| Raise OSError when _fetch exhausts redirects on a 3xx | S | Turns a silent empty-200 (broken channel that looks fine) into a diagnosable 502 with a few lines after the redirect loop in _fetch. |
| Add the chunklist TTL micro-cache | M | The single biggest performance lever: a 2s in-process cache on the m3u8 branch of _proxy_segment collapses every Emby poll within the window into one upstream fetch, cutting upstream traffic and tune latency. |
| Add a /health endpoint plus Dockerfile HEALTHCHECK | S | Stdlib-only JSON endpoint that makes a wedged-but-alive process detectable by docker and lets the operator see channel count and cache age; unblocks restart-on-unhealthy. |
| Add docker-compose log rotation and resource limits | S | Zero-code compose change (logging max-size/max-file + mem_limit/cpus) that prevents chatty per-request logs from filling NAS disk over months and caps the DoS/OOM blast radius on the shared NAS. |

## Milestone 1: Correctness fixes + testing & CI baseline

**Goal:** Eliminate the bugs that break channels or features with no error signal, AND stand up the test suite + CI gate in the same PR so every later milestone is automatically checked.

**Rationale:** These correctness findings are the only ones that produce wrong output or broken playback today while looking healthy. They are cheap (mostly S/M), independent of everything else, and directly cause the 'channel is down' symptoms that are hardest to diagnose. Doing them first removes correctness risk before any performance or refactoring work touches the same functions (rewrite_m3u8, _fetch, refresh_playlist). The testing/CI tasks (originally Milestone 4) are pulled forward into this PR because the `_header` bug is the strongest argument for a test gate — a 5-line test catches it — and because the gate must exist before smaller models start changing code, so it cannot reship this class of regression.

> **Scope note (re-scoped):** Tasks **4.1 (unit tests), 4.2 (ruff), 4.3 (pin urllib3), 4.4 (CI test gate), 4.5 (version + CHANGELOG)** ship in the Milestone 1 PR alongside the six correctness fixes. Milestone 4 below retains only 4.6 (optional refactor). The 4.1 unit suite includes the `refresh_playlist`→`build_playlist` header-regression test that proves task 1.1.

### 1.1 Fix the missing _header global so EPG attributes survive
*Effort S · severity high*  
- **File:** `proxy.py`
- **Change:** On line 163 change `global _channels, _cache_ts` to `global _channels, _header, _cache_ts`. No other change needed; the assignment _header = header at line 174 then updates the module global instead of a discarded local.
- **Acceptance:** Point M3U_URL at a playlist whose first line is `#EXTM3U url-tvg="http://epg"`, restart, and confirm `curl http://192.168.25.9:7654/playlist.m3u | head -1` returns that exact attributed line, not a bare `#EXTM3U`.

### 1.2 Resolve relative playlist URLs against the post-redirect URL
*Effort M · severity high*  
- **File:** `proxy.py`
- **Change:** Make _fetch return the final URL alongside the body: change the return at line 86 to `return r.data, current_url` and update the two callers. In refresh_playlist (line 169) unpack and ignore the URL. In the /stream handler (lines 272-274) use the returned final URL to compute `base` instead of ch['url']. In _proxy_segment, after the GET at line 327, compute the base from `r.geturl()` (the post-redirect URL) and pass that into rewrite_m3u8 at line 341 instead of url.rsplit.
- **Acceptance:** Against an upstream that 302-redirects a master playlist to a different path, confirm the rewritten variant/segment /fetch URLs point at the redirected location and the stream plays; relative URIs no longer 404.

### 1.3 Rewrite URIs inside #EXT-X-KEY / #EXT-X-MAP / #EXT-X-MEDIA tags
*Effort M · severity high*  
- **File:** `proxy.py`
- **Change:** In rewrite_m3u8 (line 144), before the existing non-'#' branch at line 149, add a branch: if the stripped line starts with #EXT-X-KEY, #EXT-X-MAP, or #EXT-X-MEDIA and contains a URI="..." attribute (regex `URI="([^"]*)"`), extract the URI, urljoin it against base_url, build the proxied `{proxy_base}/fetch?url=<quoted>&{params}` form, and substitute it back inside the original quotes; append the modified line. All other '#' lines still pass through unchanged.
- **Acceptance:** Run rewrite_m3u8 on a synthetic playlist containing all three tags and assert each embedded URI is rewritten to a /fetch?url=... URL with the channel headers, while plain comment lines are untouched.

### 1.4 Return 502 when _fetch exhausts redirects on a 3xx
*Effort S · severity low*  
- **File:** `proxy.py`
- **Change:** After the `for _ in range(10)` redirect loop in _fetch (right after line 79, before the status checks), add: if r.status in (301,302,303,307,308): raise OSError('Too many redirects'). This routes redirect-loop cases to the caller's 502 path instead of returning an empty/garbage body as a fake success.
- **Acceptance:** Point a test channel at a URL that 302s in a loop and confirm /stream returns 502, not a 200 with an empty application/x-mpegurl body.

### 1.5 Stop the double-send after a partial response
*Effort M · severity medium*  
- **File:** `proxy.py`
- **Change:** Wrap the wfile.write in _send (line 234) in try/except (BrokenPipeError, ConnectionResetError) and swallow those. Add a self._started flag set True in _send and in the TS streaming branch right after end_headers (line 350). In the /stream except (line 281) and /fetch except (line 300) handlers, only call self._error(502) if not self._started; otherwise just log and return. This prevents emitting a second status block after a 200 body has begun.
- **Acceptance:** Simulate a client disconnect mid-stream (e.g. curl with --max-time cut short) and confirm logs show a clean handled close with no second HTTP status line written to the socket.

### 1.6 Preserve non-VLCOPT per-channel tags like #EXTGRP
*Effort S · severity low*  
- **File:** `proxy.py`
- **Change:** In parse_playlist's inner loop (lines 116-126), collect '#' lines that are not the three #EXTVLCOPT directives into a list and store it as ch['extra'] (default empty list) at line 130. In build_playlist (lines 138-140) emit ch['info'], then each line in ch.get('extra', []), then the proxied /stream URL.
- **Acceptance:** Parse a playlist whose channel block includes a standalone `#EXTGRP:News` line and confirm /playlist.m3u retains the #EXTGRP line between the #EXTINF and the rewritten /stream URL.

## Milestone 2: Performance: chunklist cache and connection efficiency

**Goal:** Cut upstream chunklist traffic and per-request latency without adding dependencies, keeping the single-file design.

**Rationale:** With correctness stabilized, this milestone captures the measured performance wins. The chunklist TTL cache is the dominant lever; HTTP/1.1 keep-alive and the pool-size bump are cheap multipliers that also reduce thread churn. The redundant-rewrite cleanup is subsumed by the cache (it stores already-rewritten bytes), and a concurrency cap is hygiene that bounds the DoS surface. Async is explicitly rejected here.

### 2.1 Add a TTL micro-cache for chunklist m3u8 responses
*Effort M · severity high*  
- **File:** `proxy.py`
- **Change:** Add module-level `_seg_cache: dict = {}`, `_seg_cache_lock = threading.Lock()`, and `CHUNKLIST_TTL = int(os.environ.get('CHUNKLIST_TTL','2'))`. In _proxy_segment, for GET requests, before the upstream request at line 327 check _seg_cache for url: if present and time.monotonic()-ts < CHUNKLIST_TTL, self._send(200,'application/x-mpegurl',data) and return. After computing the rewritten data in the m3u8 branch (line 341-343), store (time.monotonic(), data) under url under the lock, and evict an arbitrary entry when len(_seg_cache) > 500. Do NOT cache the TS streaming branch.
- **Acceptance:** Request the same /fetch?url=<chunklist> twice within 2s and confirm only one upstream fetch occurs (the CHUNK log line at proxy.py:299 appears once per TTL window, not once per request).

### 2.2 Compute proxy_base once per request
*Effort S · severity low*  
- **File:** `proxy.py`
- **Change:** Compute self.proxy_base() once at the top of do_GET into a local and pass it into build_playlist (line 254), rewrite_m3u8 in /stream (line 276), and _proxy_segment (add a proxy_base parameter, used at line 342) instead of recomputing it. This is a tidy-up that rides along with the cache; rewrite_m3u8 itself now runs once per TTL window because the cache stores rewritten bytes.
- **Acceptance:** Behavior unchanged: /playlist.m3u and a chunklist still serve identical bytes; proxy_base() is invoked at most once per request (verify by a temporary counter or by reading the call sites).

### 2.3 Enable HTTP/1.1 keep-alive with framing guards
*Effort M · severity medium*  
- **File:** `proxy.py`
- **Change:** Set class attribute `protocol_version = 'HTTP/1.1'` on ProxyHandler (line 219). In _error (lines 236-238) add `self.send_header('Content-Length','0')` before end_headers. In the TS streaming branch, when upstream provides no Content-Length (line 348 condition false), send `self.send_header('Connection','close')` before end_headers so a length-less streamed response does not desync a kept-alive connection. _send already sends Content-Length so it is safe.
- **Acceptance:** `curl -v` shows HTTP/1.1 on /playlist.m3u and reuses one TCP connection across two requests in a single invocation; a streamed TS with no upstream Content-Length still completes without the client hanging.

### 2.4 Raise PoolManager per-host maxsize
*Effort S · severity low*  
- **File:** `proxy.py`
- **Change:** In the PoolManager construction (lines 37-41) change maxsize from 10 to 32 and add block=False explicitly to document intent; keep num_pools=20. This retains more pooled connections per CDN host so concurrent segment/chunklist fetches reuse keep-alive instead of churning one-shot connections.
- **Acceptance:** Under a few concurrent segment fetches to one host, observe fewer new upstream connections (reuse) and no behavior change otherwise; the app still serves /playlist.m3u.

### 2.5 Bound concurrency with a semaphore
*Effort M · severity low*  
- **File:** `proxy.py`
- **Change:** Add `MAX_CONCURRENT = int(os.environ.get('MAX_CONCURRENT','64'))` and a module-level `_sem = threading.BoundedSemaphore(MAX_CONCURRENT)`. At the top of do_GET acquire with a short timeout (e.g. _sem.acquire(timeout=2)); if it fails, self._error(503) and return. Release in a finally that wraps the rest of do_GET. This caps in-flight work without changing the server class.
- **Acceptance:** Fire ~200 concurrent requests (hey/ab) and confirm excess requests get 503 and the live thread count plateaus rather than growing unbounded.

## Milestone 3: Observability baseline

**Goal:** Make the proxy's health, cache freshness, and error trends visible so subsequent changes are measurable and silent regressions surface.

**Rationale:** Observability comes after the correctness and performance changes so it can report on the new cache and reflect real behavior, and before the test/CI milestone so that operators (and the small models) have a runtime signal. The /health endpoint is the anchor that the cache-visibility and error-counter findings piggyback on; structured logging and config validation round out diagnosability for a single maintainer.

### 3.1 Add a /health readiness endpoint
*Effort S · severity medium*  
- **File:** `proxy.py`
- **Change:** Add `import json` at the top. In do_GET add an early branch: if path == '/health', build a dict {'ok': bool(_channels), 'channels': len(_channels), 'cache_age_s': round(time.monotonic()-_cache_ts,1) if _cache_ts else None} and self._send(200 if _channels else 503, 'application/json', json.dumps(...).encode()) then return.
- **Acceptance:** `curl http://192.168.25.9:7654/health` returns JSON with ok/channels/cache_age_s; with an empty cache it returns 503.

### 3.2 Expose cache refresh status in /health
*Effort S · severity low*  
- **File:** `proxy.py`
- **Change:** Add module globals `_last_refresh_ok_ts = 0.0` and `_last_refresh_err = None` (declare in refresh_playlist's global statement). On the success path in refresh_playlist (after line 175) set _last_refresh_ok_ts = time.monotonic() and _last_refresh_err = None; on the except path (line 182) set _last_refresh_err = f'{type(e).__name__}: {e}'. Add keys last_success_age_s and last_error to the /health JSON.
- **Acceptance:** Curl /health, then point M3U_URL at a broken URL and wait for a refresh; confirm last_error populates and last_success_age_s grows.

### 3.3 Add aggregate error counters to /health
*Effort M · severity low*  
- **File:** `proxy.py`
- **Change:** Add a module-level `_stats = {'fetch_ok':0,'fetch_err':0,'stream_ok':0,'stream_err':0}` guarded by a threading.Lock. Increment stream_ok after self._send in /stream (line 280) and stream_err in its except (line 282); increment fetch_ok after _proxy_segment returns in /fetch (line 296 area) and fetch_err in its except (line 301). Emit _stats inside the /health JSON. Keep it dependency-free (no prometheus_client).
- **Acceptance:** Hit a known-dead /stream/<id> and confirm stream_err increments in /health; a successful tune increments stream_ok.

### 3.4 Switch print() to stdlib logging with timestamps and levels
*Effort M · severity low*  
- **File:** `proxy.py`
- **Change:** At startup configure `logging.basicConfig(level=os.environ.get('LOG_LEVEL','INFO'), format='%(asctime)s %(levelname)s %(message)s')` and create a module logger. Replace the print() calls (lines 165-183, 222, 269, 277-279, 282, 299, 301-302) with log.info/warning/error keeping the same messages. Stays dependency-free.
- **Acceptance:** `docker logs m3uproxy` shows each line with an ISO timestamp and a level (INFO/WARNING/ERROR); errors are filterable from chatter.

### 3.5 Validate numeric env vars and echo effective config at startup
*Effort S · severity low*  
- **File:** `proxy.py`
- **Change:** Add a helper `_envint(name, default)` that wraps int() in try/except ValueError, prints '[m3uproxy] FATAL: <name> must be an integer, got <value>' and sys.exit(1); use it for the int() conversions at lines 24,27-31. After binding the server (after line 376) log a config banner with PORT, PLAYLIST_TTL, CONNECT/STREAM/PLAYLIST timeouts, FETCH_RETRIES, CHUNKLIST_TTL, channel count, and only the M3U_URL host (urlsplit().hostname) to avoid leaking credentials in the query string.
- **Acceptance:** Start with PLAYLIST_TTL=abc and confirm a clear FATAL message and exit 1 instead of a raw traceback; a normal start logs the effective config with the M3U_URL host only.

## Milestone 4: Testing and quality gate

> **Re-scoped:** tasks 4.1–4.5 ship in the **Milestone 1 PR** (see the Milestone 1 scope note). Only **4.6 (optional refactor)** remains here, to be done after the observability work so helpers can be extracted against a green test suite.

**Goal:** Give the small models doing maintenance an executable spec and a CI gate so the class of silent regression already in the tree cannot reship.

**Rationale:** The header-global bug is the strongest argument for tests: a 5-line test exercising the cache path would have caught it. This milestone depends on the correctness fixes existing (so the tests assert correct behavior) and naturally follows observability. Tests, lint, and the CI gate are sequenced together because the gate is meaningless until there is something to run.

### 4.1 Add a stdlib unittest suite for the pure parsing/rewriting logic
*Effort M · severity medium*  
- **File:** `tests/test_proxy.py`
- **Change:** Create tests/test_proxy.py using unittest (no new runtime dep). Cover: (1) parse_playlist on a 2-channel sample with all three #EXTVLCOPT directives asserts ids '1'/'2', the headers dict, and an attributed #EXTM3U line returned verbatim; (2) refresh_playlist cache path then build_playlist emits the attributed header as line 0 and `<base>/stream/<id>` URLs — this is the test that catches the header-global bug, so it must exercise refresh_playlist, not just parse_playlist; (3) rewrite_m3u8 turns non-# lines into /fetch?url=<quoted-abs>&... including a relative-URL urljoin case, passes # lines through, and rewrites a #EXT-X-KEY URI. Run with `python -m unittest discover tests`.
- **Acceptance:** `python -m unittest discover tests` passes with ~10 assertions, and reverting the line-163 _header fix makes the build_playlist header test fail.

### 4.2 Add a ruff lint/format config
*Effort S · severity low*  
- **File:** `pyproject.toml`
- **Change:** Create pyproject.toml with a minimal [tool.ruff] section enabling the default rules plus pyflakes (so F841 unused-local is caught). Document `ruff check proxy.py` in the README dev notes. ruff alone catches the assigned-but-unused local that the header bug produced; mypy is optional.
- **Acceptance:** `ruff check proxy.py` passes on the fixed tree, and against a version where `_header = header` is a discarded local it reports F841 unused-local.

### 4.3 Pin urllib3 to a compatible range
*Effort S · severity low*  
- **File:** `requirements.txt`
- **Change:** Change the single line from `urllib3>=2.0` to `urllib3>=2.0,<3` so a urllib3 3.x major (which could change Timeout/Retry/redirect/drain_conn semantics _fetch relies on) cannot land silently on the next build.
- **Acceptance:** `pip install -r requirements.txt` resolves a 2.x urllib3, and the app still imports and serves /playlist.m3u.

### 4.4 Add a CI test/lint gate that the build depends on
*Effort S · severity low*  
- **File:** `.github/workflows/docker.yml`
- **Change:** Add a `test` job that uses actions/setup-python@v5 (python-version 3.12), installs ruff, and runs `python -m py_compile proxy.py`, `ruff check proxy.py`, and `python -m unittest discover tests`. Add `pull_request: branches: [main]` to the `on:` block. Make build-and-push depend on the test job via `needs: test`, and guard the push so images are not published on pull_request (e.g. `if: github.event_name != 'pull_request'`).
- **Acceptance:** A PR runs the test job; a deliberate syntax error or failing test blocks the build-and-push job, and no image is pushed on PR events.

### 4.5 Add a version constant and changelog
*Effort S · severity low*  
- **File:** `proxy.py`
- **Change:** Add `__version__ = '1.3'` near the top of proxy.py and include it in the startup log line (extend line 376 to log the version). Add a CHANGELOG.md (Keep-a-Changelog style) with one bullet per existing tag v1..v1.3.
- **Acceptance:** Container start logs the version; CHANGELOG.md exists with entries for the current tags.

### 4.6 Extract shared m3u8-sniff and base-URL helpers
*Effort S · severity low*  
- **File:** `proxy.py`
- **Change:** Add `SNIFF_BYTES = 512` and `MAX_REDIRECTS = 10` module consts and two helpers: `_looks_like_m3u8(head: bytes) -> bool` returning `b'#EXT' in head[:SNIFF_BYTES]`, and `_base_url(url: str) -> str` returning `url.rsplit('/',1)[0] + '/'`. Use them at the /stream sniff/base (lines 274-276) and the _proxy_segment sniff/base (lines 335-341), and reuse MAX_REDIRECTS in the _fetch loop (line 69). Make the /fetch log label (line 298) use the sniff result rather than the .m3u8 extension guess.
- **Acceptance:** Tests still pass and behavior is unchanged; the CHUNK/SEG log label is correct for an extensionless chunklist URL.

## Milestone 5: Container and deployment hardening

**Goal:** Shrink blast radius and make deploys reproducible and recoverable, mostly via compose/Dockerfile/CI config with little or no app code.

**Rationale:** These are defense-in-depth and supply-chain hygiene items, correctly low/medium for a LAN-only single-instance tool, so they come after correctness, performance, observability, and the test gate. They cluster naturally: container runtime hardening (non-root, healthcheck, limits, log rotation), then memory-safety caps in code, then CI/supply-chain (scan, pinning, multi-arch, semver image tags, pinned compose).

### 5.1 Run the container as a non-root user
*Effort S · severity low*  
- **File:** `Dockerfile`
- **Change:** After `COPY proxy.py .` (line 5) add `RUN adduser -D -u 10001 app` and `USER 10001` before the CMD. The app writes nothing to /app and binds the unprivileged 7654, so this is safe.
- **Acceptance:** `docker exec m3uproxy id` shows uid=10001 and /playlist.m3u still returns 200.

### 5.2 Add a Dockerfile HEALTHCHECK
*Effort S · severity low*  
- **File:** `Dockerfile`
- **Change:** Add before CMD: `HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7654/health',timeout=4).status==200 else 1)"`. Probe /health (cheap, no synchronous playlist fetch) rather than /playlist.m3u to avoid the cold-cache 20s fetch exceeding the timeout.
- **Acceptance:** `docker inspect --format '{{.State.Health.Status}}' m3uproxy` shows healthy after start and unhealthy when the port is blocked.

### 5.3 Add log rotation, resource limits, and container hardening to compose
*Effort S · severity low*  
- **File:** `docker-compose.yml`
- **Change:** Under the m3uproxy service add: `logging: {driver: json-file, options: {max-size: '10m', max-file: '3'}}`, `mem_limit: 256m`, `cpus: 1.0`, `read_only: true`, `security_opt: ['no-new-privileges:true']`, and `cap_drop: ['ALL']`. The app writes nothing to disk so read_only is safe. Optionally mirror the healthcheck here.
- **Acceptance:** `docker inspect m3uproxy` shows ReadonlyRootfs=true, no added caps, and the log options; `docker stats` shows memory bounded by the limit while /playlist.m3u and a stream still work.

### 5.4 Cap response size in the buffered (playlist/m3u8) paths
*Effort M · severity medium*  
- **File:** `proxy.py`
- **Change:** Add `MAX_PLAYLIST_BYTES = int(os.environ.get('MAX_PLAYLIST_BYTES', str(8*1024*1024)))`. In _fetch switch to preload_content=False and read via r.stream(CHUNK_SIZE) into a bytearray, aborting with OSError('response too large') once it exceeds MAX_PLAYLIST_BYTES. Apply the same cap to the m3u8 sub-playlist buffering in _proxy_segment (the rest = r.read() at line 339). Do NOT cap the TS streaming branch. This guards against a misclassified direct stream exhausting RAM on the /stream path.
- **Acceptance:** Point a test channel at a large non-m3u8 body and confirm /stream returns an error (502) and process RSS stays bounded rather than growing toward OOM.

### 5.5 Suppress the Server banner and HTTP version disclosure
*Effort S · severity low*  
- **File:** `proxy.py`
- **Change:** On ProxyHandler (line 219) set class attributes `server_version = 'm3uproxy'` and `sys_version = ''` so the banner no longer leaks Python/3.12.x. (Note: protocol_version='HTTP/1.1' is handled in the performance milestone with its framing guards.)
- **Acceptance:** `curl -sI http://192.168.25.9:7654/playlist.m3u` shows a Server header with no Python/3.12.x version string.

### 5.6 Add a SIGTERM handler and non-blocking startup
*Effort M · severity low*  
- **File:** `proxy.py`
- **Change:** Install a signal handler for SIGTERM/SIGINT that calls server.shutdown(); run serve_forever in a thread or use try/finally so docker stop returns promptly and active streams end cleanly. Move the synchronous refresh_playlist() at line 371 off the startup critical path (e.g. run it in a daemon thread) so the server binds immediately and serves 503 (the existing path at lines 251-253) until the playlist loads, rather than appearing hung if the upstream is slow.
- **Acceptance:** `docker stop m3uproxy` returns within a couple of seconds without the 10s SIGKILL fallback; with a slow/unreachable upstream the container still binds and /health returns 503 promptly instead of hanging.

### 5.7 Pin the base image by digest and add a report-only Trivy scan
*Effort M · severity low*  
- **File:** `Dockerfile`
- **Change:** Change line 1 to `FROM python:3.12-alpine@sha256:<current-digest>` (resolve via `docker buildx imagetools inspect python:3.12-alpine`). In .github/workflows/docker.yml add a report-only Trivy step after build (aquasecurity/trivy-action, severity HIGH,CRITICAL, exit-code 0) and set `provenance: true` and `sbom: true` on the build-push step. Optionally add a .github/dependabot.yml docker entry to bump the digest.
- **Acceptance:** The FROM line carries an explicit @sha256 digest, the build still succeeds, and Trivy output appears in the Actions log without failing the build.

### 5.8 Build multi-arch images with build cache
*Effort S · severity medium*  
- **File:** `.github/workflows/docker.yml`
- **Change:** Add `docker/setup-qemu-action@v3` and `docker/setup-buildx-action@v3` steps before the build, set `platforms: linux/amd64,linux/arm64` on the build-push step, and add `cache-from: type=gha` and `cache-to: type=gha,mode=max`. This future-proofs against an ARM NAS and speeds repeat builds.
- **Acceptance:** `docker buildx imagetools inspect ghcr.io/anthonws/m3uproxy:latest` lists both linux/amd64 and linux/arm64; a second consecutive push shows the pip layer CACHED.

### 5.9 Publish semver image tags and pin compose
*Effort S · severity medium*  
- **File:** `.github/workflows/docker.yml`
- **Change:** Add `tags: ['v*.*.*']` under `on: push` and add semver templates to metadata-action: type=semver pattern {{version}}, {{major}}.{{minor}}, {{major}}; keep type=raw value=latest (default-branch only) and type=sha. Then change docker-compose.yml line 4 from `:latest` to a pinned semver tag (e.g. `:1.3`) or a digest, and document in README that upgrading means bumping the tag.
- **Acceptance:** Pushing a v1.4 tag produces ghcr.io/anthonws/m3uproxy:1.4, :1, and :1.4; `docker compose config` shows the pinned reference and `docker compose up -d` pulls exactly that tag.

## Milestone 6: Optional defense-in-depth: SSRF guard, auth, header privacy

**Goal:** Add code-level protections that today are provided only by LAN-only deployment, so a single misconfiguration does not turn latent risks live.

**Rationale:** These are correctly the last milestone: each is gated entirely by network reachability today, and the cloud-metadata angle does not apply to a NAS. They are worth doing as latent-high insurance before any reverse-proxy/port-forward exposure, but should not displace the correctness, performance, observability, and quality work. They are optional and can be adopted opportunistically.

### 6.1 Validate /fetch and channel URLs against private/loopback ranges
*Effort M · severity medium*  
- **File:** `proxy.py`
- **Change:** Add a `_url_allowed(url)` helper using urllib.parse.urlsplit (reject scheme not in {http,https}) and the ipaddress module: resolve the hostname and reject if any resolved address is_private/is_loopback/is_link_local/is_reserved (including literal 169.254.169.254). Call it in _proxy_segment before the upstream request (line 327) and in _fetch on each redirect hop (re-validate current_url at line 77); switch _proxy_segment to manual redirect following with the same check, or set redirect=False and re-validate. Return 403 on rejection.
- **Acceptance:** GET /fetch?url=http://127.0.0.1:7654/playlist.m3u and GET /fetch?url=http://169.254.169.254/ both return 403, while a normal upstream chunklist still proxies and plays.

### 6.2 Add an optional shared-secret token gate
*Effort M · severity low*  
- **File:** `proxy.py`
- **Change:** Add `PROXY_TOKEN = os.environ.get('PROXY_TOKEN','')`. When set, require a matching ?token= query param or X-Proxy-Token header on /stream and /fetch (return 401 otherwise), and have build_playlist and rewrite_m3u8 embed &token=<value> in the URLs they emit. When unset, keep current behavior and log a one-line startup warning. /health and /playlist.m3u stay open.
- **Acceptance:** With PROXY_TOKEN set, /fetch without the token returns 401; the rewritten playlist URLs include the token and channels still play. With it unset, behavior is unchanged and a warning is logged at startup.

### 6.3 Keep injected headers server-side instead of in URLs and scrub logs
*Effort M · severity low*  
- **File:** `proxy.py`
- **Change:** Change rewrite_m3u8 to emit /fetch?url=<abs>&cid=<id> instead of round-tripping Referer/User-Agent/Origin in the query, and in the /fetch handler look the header set up from the _channels cache by cid (mirroring /stream at lines 262-267). If that is too invasive, at minimum override log_message (lines 221-222) to strip everything after '?' in self.requestline before printing, so query-string header values do not land in logs.
- **Acceptance:** Served chunklist URLs no longer contain Referer=/Origin= (or, in the minimal version, log lines no longer show the query string), and segments still play with the correct headers.
