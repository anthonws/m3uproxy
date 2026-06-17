"""Unit tests for the pure parsing / rewriting logic in proxy.py.

Importing the module is side-effect free (the server only starts under
``if __name__ == "__main__"``), so these run with no network access.
"""

import http.client
import json
import os
import socket
import sys
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxy  # noqa: E402


SAMPLE = b'''#EXTM3U url-tvg="http://epg.example/guide.xml"
#EXTINF:-1 tvg-id="A",Channel A
#EXTVLCOPT:http-referrer=https://ref.example
#EXTVLCOPT:http-user-agent=Mozilla/Test
#EXTVLCOPT:http-origin=https://ref.example
#EXTGRP:News
http://up.example/a/stream.m3u8
#EXTINF:-1 tvg-id="B",Channel B
http://up.example/b/stream.m3u8
'''

ATTR_HEADER = '#EXTM3U url-tvg="http://epg.example/guide.xml"'


class ParsePlaylistTest(unittest.TestCase):
    def test_parses_channels_headers_and_header_line(self):
        channels, header = proxy.parse_playlist(SAMPLE)
        self.assertEqual(header, ATTR_HEADER)
        self.assertEqual(set(channels.keys()), {"1", "2"})
        self.assertEqual(channels["1"]["headers"], {
            "Referer": "https://ref.example",
            "User-Agent": "Mozilla/Test",
            "Origin": "https://ref.example",
        })
        self.assertEqual(channels["1"]["url"], "http://up.example/a/stream.m3u8")
        self.assertEqual(channels["2"]["headers"], {})

    def test_preserves_non_vlcopt_tags(self):
        channels, _ = proxy.parse_playlist(SAMPLE)
        self.assertIn("#EXTGRP:News", channels["1"].get("extra", []))


class HeaderRegressionTest(unittest.TestCase):
    """Guards the missing ``global _header`` bug: ``refresh_playlist`` must publish
    the attributed ``#EXTM3U`` line to the module global so ``build_playlist`` emits
    it. Reverting the global declaration makes these tests fail."""

    def setUp(self):
        self._orig_fetch = proxy._fetch
        self._orig_url = proxy.M3U_URL
        proxy.M3U_URL = "http://up.example/playlist.m3u"
        proxy._fetch = lambda *a, **k: (SAMPLE, "http://up.example/playlist.m3u")

    def tearDown(self):
        proxy._fetch = self._orig_fetch
        proxy.M3U_URL = self._orig_url

    def test_refresh_then_build_preserves_header(self):
        self.assertTrue(proxy.refresh_playlist())
        self.assertEqual(proxy._header, ATTR_HEADER)
        out = proxy.build_playlist("http://proxy:7654", proxy._channels, proxy._header)
        self.assertEqual(out.splitlines()[0], ATTR_HEADER)

    def test_build_includes_extra_tag(self):
        proxy.refresh_playlist()
        out = proxy.build_playlist("http://proxy:7654", proxy._channels, proxy._header)
        self.assertIn("#EXTGRP:News", out.splitlines())


class RewriteM3u8Test(unittest.TestCase):
    def test_relative_segment_rewritten(self):
        content = b"#EXTINF:4.0,\nseg1.ts\n"
        out = proxy.rewrite_m3u8(content, "http://up.example/live/",
                                 "http://proxy:7654", {}).decode()
        lines = out.splitlines()
        self.assertEqual(lines[0], "#EXTINF:4.0,")
        expected = ("http://proxy:7654/fetch?url="
                    + urllib.parse.quote("http://up.example/live/seg1.ts", safe=""))
        self.assertTrue(lines[1].startswith(expected), lines[1])

    def test_plain_comment_passthrough(self):
        content = b"#EXT-X-VERSION:5\nseg.ts\n"
        out = proxy.rewrite_m3u8(content, "http://up.example/",
                                 "http://proxy:7654", {}).decode()
        self.assertIn("#EXT-X-VERSION:5", out.splitlines())

    def test_ext_x_key_uri_rewritten(self):
        content = (b'#EXT-X-KEY:METHOD=AES-128,URI="https://up.example/key.bin"\n'
                   b'seg.ts\n')
        out = proxy.rewrite_m3u8(content, "http://up.example/",
                                 "http://proxy:7654", {}).decode()
        key_line = next(ln for ln in out.splitlines() if ln.startswith("#EXT-X-KEY"))
        self.assertIn("/fetch?url=", key_line)
        self.assertIn(urllib.parse.quote("https://up.example/key.bin", safe=""), key_line)
        self.assertTrue(key_line.startswith('#EXT-X-KEY:METHOD=AES-128,URI="'))


