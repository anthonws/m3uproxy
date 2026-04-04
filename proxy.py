#!/usr/bin/env python3
"""
m3uproxy - A simple HLS proxy for M3U playlists with per-channel header injection.

Reads #EXTVLCOPT directives from M3U playlists and injects the correct
Referer, User-Agent and Origin headers when proxying HLS streams.
This allows media servers like Emby/Jellyfin/Plex to play streams that
require specific HTTP headers without any special configuration.
"""

import re
import os
import gzip
import threading
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Configuration (overridable via environment variables) ---
M3U_URL   = os.environ.get("M3U_URL",    "https://github.com/LITUATUI/M3UPT/raw/main/M3U/M3UPT.m3u")
HOST      = os.environ.get("PROXY_HOST", "0.0.0.0")
PORT      = int(os.environ.get("PROXY_PORT", "7654"))
UA        = os.environ.get("DEFAULT_UA", "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0")

# --- In-memory cache ---
cache      = {"channels": {}}
cache_lock = threading.Lock()


def fetch_url(url, headers=None):
    """Fetch a URL, handling gzip decompression automatically."""
    h = dict(headers or {})
    h.setdefault("User-Agent", UA)
    h["Accept-Encoding"] = "gzip, deflate"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        data     = r.read()
        final    = r.url
        encoding = r.headers.get("Content-Encoding", "")
        if encoding == "gzip" or data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        return data, final


def parse_playlist(content):
    """
    Parse an M3U playlist and extract channels with their EXTVLCOPT headers.
    Returns a dict of {channel_id: {info, url, headers}}.
    """
    channels = {}
    lines    = content.decode("utf-8").splitlines()
    i        = 0
    cid      = 1

    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            info    = line
            headers = {}
            i      += 1
            while i < len(lines) and lines[i].strip().startswith("#"):
                opt = lines[i].strip()
                for key, pattern in [
                    ("Referer",    r'#EXTVLCOPT:http-referrer=(.+)'),
                    ("User-Agent", r'#EXTVLCOPT:http-user-agent=(.+)'),
                    ("Origin",     r'#EXTVLCOPT:http-origin=(.+)'),
                ]:
                    m = re.match(pattern, opt)
                    if m:
                        headers[key] = m.group(1).strip()
                i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith("#"):
                    channels[str(cid)] = {"info": info, "url": url, "headers": headers}
                    cid += 1
        i += 1

    return channels


def build_playlist(proxy_base, channels):
    """Build an M3U playlist with all stream URLs rewritten to go through this proxy."""
    lines = ["#EXTM3U"]
    for cid, ch in channels.items():
        lines.append(ch["info"])
        lines.append(f"{proxy_base}/stream/{cid}")
    return "\n".join(lines)


def rewrite_m3u8(content, base_url, proxy_base, headers):
    """
    Rewrite relative URLs in an m3u8 playlist to absolute proxy URLs,
    embedding the upstream headers as query parameters.
    """
    lines  = content.decode("utf-8").splitlines()
    result = []
    params = urllib.parse.urlencode({k: v for k, v in headers.items()})

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            abs_url = urllib.parse.urljoin(base_url, stripped)
            encoded = urllib.parse.quote(abs_url, safe="")
            result.append(f"{proxy_base}/fetch?url={encoded}&{params}")
        else:
            result.append(line)

    return "\n".join(result).encode("utf-8")


def refresh_playlist():
    """Fetch and parse the upstream M3U playlist into the cache."""
    try:
        content, _ = fetch_url(M3U_URL)
        channels   = parse_playlist(content)
        with cache_lock:
            cache["channels"] = channels
        print(f"[m3uproxy] Loaded {len(channels)} channels from {M3U_URL}", flush=True)
    except Exception as e:
        print(f"[m3uproxy] Failed to refresh playlist: {e}", flush=True)


class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[m3uproxy] {self.address_string()} {fmt % args}", flush=True)

    def proxy_base(self):
        host = self.headers.get("Host", f"127.0.0.1:{PORT}").split(":")[0]
        return f"http://{host}:{PORT}"

    def send(self, code, content_type, data):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query  = urllib.parse.parse_qs(parsed.query)
        path   = parsed.path

        # ── /playlist.m3u  ────────────────────────────────────────────────
        if path in ("/playlist.m3u", "/"):
            refresh_playlist()
            with cache_lock:
                m3u = build_playlist(self.proxy_base(), cache["channels"])
            self.send(200, "application/x-mpegurl", m3u.encode("utf-8"))
            return

        # ── /stream/<id>  ─────────────────────────────────────────────────
        m = re.match(r'^/stream/(\w+)$', path)
        if m:
            cid = m.group(1)
            with cache_lock:
                ch = cache["channels"].get(cid)
            if not ch:
                self.send_response(404); self.end_headers(); return

            hdrs = dict(ch["headers"])
            print(f"[m3uproxy] Stream {cid}: {ch['info'].split(',')[-1].strip()} -> {ch['url']}", flush=True)
            try:
                data, final = fetch_url(ch["url"], hdrs)
                base        = final.rsplit("/", 1)[0] + "/"
                if b"#EXT" in data[:512]:
                    data = rewrite_m3u8(data, base, self.proxy_base(), hdrs)
                self.send(200, "application/x-mpegurl", data)
            except Exception as e:
                print(f"[m3uproxy] Stream error: {e}", flush=True)
                self.send_response(502); self.end_headers()
            return

        # ── /fetch?url=...  ───────────────────────────────────────────────
        if path == "/fetch":
            url = query.get("url", [None])[0]
            if not url:
                self.send_response(400); self.end_headers(); return
            url  = urllib.parse.unquote(url)
            hdrs = {k: query[k][0] for k in ("Referer", "User-Agent", "Origin") if k in query}
            try:
                data, final = fetch_url(url, hdrs)
                base        = final.rsplit("/", 1)[0] + "/"
                ctype       = "application/x-mpegurl" if (url.endswith(".m3u8") or b"#EXT" in data[:100]) else "video/mp2t"
                if b"#EXT" in data[:512]:
                    data = rewrite_m3u8(data, base, self.proxy_base(), hdrs)
                self.send(200, ctype, data)
            except Exception as e:
                print(f"[m3uproxy] Fetch error for {url}: {e}", flush=True)
                self.send_response(502); self.end_headers()
            return

        self.send_response(404); self.end_headers()


if __name__ == "__main__":
    refresh_playlist()
    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    print(f"[m3uproxy] Listening on {HOST}:{PORT}", flush=True)
    print(f"[m3uproxy] Playlist URL: http://<your-ip>:{PORT}/playlist.m3u", flush=True)
    server.serve_forever()
