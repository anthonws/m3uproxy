#!/usr/bin/env python3
"""
m3uproxy – lightweight HLS proxy with per-channel header injection.

Reads #EXTVLCOPT directives from M3U playlists and injects the correct
Referer, User-Agent and Origin headers when proxying HLS streams.
"""

import fnmatch
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import urllib3

__version__ = "1.6"

log = logging.getLogger("m3uproxy")


def _envint(name: str, default: int) -> int:
    """Read an integer env var, exiting with a clear message on a bad value."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        # logging isn't configured yet at config-parse time; print the fatal directly.
        print(f"[m3uproxy] FATAL: {name} must be an integer, got {raw!r}", flush=True)
        sys.exit(1)


def _parse_token_rules(spec: str) -> list:
    """Parse TOKEN_INJECT into [(host_glob, token_endpoint, param), ...].

    Format: 'host-glob|token-endpoint|query-param', multiple rules separated by ';;'.
    Lets an operator make the proxy mint and inject a signed token (e.g. a URL-signing
    token a provider serves blank) without any provider-specific code.
    """
    rules = []
    for raw in spec.split(";;"):
        raw = raw.strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 3 or not all(parts):
            print(f"[m3uproxy] WARNING: ignoring malformed TOKEN_INJECT rule: {raw!r}", flush=True)
            continue
        rules.append((parts[0], parts[1], parts[2]))
    return rules


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
M3U_URL          = os.environ.get("M3U_URL", "")
HOST             = os.environ.get("PROXY_HOST", "0.0.0.0")
PORT             = _envint("PROXY_PORT", 7654)
UA               = os.environ.get("DEFAULT_UA",
                       "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0")
PLAYLIST_TTL     = _envint("PLAYLIST_TTL",     86400)  # seconds between background refreshes
CONNECT_TIMEOUT  = _envint("CONNECT_TIMEOUT",  5)      # TCP connect timeout
STREAM_TIMEOUT   = _envint("STREAM_TIMEOUT",   10)     # per-segment READ timeout (max gap between bytes)
SEGMENT_TIMEOUT  = _envint("SEGMENT_TIMEOUT",  20)     # total wall-clock per segment (0 disables); fail fast on trickle
PLAYLIST_TIMEOUT = _envint("PLAYLIST_TIMEOUT", 20)     # playlist fetch timeout
FETCH_RETRIES    = _envint("FETCH_RETRIES",    2)      # retries on transient errors
CHUNKLIST_TTL    = _envint("CHUNKLIST_TTL",    2)      # seconds to cache a chunklist (0 disables)
CHUNKLIST_STALE_TTL = _envint("CHUNKLIST_STALE_TTL", 15)  # serve last-good chunklist this long past TTL on upstream error (0 disables)
CHUNK_SIZE       = 128 * 1024                          # 128 KB streaming chunks
SEG_CACHE_MAX    = 500                                 # max cached chunklists (memory bound)
POOL_NUM_POOLS   = _envint("POOL_NUM_POOLS",   20)     # distinct upstream hosts pooled
POOL_MAXSIZE     = _envint("POOL_MAXSIZE",     32)     # keep-alive conns kept per upstream host
MAX_CONCURRENT   = _envint("MAX_CONCURRENT",   0)      # in-flight request cap (0 = unlimited)
CLIENT_TIMEOUT   = _envint("CLIENT_TIMEOUT",   30)     # idle keep-alive socket timeout (seconds)
LOG_LEVEL        = os.environ.get("LOG_LEVEL", "INFO")
LOGS_ENDPOINT    = os.environ.get("LOGS_ENDPOINT", "0") == "1"  # expose GET /logs (opt-in)
LOG_RING_MAX     = _envint("LOG_RING_MAX", 300)                 # recent log lines kept for /logs
TOKEN_TTL        = _envint("TOKEN_TTL", 30)                     # seconds to cache an injected token
TOKEN_FAIL_TTL   = 10                                           # cooldown before re-probing a failing token endpoint
_token_rules     = _parse_token_rules(os.environ.get("TOKEN_INJECT", ""))  # signed-token injection rules

# In-memory ring buffer of recent log lines, surfaced by the opt-in GET /logs endpoint
# (so logs are reachable over HTTP without a shell on the host).
_log_ring: deque = deque(maxlen=LOG_RING_MAX)


class _RingHandler(logging.Handler):
    def emit(self, record):
        try:
            _log_ring.append(self.format(record))
        except Exception:
            pass


_ring_handler = _RingHandler()
_ring_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_ring_handler)
log.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

# ---------------------------------------------------------------------------
# Connection pool  (shared across all threads)
# ---------------------------------------------------------------------------
_pool = urllib3.PoolManager(
    num_pools=POOL_NUM_POOLS,
    maxsize=POOL_MAXSIZE,
    block=False,  # never block a request waiting for a pooled slot; spill extra conns
    retries=urllib3.Retry(total=False, redirect=10),
)

# Optional cap on the number of requests PROCESSING concurrently (not open connections
# or threads — idle keep-alive connections are bounded by CLIENT_TIMEOUT reaping them).
# A coarse load-shed: requests past the cap get a fast 503. Off by default.
_sem = threading.BoundedSemaphore(MAX_CONCURRENT) if MAX_CONCURRENT > 0 else None

# ---------------------------------------------------------------------------
# Playlist cache
# ---------------------------------------------------------------------------
_channels: dict = {}
_header: str = "#EXTM3U"
_cache_ts: float = 0.0
_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Observability state
# ---------------------------------------------------------------------------
_last_refresh_ok_ts: float = 0.0
_last_refresh_err: str | None = None
_last_request_err: str | None = None  # most recent /stream or /fetch failure (host-scoped)
_stats = {"stream_ok": 0, "stream_err": 0, "fetch_ok": 0, "fetch_err": 0, "fetch_stale": 0}
_stats_lock = threading.Lock()


def _bump(key: str) -> None:
    with _stats_lock:
        _stats[key] += 1

# ---------------------------------------------------------------------------
# Chunklist micro-cache
#
# Live HLS chunklists are re-requested every few seconds by every viewer. Caching
# the raw upstream bytes for a short TTL collapses concurrent polls of the same
# channel into a single upstream fetch. We store the raw m3u8 plus the resolved
# base URL and re-run the (cheap) per-request rewrite on a hit, so injected headers
# and the proxy host stay correct per request. TS segments are never cached.
# ---------------------------------------------------------------------------
_seg_cache: dict = {}            # url -> (ts, raw_bytes, base_url)
_seg_cache_lock = threading.Lock()

# Cache of injected signed tokens, keyed by token endpoint (tokens are usually
# time-limited, so re-fetched every TOKEN_TTL seconds).
_token_cache: dict = {}          # endpoint -> (ts, token)
_token_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers(extra: dict) -> dict:
    return {"User-Agent": UA, **extra}


def _safe_host(url: str) -> str:
    """Host[:port] of a URL for logging — never the path/query (which may hold tokens)."""
    try:
        return urllib.parse.urlsplit(url).netloc or url
    except ValueError:
        return "<invalid-url>"


def _get_token(endpoint: str) -> str | None:
    """Fetch (and cache) a signed token from a provider endpoint, or None on failure.

    Failures are negatively cached for TOKEN_FAIL_TTL so a down/slow endpoint is probed at
    most once per cooldown rather than on every proxied request (which would block request
    threads and hammer the endpoint). The body is sanity-checked so an HTTP 200 error/HTML
    page is not cached and injected as a bogus token.
    """
    now = time.monotonic()
    with _token_lock:
        hit = _token_cache.get(endpoint)
        if hit and now - hit[0] < (TOKEN_TTL if hit[1] else min(TOKEN_TTL, TOKEN_FAIL_TTL)):
            return hit[1]
    token = None
    try:
        t = urllib3.Timeout(connect=CONNECT_TIMEOUT, read=STREAM_TIMEOUT)
        r = _pool.request("GET", endpoint, headers=_headers({}), timeout=t)
        if r.status >= 400:
            raise OSError(f"HTTP {r.status}")
        body = r.data.decode("utf-8", "replace").strip()
        # A signed token is a short, single, printable string — reject obvious non-tokens
        # (empty, oversized, HTML/JSON pages, anything with whitespace) so a 200 error page
        # can't poison the cache.
        if not body or len(body) > 512 or body[0] in "<{[" or any(c.isspace() for c in body):
            raise OSError(f"implausible token body ({len(body)} chars)")
        token = body
    except Exception as e:
        log.warning("token fetch failed for %s: %s", _safe_host(endpoint), e)
    with _token_lock:
        _token_cache[endpoint] = (time.monotonic(), token)  # cache success AND failure (negative)
    return token


def _inject_token(url: str) -> str:
    """If `url`'s host matches a TOKEN_INJECT rule and the rule's query param has no value
    anywhere in the query, set it to a freshly-minted token. Surgical — other params are
    left byte-for-byte intact and any #fragment is preserved. Server-side only, so the token
    never reaches the client. No-op when no rules are configured."""
    if not _token_rules:
        return url
    base, frag = urllib.parse.urldefrag(url)  # keep the token out of the fragment
    host = urllib.parse.urlsplit(base).hostname or ""
    for glob, endpoint, param in _token_rules:
        if not fnmatch.fnmatch(host, glob):
            continue
        occ = list(re.finditer(rf"[?&]{re.escape(param)}=([^&]*)", base))
        if any(o.group(1) for o in occ):
            return url  # the param already carries a value
        token = _get_token(endpoint)
        if not token:
            return url  # no token available; let the request proceed (and likely fail) as-is
        enc = urllib.parse.quote(token, safe="")
        if occ:  # present but empty -> fill the first occurrence
            o = occ[0]
            base = base[:o.start(1)] + enc + base[o.end(1):]
        else:    # absent -> append to the query
            base = f"{base}{'&' if '?' in base else '?'}{param}={enc}"
        return f"{base}#{frag}" if frag else base
    return url


def _chunklist_cache_get(url: str, now: float) -> tuple[bytes, str] | None:
    """Return (raw_bytes, base_url) for a fresh cached chunklist, else None."""
    if CHUNKLIST_TTL <= 0:
        return None
    with _seg_cache_lock:
        hit = _seg_cache.get(url)
    if hit and now - hit[0] < CHUNKLIST_TTL:
        return hit[1], hit[2]
    return None


def _chunklist_cache_get_stale(url: str, now: float) -> tuple[bytes, str] | None:
    """Return a cached chunklist still within the stale-serve grace window
    (TTL + CHUNKLIST_STALE_TTL), to ride out a brief upstream error. None if disabled/too old."""
    if CHUNKLIST_TTL <= 0 or CHUNKLIST_STALE_TTL <= 0:
        return None
    with _seg_cache_lock:
        hit = _seg_cache.get(url)
    if hit and now - hit[0] < CHUNKLIST_TTL + CHUNKLIST_STALE_TTL:
        return hit[1], hit[2]
    return None


def _chunklist_cache_put(url: str, raw: bytes, base: str, now: float) -> None:
    """Cache raw chunklist bytes + resolved base, bounding the cache size."""
    if CHUNKLIST_TTL <= 0:
        return
    with _seg_cache_lock:
        if len(_seg_cache) >= SEG_CACHE_MAX and url not in _seg_cache:
            _seg_cache.pop(next(iter(_seg_cache)))  # evict one arbitrary entry
        _seg_cache[url] = (now, raw, base)


def _fetch(url: str, extra_headers: dict, timeout: int) -> tuple[bytes, str]:
    """Fetch a URL fully into memory with retries and manual redirect following.

    Returns (body, final_url) where final_url reflects any redirects followed, so
    callers can resolve relative URLs against the location that actually served them.
    """
    h = _headers(extra_headers)
    t = urllib3.Timeout(connect=CONNECT_TIMEOUT, read=timeout)
    last_exc: Exception | None = None

    for attempt in range(FETCH_RETRIES + 1):
        try:
            current_url = url
            for _ in range(10):  # follow up to 10 redirects manually
                r = _pool.request("GET", _inject_token(current_url), headers=h, timeout=t,
                                  preload_content=True, redirect=False)

                if r.status in (301, 302, 303, 307, 308):
                    location = r.headers.get("Location", "")
                    if not location:
                        raise OSError("Redirect with no Location header")
                    current_url = urllib.parse.urljoin(current_url, location)
                    continue
                break

            if r.status in (301, 302, 303, 307, 308):
                raise OSError("Too many redirects")
            if r.status >= 500 and attempt < FETCH_RETRIES:
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status >= 400:
                raise OSError(f"HTTP {r.status}")
            return r.data, current_url
        except OSError:
            raise
        except Exception as e:
            last_exc = e
            if attempt < FETCH_RETRIES:
                time.sleep(0.3 * (attempt + 1))

    raise last_exc  # type: ignore[misc]


def _resolve_base(request_url: str, response_url: str | None) -> str:
    """Directory base for resolving relative URLs in a playlist, honoring redirects.

    ``response_url`` (e.g. urllib3's ``HTTPResponse.geturl()``) may be absolute when a
    redirect was followed, or just a bare path for a non-redirected PoolManager request.
    Joining it onto the absolute ``request_url`` yields a correct absolute base either way.
    """
    final = urllib.parse.urljoin(request_url, response_url or request_url)
    return final.rsplit("/", 1)[0] + "/"


# ---------------------------------------------------------------------------
# Playlist parsing and building
# ---------------------------------------------------------------------------

def parse_playlist(content: bytes) -> tuple[dict, str]:
    """Returns (channels, extm3u_header) preserving the original #EXTM3U line."""
    channels: dict = {}
    header = "#EXTM3U"
    lines = content.decode("utf-8", errors="replace").splitlines()
    cid = 1
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTM3U"):
            header = line
        elif line.startswith("#EXTINF"):
            info = line
            hdrs: dict = {}
            extra: list = []
            i += 1
            while i < len(lines) and lines[i].strip().startswith("#"):
                opt = lines[i].strip()
                matched = False
                for key, pattern in [
                    ("Referer",    r"#EXTVLCOPT:http-referrer=(.+)"),
                    ("User-Agent", r"#EXTVLCOPT:http-user-agent=(.+)"),
                    ("Origin",     r"#EXTVLCOPT:http-origin=(.+)"),
                ]:
                    m = re.match(pattern, opt)
                    if m:
                        hdrs[key] = m.group(1).strip()
                        matched = True
                if not matched:
                    # Preserve other per-channel tags (e.g. #EXTGRP) for the media server.
                    extra.append(opt)
                i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith("#"):
                    channels[str(cid)] = {"info": info, "url": url,
                                          "headers": hdrs, "extra": extra}
                    cid += 1
        i += 1
    return channels, header


def build_playlist(proxy_base: str, channels: dict, header: str) -> str:
    lines = [header]
    for cid, ch in channels.items():
        lines.append(ch["info"])
        lines.extend(ch.get("extra", []))
        lines.append(f"{proxy_base}/stream/{cid}")
    return "\n".join(lines)


def rewrite_m3u8(content: bytes, base_url: str, proxy_base: str, headers: dict) -> bytes:
    params = urllib.parse.urlencode(headers)

    def _proxied(target: str) -> str:
        abs_url = urllib.parse.urljoin(base_url, target)
        encoded = urllib.parse.quote(abs_url, safe="")
        return f"{proxy_base}/fetch?url={encoded}&{params}"

    def _rewrite_uri_attr(m):
        return f'URI="{_proxied(m.group(1))}"'

    result = []
    for line in content.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            # Segment / variant playlist URL.
            result.append(_proxied(stripped))
        elif stripped.startswith("#") and 'URI="' in stripped:
            # Tag carrying a resource URI (#EXT-X-KEY/#EXT-X-MAP/#EXT-X-MEDIA): proxy it
            # too so the AES key / init segment / alternate rendition gets header injection.
            result.append(re.sub(r'URI="([^"]*)"', _rewrite_uri_attr, line))
        else:
            result.append(line)
    return "\n".join(result).encode("utf-8")


# ---------------------------------------------------------------------------
# Playlist cache management
# ---------------------------------------------------------------------------

def refresh_playlist() -> bool:
    global _channels, _header, _cache_ts, _last_refresh_ok_ts, _last_refresh_err
    if not M3U_URL:
        log.error("M3U_URL is not set")
        _last_refresh_err = "M3U_URL is not set"
        return False
    log.info("Fetching playlist: %s", _safe_host(M3U_URL))
    try:
        data, _ = _fetch(M3U_URL, {}, PLAYLIST_TIMEOUT)
        log.info("Fetched %s bytes", f"{len(data):,}")
        channels, header = parse_playlist(data)
        with _cache_lock:
            _channels = channels
            _header = header
            _cache_ts = time.monotonic()
        if channels:
            log.info("Loaded %d channels", len(channels))
        else:
            log.warning("0 channels parsed — playlist may be empty or in an unexpected format")
        _last_refresh_ok_ts = time.monotonic()
        _last_refresh_err = None
        return True
    except Exception as e:
        log.error("Playlist refresh failed: %s: %s", type(e).__name__, e)
        _last_refresh_err = f"{type(e).__name__}: {e}"
        return False


def _background_refresher() -> None:
    """Daemon thread: refresh playlist every PLAYLIST_TTL seconds."""
    while True:
        time.sleep(PLAYLIST_TTL)
        refresh_playlist()


def _ensure_channels() -> tuple[dict, str]:
    """
    Return (channels, header) from cache.
    - Blocks synchronously only on first load (empty cache).
    - Triggers a background refresh when the TTL has expired; serves stale data immediately.
    """
    with _cache_lock:
        channels = _channels
        header = _header
        age = time.monotonic() - _cache_ts

    if not channels:
        refresh_playlist()
        with _cache_lock:
            channels = _channels
            header = _header
    elif age > PLAYLIST_TTL:
        threading.Thread(target=refresh_playlist, daemon=True).start()

    return channels, header


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):

    # HTTP/1.1 enables client keep-alive (Emby reuses one TCP connection for the many
    # segment requests of a stream). Correct framing is mandatory: every response must
    # carry a Content-Length or close the connection, or a kept-alive socket desyncs.
    protocol_version = "HTTP/1.1"
    # Reap idle kept-alive connections so they don't pin a thread indefinitely.
    timeout = CLIENT_TIMEOUT

    # Set True once a response (status line + headers) has begun, so error paths
    # know not to emit a second HTTP status line over an in-flight response.
    _started = False

    # Per-request outcome for /fetch accounting: "ok", "err" (aborted mid-body) or
    # "stale" (served a cached chunklist because the upstream errored).
    _outcome = "ok"
    _degraded_reason = ""

    def log_message(self, fmt, *args):
        # Strip query strings from the logged request line: the proxied /fetch URLs carry
        # Referer/Origin/User-Agent values that can be provider tokens. Keep path only.
        msg = re.sub(r"\?\S*", "?…", fmt % args)
        log.info("%s – %s", self.address_string(), msg)

    def log_error(self, fmt, *args):
        # "Request timed out" here is the idle keep-alive reaper closing a connection
        # Emby left idle past CLIENT_TIMEOUT — expected and benign, so keep it at DEBUG.
        # Genuine handler errors go to WARNING (with query strings stripped).
        msg = re.sub(r"\?\S*", "?…", fmt % args)
        if "timed out" in msg.lower():
            log.debug("idle connection closed (%s)", self.address_string())
        else:
            log.warning("%s – %s", self.address_string(), msg)

    def proxy_base(self) -> str:
        host = self.headers.get("Host", f"127.0.0.1:{PORT}")
        return f"http://{host.split(':')[0]}:{PORT}"

    def _send(self, code: int, content_type: str, data: bytes) -> None:
        self._started = True
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except Exception:
                # Body partially written: never reuse a possibly-desynced keep-alive
                # socket (covers client disconnect and any other write error).
                self.close_connection = True

    def _error(self, code: int, close: bool = False) -> None:
        self._started = True
        self.send_response(code)
        self.send_header("Content-Length", "0")  # frame the empty body for keep-alive
        if close:
            self.send_header("Connection", "close")  # also sets self.close_connection
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        # Optional cap on concurrently-PROCESSING requests. Past the cap, shed load: fail
        # fast (non-blocking, so no thread parks waiting) and close the connection so the
        # client doesn't immediately retry on the same socket. Off unless MAX_CONCURRENT set.
        # /health and /logs bypass the cap so monitoring stays truthful even under load.
        if _sem is None or urllib.parse.urlparse(self.path).path in ("/health", "/logs"):
            self._handle()
            return
        if not _sem.acquire(blocking=False):
            self._error(503, close=True)
            return
        try:
            self._handle()
        finally:
            _sem.release()

    def _handle(self):
        global _last_request_err
        # One handler instance serves multiple requests on a kept-alive connection,
        # so reset per-request state that error paths rely on.
        self._started = False
        self._outcome = "ok"
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        query  = urllib.parse.parse_qs(parsed.query)

        # ── /health ──────────────────────────────────────────────────────────
        if path == "/health":
            now = time.monotonic()
            with _cache_lock:
                n = len(_channels)
                cache_age = round(now - _cache_ts, 1) if _cache_ts else None
            with _stats_lock:
                stats = dict(_stats)
            body = {
                "ok": bool(n),
                "version": __version__,
                "channels": n,
                "cache_age_s": cache_age,
                "last_refresh_ok_age_s": round(now - _last_refresh_ok_ts, 1) if _last_refresh_ok_ts else None,
                "last_refresh_error": _last_refresh_err,
                "last_request_error": _last_request_err,
                **stats,
            }
            self._send(200 if n else 503, "application/json", json.dumps(body).encode())
            return

        # ── /logs (opt-in via LOGS_ENDPOINT) ─────────────────────────────────
        if path == "/logs":
            if not LOGS_ENDPOINT:
                self._error(404)
                return
            lines = list(_log_ring)
            tail = query.get("tail", [None])[0]
            if tail and tail.isdigit():
                lines = lines[-int(tail):]
            self._send(200, "text/plain; charset=utf-8", ("\n".join(lines) + "\n").encode())
            return

        # ── /playlist.m3u ────────────────────────────────────────────────────
        if path in ("/playlist.m3u", "/"):
            channels, header = _ensure_channels()
            if not channels:
                self._error(503)
                return
            m3u = build_playlist(self.proxy_base(), channels, header)
            self._send(200, "application/x-mpegurl", m3u.encode())
            return

        # ── /stream/<id> ─────────────────────────────────────────────────────
        m = re.match(r"^/stream/(\w+)$", path)
        if m:
            cid = m.group(1)
            with _cache_lock:
                ch = _channels.get(cid)
            if not ch:
                self._error(404)
                return
            hdrs = dict(ch["headers"])
            name = ch['info'].split(',')[-1].strip()
            log.info("TUNE %s", name)
            try:
                t0 = time.monotonic()
                data, final_url = _fetch(ch["url"], hdrs, STREAM_TIMEOUT)
                elapsed = time.monotonic() - t0
                base = _resolve_base(ch["url"], final_url)
                if b"#EXT" in data[:512]:
                    data = rewrite_m3u8(data, base, self.proxy_base(), hdrs)
                    log.info("MASTER m3u8 %.0fms (%dB)", elapsed * 1000, len(data))
                else:
                    log.info("STREAM %.0fms (%dB)", elapsed * 1000, len(data))
                self._send(200, "application/x-mpegurl", data)
                _bump("stream_ok")
            except Exception as e:
                log.error("Stream %s error: %s", cid, e)
                _last_request_err = f"stream {cid}: {type(e).__name__}: {e}"
                _bump("stream_err")
                if not self._started:
                    self._error(502)
            return

        # ── /fetch?url=... ───────────────────────────────────────────────────
        if path == "/fetch":
            raw = query.get("url", [None])[0]
            if not raw:
                self._error(400)
                return
            url  = urllib.parse.unquote(raw)
            hdrs = {k: query[k][0] for k in ("Referer", "User-Agent", "Origin") if k in query}
            try:
                t0 = time.monotonic()
                self._proxy_segment(url, hdrs)
                elapsed = time.monotonic() - t0
                if self._outcome == "ok":
                    label = "CHUNK" if url.endswith(".m3u8") or url.endswith(".m3u") else "SEG"
                    log.info("%s %.0fms %s", label, elapsed * 1000, url.split('/')[-1].split('?')[0])
                    _bump("fetch_ok")
                else:
                    # Aborted mid-body, or served stale because the upstream errored — record
                    # it so /health reflects the degradation rather than showing all-green.
                    _last_request_err = self._degraded_reason
                    _bump("fetch_err" if self._outcome == "err" else "fetch_stale")
            except Exception as e:
                # Log host + filename only — the full URL/query may carry CDN tokens.
                fname = url.split("?")[0].rsplit("/", 1)[-1]
                log.error("Fetch error %s …/%s: %s", _safe_host(url), fname, e)
                _last_request_err = f"{_safe_host(url)} …/{fname}: {type(e).__name__}: {e}"
                _bump("fetch_err")
                if not self._started:
                    self._error(502)
            return

        self._error(404)

    def _serve_stale_chunklist(self, url: str, now: float, hdrs: dict, reason: str) -> bool:
        """If a recent chunklist for `url` is still within the stale grace window, serve it
        (re-running the per-request rewrite) and return True; else return False."""
        stale = _chunklist_cache_get_stale(url, now)
        if stale is None:
            return False
        raw, base = stale
        log.warning("serving stale chunklist (%s) for %s", reason, _safe_host(url))
        self._outcome = "stale"
        self._degraded_reason = f"stale chunklist served ({reason}) {_safe_host(url)}"
        self._send(200, "application/x-mpegurl", rewrite_m3u8(raw, base, self.proxy_base(), hdrs))
        return True

    def _proxy_segment(self, url: str, hdrs: dict) -> None:
        """
        Proxy a segment or sub-playlist.
        - HEAD: lightweight upstream HEAD, no body transfer.
        - m3u8: peek first bytes, buffer + rewrite URLs.
        - TS:   stream in chunks without buffering the full segment.
        """
        h = _headers(hdrs)
        t = urllib3.Timeout(connect=CONNECT_TIMEOUT, read=STREAM_TIMEOUT)

        if self.command == "HEAD":
            r = _pool.request("HEAD", _inject_token(url), headers=h, timeout=t, preload_content=False)
            r.drain_conn()
            self._started = True
            self.send_response(200 if r.status < 400 else r.status)
            self.send_header("Content-Type", r.headers.get("Content-Type", "video/mp2t"))
            if cl := r.headers.get("Content-Length"):
                self.send_header("Content-Length", cl)
            self.end_headers()
            return

        # Serve a fresh cached chunklist without touching upstream. Only chunklists
        # are ever cached, so a hit unambiguously means an m3u8.
        now = time.monotonic()
        cached = _chunklist_cache_get(url, now)
        if cached is not None:
            raw, base = cached
            self._send(200, "application/x-mpegurl",
                       rewrite_m3u8(raw, base, self.proxy_base(), hdrs))
            return

        try:
            r = _pool.request("GET", _inject_token(url), headers=h, timeout=t, preload_content=False)
        except Exception as e:
            # Connection/timeout error: ride out a brief blip with the last-good chunklist.
            if self._serve_stale_chunklist(url, now, hdrs, type(e).__name__):
                return
            raise
        if r.status >= 400:
            r.drain_conn()
            # Transient upstream error: serve a recently-cached chunklist if we have one so a
            # brief blip doesn't drop the stream; otherwise surface the real status (logged +
            # counted by the /fetch handler).
            if self._serve_stale_chunklist(url, now, hdrs, f"upstream HTTP {r.status}"):
                return
            raise OSError(f"upstream HTTP {r.status}")

        try:
            # Peek at first 512 bytes to determine content type without buffering everything.
            peek = r.read(512)

            if b"#EXT" in peek:
                # Sub-playlist: buffer the rest and rewrite URLs against the
                # post-redirect URL so relative segment paths resolve correctly.
                rest = r.read()
                r.release_conn()
                base = _resolve_base(url, r.geturl())
                raw = peek + rest
                _chunklist_cache_put(url, raw, base, now)
                data = rewrite_m3u8(raw, base, self.proxy_base(), hdrs)
                self._send(200, "application/x-mpegurl", data)
            else:
                # TS segment: stream directly to client without buffering.
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                if cl := r.headers.get("Content-Length"):
                    self.send_header("Content-Length", cl)
                else:
                    # No upstream length: we can't frame the body, so close the
                    # connection after it to keep a kept-alive socket in sync.
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self._started = True
                # Bound the time spent waiting on the UPSTREAM for this segment. A trickling
                # upstream sends bytes slowly enough to keep dodging the per-read timeout but
                # can hang for ~a minute and freeze the player. We accumulate only upstream
                # read time (not client write time, which is legitimate backpressure on a slow
                # client) and abort + close once it exceeds SEGMENT_TIMEOUT, so the client
                # retries instead of stalling.
                try:
                    self.wfile.write(peek)
                    stream = r.stream(CHUNK_SIZE)
                    upstream_read = 0.0
                    while True:
                        t_read = time.monotonic()
                        try:
                            chunk = next(stream)
                        except StopIteration:
                            break
                        upstream_read += time.monotonic() - t_read
                        if SEGMENT_TIMEOUT > 0 and upstream_read > SEGMENT_TIMEOUT:
                            raise TimeoutError(f"upstream stalled past {SEGMENT_TIMEOUT}s")
                        self.wfile.write(chunk)
                    r.release_conn()
                except (BrokenPipeError, ConnectionResetError):
                    # Client went away mid-stream; tear down the incomplete upstream connection.
                    self.close_connection = True
                    r.close()
                except Exception as e:
                    # Upstream stalled/failed mid-body (headers already sent): close rather
                    # than desync a kept-alive socket, and don't drain a pathological trickle
                    # (which would re-block this thread). Mark degraded so /fetch counts it.
                    self.close_connection = True
                    r.close()
                    self._outcome = "err"
                    self._degraded_reason = f"segment aborted: {type(e).__name__}: {e}"
                    log.warning("stream aborted mid-body: %s: %s", type(e).__name__, e)
        except Exception:
            r.drain_conn()
            raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if not M3U_URL:
        log.warning("M3U_URL is not set")

    log.info("m3uproxy v%s starting", __version__)
    log.info("config: M3U_URL host=%s | playlist_ttl=%ss | chunklist_ttl=%ss (stale=%ss) | "
             "segment_timeout=%ss | pool num_pools=%d maxsize=%d | max_concurrent=%s | "
             "client_timeout=%ss | token_rules=%d | log_level=%s",
             _safe_host(M3U_URL) or "(unset)", PLAYLIST_TTL, CHUNKLIST_TTL, CHUNKLIST_STALE_TTL,
             SEGMENT_TIMEOUT, POOL_NUM_POOLS, POOL_MAXSIZE, MAX_CONCURRENT or "unlimited",
             CLIENT_TIMEOUT, len(_token_rules), LOG_LEVEL)

    refresh_playlist()

    threading.Thread(target=_background_refresher, daemon=True, name="playlist-refresher").start()

    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    log.info("listening on %s:%d", HOST, PORT)
    server.serve_forever()