class ResolveBaseTest(unittest.TestCase):
    """Guards the segment-base regression: urllib3's geturl() returns a bare PATH for
    a non-redirected PoolManager request, so the base must be re-absolutised."""

    def test_path_only_response_url_is_absolutised(self):
        # The exact shape that broke playback: geturl() returns only the path.
        base = proxy._resolve_base(
            "https://cdn.example/live/smil:x.smil/chunklist.m3u8",
            "/live/smil:x.smil/chunklist.m3u8",
        )
        self.assertEqual(base, "https://cdn.example/live/smil:x.smil/")

    def test_none_response_url_falls_back_to_request(self):
        base = proxy._resolve_base("https://cdn.example/a/b/chunk.m3u8", None)
        self.assertEqual(base, "https://cdn.example/a/b/")

    def test_absolute_redirect_response_url_is_used(self):
        base = proxy._resolve_base(
            "https://cdn.example/a/chunk.m3u8",
            "https://cdn2.example/x/y/chunk.m3u8",
        )
        self.assertEqual(base, "https://cdn2.example/x/y/")


class ChunklistCacheTest(unittest.TestCase):
    def setUp(self):
        self._orig_ttl = proxy.CHUNKLIST_TTL
        proxy._seg_cache.clear()

    def tearDown(self):
        proxy.CHUNKLIST_TTL = self._orig_ttl
        proxy._seg_cache.clear()

    def test_hit_within_ttl_then_miss_after(self):
        proxy.CHUNKLIST_TTL = 2
        proxy._chunklist_cache_put("u", b"raw", "http://b/", now=100.0)
        self.assertEqual(proxy._chunklist_cache_get("u", now=101.0), (b"raw", "http://b/"))
        self.assertIsNone(proxy._chunklist_cache_get("u", now=103.0))  # past TTL

    def test_ttl_zero_disables_cache(self):
        proxy.CHUNKLIST_TTL = 0
        proxy._chunklist_cache_put("u", b"raw", "http://b/", now=100.0)
        self.assertEqual(len(proxy._seg_cache), 0)
        self.assertIsNone(proxy._chunklist_cache_get("u", now=100.0))

    def test_size_is_bounded(self):
        proxy.CHUNKLIST_TTL = 60
        for i in range(proxy.SEG_CACHE_MAX + 50):
            proxy._chunklist_cache_put(f"u{i}", b"x", "http://b/", now=1.0)
        self.assertLessEqual(len(proxy._seg_cache), proxy.SEG_CACHE_MAX)


