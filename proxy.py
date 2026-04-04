#!/usr/bin/env python3
"""
m3uproxy – lightweight HLS proxy with per-channel header injection.

Reads #EXTVLCOPT directives from M3U playlists and injects the correct
Referer, User-Agent and Origin headers when proxying HLS streams.
"""

import gzip
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import urllib3

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
M3U_URL          = os.environ.get("M3U_URL", "")
HOST             = os.environ.get("PROXY_HOST", "0.0.0.0")
PORT             = int(os.environ.get("PROXY_PORT", "7654"))
UA               = os.environ.get("DEFAULT_UA",
                       "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0")
PLAYLIST_TTL     = int(os.environ.get("PLAYLIST_TTL",     "86400"))  # seconds between background refreshes
CONNECT_TIMEOUT  = int(os.environ.get("CONNECT_TIMEOUT",  "5"))    # TCP connect timeout
STREAM_TIMEOUT   = int(os.environ.get("STREAM_TIMEOUT",   "10"))   # per-segment read timeout
PLAYLIST_TIMEOUT = int(os.environ.get("PLAYLIST_TIMEOUT", "20"))   # playlist fetch timeout
FETCH_RETRIES    = int(os.environ.get("FETCH_RETRIES",    "2"))    # retries on transient errors
CHUNK_SIZE       = 128 * 1024                                       # 128 KB streaming chunks

# ---------------------------------------------------------------------------
# Connection pool  (shared across all threads)
# ---------------------------------------------------------------------------
_pool = urllib3.PoolManager(
    num_pools=20,
    maxsize=10,
    retries=urllib3.Retry(total=False, redirect=10),
)

# ---------------------------------------------------------------------------
# Playlist cache
# ---------------------------------------------------------------------------
_channels: dict = {}
_cache_ts: float = 0.0
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers(extra: dict) -> dict:
    return {"User-Agent": UA, **extra}


def _fetch(url: str, extra_headers: dict, timeout: int) -> bytes:
    """Fetch a URL fully into memory with retries. urllib3 handles decompression."""
    h = _headers(extra_headers)
    t = urllib3.Timeout(connect=CONNECT_TIMEOUT, read=timeout)
    last_exc: Exception | None = None

    for attempt in range(FETCH_RETRIES + 1):
        try:
            r = _pool.request("GET", url, headers=h, timeout=t, preload_content=True)
            if r.status >= 500 and attempt < FETCH_RETRIES:
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status >= 400:
                raise OSError(f"HTTP {r.status}")
            return r.data
        except OSError:
            raise
        except Exception as e:
            last_exc = e
            if attempt < FETCH_RETRIES:
                time.sleep(0.3 * (attempt + 1))

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Playlist parsing and building
# ---------------------------------------------------------------------------

def parse_playlist(content: bytes) -> dict:
    channels: dict = {}
    lines = content.decode("utf-8", errors="replace").splitlines()
    cid = 1
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            info = line
            hdrs: dict = {}
            i += 1
            while i < len(lines) and lines[i].strip().startswith("#"):
                opt = lines[i].strip()
                for key, pattern in [
                    ("Referer",    r"#EXTVLCOPT:http-referrer=(.+)"),
                    ("User-Agent", r"#EXTVLCOPT:http-user-agent=(.+)"),
                    ("Origin",     r"#EXTVLCOPT:http-origin=(.+)"),
                ]:
                    m = re.match(pattern, opt)
                    if m:
                        hdrs[key] = m.group(1).strip()
                i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith("#"):
                    channels[str(cid)] = {"info": info, "url": url, "headers": hdrs}
                    cid += 1
        i += 1
    return channels


def build_playlist(proxy_base: str, channels: dict) -> str:
    lines = ["#EXTM3U"]
    for cid, ch in channels.items():
        lines.append(ch["info"])
        lines.append(f"{proxy_base}/stream/{cid}")
    return "\n".join(lines)


def rewrite_m3u8(content: bytes, base_url: str, proxy_base: str, headers: dict) -> bytes:
    params = urllib.parse.urlencode(headers)
    result = []
    for line in content.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            abs_url = urllib.parse.urljoin(base_url, stripped)
            encoded = urllib.parse.quote(abs_url, safe="")
            result.append(f"{proxy_base}/fetch?url={encoded}&{params}")
        else:
            result.append(line)
    return "\n".join(result).encode("utf-8")


# ---------------------------------------------------------------------------
# Playlist cache management
# ---------------------------------------------------------------------------

