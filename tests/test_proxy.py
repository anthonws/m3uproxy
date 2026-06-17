"""Unit tests for the pure parsing / rewriting logic in proxy.py.

Importing the module is side-effect free (the server only starts under
``if __name__ == "__main__"``), so these run with no network access.
"""

import os
import sys
import unittest
import urllib.parse

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


if __name__ == "__main__":
    unittest.main()