class KeepAliveFramingTest(unittest.TestCase):
    """Integration test on a real server: HTTP/1.1 responses must be framed so a
    kept-alive connection can be reused. A missing Content-Length would desync the
    socket and the second request on the same connection would hang/fail."""

    @classmethod
    def setUpClass(cls):
        proxy._channels = {"1": {"info": "#EXTINF:-1,Test", "url": "http://x/s.m3u8",
                                 "headers": {}, "extra": []}}
        proxy._header = "#EXTM3U"
        proxy._cache_ts = 1.0
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_keepalive_reuses_one_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/playlist.m3u")
        r1 = c.getresponse()
        b1 = r1.read()
        self.assertEqual(r1.status, 200)
        self.assertEqual(r1.version, 11)  # HTTP/1.1
        self.assertIsNotNone(r1.getheader("Content-Length"))
        # Second request on the SAME socket — only works if framing was correct.
        c.request("GET", "/playlist.m3u")
        r2 = c.getresponse()
        b2 = r2.read()
        self.assertEqual(r2.status, 200)
        self.assertEqual(b1, b2)
        c.close()

    def test_error_is_framed_and_keeps_connection_alive(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/does-not-exist")
        r1 = c.getresponse()
        r1.read()
        self.assertEqual(r1.status, 404)
        self.assertEqual(r1.getheader("Content-Length"), "0")
        # Connection must still be usable after an error response.
        c.request("GET", "/playlist.m3u")
        r2 = c.getresponse()
        r2.read()
        self.assertEqual(r2.status, 200)
        c.close()

    def test_concurrency_cap_returns_503_and_closes(self):
        # Exhaust an injected semaphore so the next request is shed.
        sem = threading.BoundedSemaphore(1)
        sem.acquire()
        orig = proxy._sem
        proxy._sem = sem
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", "/playlist.m3u")
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 503)
            self.assertEqual(r.getheader("Content-Length"), "0")
            self.assertEqual((r.getheader("Connection") or "").lower(), "close")
            c.close()
        finally:
            proxy._sem = orig
            sem.release()

    def test_truncated_upstream_stream_closes_connection(self):
        # Upstream advertises a large Content-Length but drops mid-stream. The server
        # must close the connection (not keep it alive promising bytes it can't send).
        class _FakeResp:
            def __init__(self):
                self.status = 200
                self.headers = {"Content-Type": "video/mp2t", "Content-Length": "100000"}
                self._body = b"\x47" + b"\x00" * 1023  # not an m3u8
                self._pos = 0
            def read(self, n=None):
                end = len(self._body) if n is None else self._pos + n
                c = self._body[self._pos:end]
                self._pos += len(c)
                return c
            def stream(self, _sz):
                yield self._body[self._pos:]            # one short chunk...
                raise OSError("simulated upstream drop")  # ...then upstream fails
            def release_conn(self): pass
            def drain_conn(self): pass
            def geturl(self): return None

        class _FakePool:
            def request(self, *a, **k):
                return _FakeResp()

        orig = proxy._pool
        proxy._pool = _FakePool()
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
            s.sendall(b"GET /fetch?url=http%3A%2F%2Fx%2Fseg.ts HTTP/1.1\r\nHost: x\r\n\r\n")
            s.settimeout(3)
            got_eof = False
            data = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    break  # server kept the connection open -> desync (fix absent)
                if chunk == b"":
                    got_eof = True
                    break
                data += chunk
            s.close()
            self.assertTrue(data.startswith(b"HTTP/1.1 200"))
            self.assertTrue(got_eof, "server must close the connection after a truncated stream")
        finally:
            proxy._pool = orig

    def test_health_ok_returns_json(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/health")
        r = c.getresponse()
        body = json.loads(r.read())
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "application/json")
        self.assertTrue(body["ok"])
        self.assertEqual(body["channels"], 1)
        self.assertEqual(body["version"], proxy.__version__)
        for k in ("cache_age_s", "stream_ok", "stream_err", "fetch_ok", "fetch_err"):
            self.assertIn(k, body)
        c.close()

    def test_health_503_when_no_channels(self):
        saved = proxy._channels
        proxy._channels = {}
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", "/health")
            r = c.getresponse()
            body = json.loads(r.read())
            self.assertEqual(r.status, 503)
            self.assertFalse(body["ok"])
            c.close()
        finally:
            proxy._channels = saved

    def test_logs_endpoint_404_when_disabled(self):
        self.assertFalse(proxy.LOGS_ENDPOINT)  # opt-in, off by default
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/logs")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 404)
        c.close()

    def test_logs_endpoint_returns_text_when_enabled(self):
        orig = proxy.LOGS_ENDPOINT
        proxy.LOGS_ENDPOINT = True
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", "/playlist.m3u")          # generate a log line
            c.getresponse().read()
            c.request("GET", "/logs")
            r = c.getresponse()
            body = r.read().decode()
            self.assertEqual(r.status, 200)
            self.assertTrue(r.getheader("Content-Type").startswith("text/plain"))
            self.assertIn("playlist.m3u", body)
            c.close()
        finally:
            proxy.LOGS_ENDPOINT = orig

    def test_logs_scrub_query_strings(self):
        orig = proxy.LOGS_ENDPOINT
        proxy.LOGS_ENDPOINT = True
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", "/playlist.m3u?token=SECRET123")  # query carries a "token"
            c.getresponse().read()
            c.request("GET", "/logs")
            r = c.getresponse()
            body = r.read().decode()
            c.close()
            self.assertNotIn("SECRET123", body)  # query stripped from the access log
            self.assertIn("?…", body)        # replaced with "?…"
        finally:
            proxy.LOGS_ENDPOINT = orig

    def test_upstream_error_is_counted_and_recorded(self):
        # An upstream >=400 must surface as a counted fetch_err with the host recorded,
        # not a silent 502 miscounted as success.
        class _Resp403:
            status = 403
            headers = {}
            def drain_conn(self):
                pass

        class _Pool:
            def request(self, *a, **k):
                return _Resp403()

        orig = proxy._pool
        before = proxy._stats["fetch_err"]
        proxy._pool = _Pool()
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", "/fetch?url=http%3A%2F%2Fvideo-auth2.iol.pt%2Fx%2Fchunks.m3u8")
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 502)
            c.close()
            self.assertEqual(proxy._stats["fetch_err"], before + 1)
            self.assertIn("403", proxy._last_request_err or "")
            self.assertIn("video-auth2.iol.pt", proxy._last_request_err or "")
        finally:
            proxy._pool = orig

    def test_started_flag_resets_between_keepalive_requests(self):
        # A success then a failing request on the SAME connection: the failure must
        # still send its 502. Regression guard for the per-instance _started flag
        # leaking across kept-alive requests.
        orig = proxy._fetch
        proxy._fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", "/playlist.m3u")      # success -> sets _started True
            c.getresponse().read()
            c.request("GET", "/stream/1")          # _fetch raises -> must emit 502
            r2 = c.getresponse()
            r2.read()
            self.assertEqual(r2.status, 502)
            c.close()
        finally:
            proxy._fetch = orig


class EnvIntTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("M3U_TEST_INT", None)

    def test_default_when_unset(self):
        os.environ.pop("M3U_TEST_INT", None)
        self.assertEqual(proxy._envint("M3U_TEST_INT", 7), 7)

    def test_parses_valid(self):
        os.environ["M3U_TEST_INT"] = "42"
        self.assertEqual(proxy._envint("M3U_TEST_INT", 7), 42)

    def test_exits_on_invalid(self):
        os.environ["M3U_TEST_INT"] = "notanint"
        with self.assertRaises(SystemExit):
            proxy._envint("M3U_TEST_INT", 7)


class TokenInjectTest(unittest.TestCase):
    def setUp(self):
        self._rules = proxy._token_rules
        self._gettok = proxy._get_token
        proxy._token_rules = [("video-auth*.example.com", "https://t.example/tok", "sig")]
        proxy._get_token = lambda ep: "TOK+EN/="  # base64-ish (has +, /, =)

    def tearDown(self):
        proxy._token_rules = self._rules
        proxy._get_token = self._gettok

    def test_fills_empty_param_on_matching_host(self):
        out = proxy._inject_token("https://video-auth2.example.com/x/chunks.m3u8?sig=")
        self.assertEqual(out, "https://video-auth2.example.com/x/chunks.m3u8?sig=TOK%2BEN%2F%3D")

    def test_appends_param_when_absent(self):
        out = proxy._inject_token("https://video-auth2.example.com/x/chunks.m3u8")
        self.assertEqual(out, "https://video-auth2.example.com/x/chunks.m3u8?sig=TOK%2BEN%2F%3D")

    def test_appends_with_ampersand_when_other_params_present(self):
        out = proxy._inject_token("https://video-auth2.example.com/x/chunks.m3u8?a=1")
        self.assertEqual(out, "https://video-auth2.example.com/x/chunks.m3u8?a=1&sig=TOK%2BEN%2F%3D")

    def test_untouched_when_param_already_set(self):
        u = "https://video-auth2.example.com/x/chunks.m3u8?sig=ALREADY"
        self.assertEqual(proxy._inject_token(u), u)

    def test_appends_before_fragment(self):
        out = proxy._inject_token("https://video-auth2.example.com/x/chunks.m3u8#frag")
        self.assertEqual(out, "https://video-auth2.example.com/x/chunks.m3u8?sig=TOK%2BEN%2F%3D#frag")

    def test_skips_when_a_later_duplicate_has_value(self):
        u = "https://video-auth2.example.com/x/chunks.m3u8?sig=&sig=REAL"
        self.assertEqual(proxy._inject_token(u), u)

    def test_untouched_on_nonmatching_host(self):
        u = "https://other.example.org/x/chunks.m3u8?sig="
        self.assertEqual(proxy._inject_token(u), u)

    def test_noop_when_no_rules(self):
        proxy._token_rules = []
        u = "https://video-auth2.example.com/x/chunks.m3u8?sig="
        self.assertEqual(proxy._inject_token(u), u)

    def test_parse_rules_skips_malformed(self):
        rules = proxy._parse_token_rules("a*.x|https://t|p ;; bad-rule ;; b*.y|https://u|q ;; |https://z|r")
        self.assertEqual(rules, [("a*.x", "https://t", "p"), ("b*.y", "https://u", "q")])