def refresh_playlist() -> bool:
    global _channels, _cache_ts
    if not M3U_URL:
        print("[m3uproxy] M3U_URL is not set", flush=True)
        return False
    try:
        data = _fetch(M3U_URL, {}, PLAYLIST_TIMEOUT)
        channels = parse_playlist(data)
        with _cache_lock:
            _channels = channels
            _cache_ts = time.monotonic()
        print(f"[m3uproxy] Loaded {len(channels)} channels", flush=True)
        return True
    except Exception as e:
        print(f"[m3uproxy] Playlist refresh failed: {e}", flush=True)
        return False


def _background_refresher() -> None:
    """Daemon thread: refresh playlist every PLAYLIST_TTL seconds."""
    while True:
        time.sleep(PLAYLIST_TTL)
        refresh_playlist()


def _ensure_channels() -> dict:
    """
    Return the current channel cache.
    - Blocks synchronously only on first load (empty cache).
    - Triggers a background refresh when the TTL has expired; serves stale data immediately.
    """
    with _cache_lock:
        channels = _channels
        age = time.monotonic() - _cache_ts

    if not channels:
        refresh_playlist()
        with _cache_lock:
            channels = _channels
    elif age > PLAYLIST_TTL:
        threading.Thread(target=refresh_playlist, daemon=True).start()

    return channels


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[m3uproxy] {self.address_string()} – {fmt % args}", flush=True)

    def proxy_base(self) -> str:
        host = self.headers.get("Host", f"127.0.0.1:{PORT}")
        return f"http://{host.split(':')[0]}:{PORT}"

    def _send(self, code: int, content_type: str, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, code: int) -> None:
        self.send_response(code)
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        query  = urllib.parse.parse_qs(parsed.query)

        # ── /playlist.m3u ────────────────────────────────────────────────────
        if path in ("/playlist.m3u", "/"):
            channels = _ensure_channels()
            if not channels:
                self._error(503)
                return
            m3u = build_playlist(self.proxy_base(), channels)
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
            print(f"[m3uproxy] → {ch['info'].split(',')[-1].strip()}", flush=True)
            try:
                data = _fetch(ch["url"], hdrs, STREAM_TIMEOUT)
                base = ch["url"].rsplit("/", 1)[0] + "/"
                if b"#EXT" in data[:512]:
                    data = rewrite_m3u8(data, base, self.proxy_base(), hdrs)
                self._send(200, "application/x-mpegurl", data)
            except Exception as e:
                print(f"[m3uproxy] Stream {cid} error: {e}", flush=True)
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
                self._proxy_segment(url, hdrs)
            except Exception as e:
                print(f"[m3uproxy] Fetch error {url}: {e}", flush=True)
                self._error(502)
            return

        self._error(404)

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
            r = _pool.request("HEAD", url, headers=h, timeout=t, preload_content=False)
            r.drain_conn()
            self.send_response(200 if r.status < 400 else r.status)
            self.send_header("Content-Type", r.headers.get("Content-Type", "video/mp2t"))
            if cl := r.headers.get("Content-Length"):
                self.send_header("Content-Length", cl)
            self.end_headers()
            return

        r = _pool.request("GET", url, headers=h, timeout=t, preload_content=False)
        if r.status >= 400:
            r.drain_conn()
            self._error(502)
            return

        try:
            # Peek at first 512 bytes to determine content type without buffering everything.
            peek = r.read(512)

            if b"#EXT" in peek:
                # Sub-playlist: buffer the rest and rewrite URLs.
                rest = r.read()
                r.release_conn()
                data = rewrite_m3u8(peek + rest, url.rsplit("/", 1)[0] + "/",
                                    self.proxy_base(), hdrs)
                self._send(200, "application/x-mpegurl", data)
            else:
                # TS segment: stream directly to client without buffering.
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                if cl := r.headers.get("Content-Length"):
                    self.send_header("Content-Length", cl)
                self.end_headers()
                try:
                    self.wfile.write(peek)
                    for chunk in r.stream(CHUNK_SIZE):
                        self.wfile.write(chunk)
                    r.release_conn()
                except (BrokenPipeError, ConnectionResetError):
                    r.drain_conn()  # client disconnected; close upstream cleanly
        except Exception:
            r.drain_conn()
            raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not M3U_URL:
        print("[m3uproxy] WARNING: M3U_URL is not set", flush=True)

    refresh_playlist()

    threading.Thread(target=_background_refresher, daemon=True, name="playlist-refresher").start()

    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    print(f"[m3uproxy] Listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()