class TokenFetchTest(unittest.TestCase):
    def test_get_token_fetches_once_and_caches(self):
        calls = {"n": 0}

        class _Resp:
            status = 200
            data = b"  TOKENVALUE  \n"

        class _Pool:
            def request(self, *a, **k):
                calls["n"] += 1
                return _Resp()

        orig_pool, orig_cache, orig_ttl = proxy._pool, proxy._token_cache, proxy.TOKEN_TTL
        proxy._pool = _Pool()
        proxy._token_cache = {}
        proxy.TOKEN_TTL = 60
        try:
            self.assertEqual(proxy._get_token("https://t.example/tok"), "TOKENVALUE")
            self.assertEqual(proxy._get_token("https://t.example/tok"), "TOKENVALUE")
            self.assertEqual(calls["n"], 1)  # second call served from cache
        finally:
            proxy._pool, proxy._token_cache, proxy.TOKEN_TTL = orig_pool, orig_cache, orig_ttl

    def test_get_token_negatively_caches_failures(self):
        calls = {"n": 0}

        class _Pool:
            def request(self, *a, **k):
                calls["n"] += 1
                raise OSError("boom")

        orig_pool, orig_cache = proxy._pool, proxy._token_cache
        proxy._pool = _Pool()
        proxy._token_cache = {}
        try:
            self.assertIsNone(proxy._get_token("https://t.example/down"))
            self.assertIsNone(proxy._get_token("https://t.example/down"))
            self.assertEqual(calls["n"], 1)  # failure cached -> not re-probed every call
        finally:
            proxy._pool, proxy._token_cache = orig_pool, orig_cache

    def test_get_token_rejects_implausible_body(self):
        class _Resp:
            status = 200
            data = b"<html><body>Service Unavailable</body></html>"

        class _Pool:
            def request(self, *a, **k):
                return _Resp()

        orig_pool, orig_cache = proxy._pool, proxy._token_cache
        proxy._pool = _Pool()
        proxy._token_cache = {}
        try:
            self.assertIsNone(proxy._get_token("https://t.example/maintenance"))
        finally:
            proxy._pool, proxy._token_cache = orig_pool, orig_cache


if __name__ == "__main__":
    unittest.main()
