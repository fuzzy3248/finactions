"""RSS/Atom feed tests without a network (a fake fetcher): PYTHONPATH=src python3 -m unittest tests.test_feeds -v"""
import base64
import json
import os
import tempfile
import struct
import threading
import unittest
import urllib.error
import urllib.request

from finactions import feeds

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Channel &amp; Company</title>
  <item><title>Older item</title><link>https://example.com/a</link><pubDate>Sat, 14 Nov 2026 10:00:00 +0000</pubDate>
    <description>First &lt;b&gt;description&lt;/b&gt; with &amp;amp; entity</description></item>
  <item><title><![CDATA[Newer <i>item</i> &quot;test&quot;]]></title><link>https://example.com/b</link>
    <pubDate>Sun, 15 Nov 2026 12:30:00 GMT</pubDate><content:encoded><![CDATA[<p>Caf&#233;   with  <a href="x">a link</a></p>]]></content:encoded></item>
  <item><title>No link</title><pubDate>Sun, 15 Nov 2026 13:00:00 GMT</pubDate></item>
  <item><title>Bad address</title><link>javascript:alert(1)</link></item>
  <item><title>Via guid</title><guid isPermaLink="true">https://example.com/g</guid><dc:date>2026-11-15T14:00:00Z</dc:date></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atomic</title>
  <entry><title>Atom entry</title><link rel="self" href="https://example.com/self"/><link rel="alternate" href="https://example.com/alt"/>
    <updated>2026-11-15T09:00:00+01:00</updated><summary>Short description</summary></entry>
  <entry><title>No date</title><link href="https://example.com/nodate"/></entry>
</feed>"""


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ParseTest(unittest.TestCase):
    def test_rss_items_are_cleaned_sorted_and_filtered(self):
        parsed = feeds.parse_feed(RSS)
        self.assertEqual(parsed['title'], 'Channel & Company')
        titles = [i['title'] for i in parsed['items']]
        # newest first; without the item lacking a link and the one with a non-http/https link
        self.assertEqual(titles, ['Via guid', 'Newer item "test"', 'Older item'])
        newer = parsed['items'][1]
        self.assertEqual(newer['link'], 'https://example.com/b')
        self.assertEqual(newer['ts'], 1794745800)   # 2026-11-15 12:30 UTC
        self.assertEqual(newer['summary'], 'Café with a link')   # content:encoded without HTML, entities expanded, spaces collapsed
        self.assertEqual(parsed['items'][2]['summary'], 'First description with & entity')
        self.assertEqual(parsed['items'][0]['ts'], 1794751200)   # dc:date in ISO with "Z"

    def test_atom_uses_alternate_link_and_handles_missing_date(self):
        items = feeds.parse_feed(ATOM)['items']
        self.assertEqual([i['link'] for i in items], ['https://example.com/alt', 'https://example.com/nodate'])
        self.assertEqual(items[0]['ts'], 1794729600)   # 09:00 +01:00 = 08:00 UTC
        self.assertIsNone(items[1]['ts'])

    def test_html_in_titles_never_survives(self):
        data = b'<rss><channel><item><title>&lt;script&gt;x()&lt;/script&gt;Title</title><link>https://e.com/1</link></item></channel></rss>'
        # after the entities are expanded only plain text remains; the dashboard inserts it as text, not as HTML
        self.assertNotIn('<script>', feeds.parse_feed(data)['items'][0]['title'])

    def test_limit_and_long_text_are_trimmed(self):
        many = ''.join(f'<item><title>T{i}</title><link>https://e.com/{i}</link><pubDate>Sun, 15 Nov 2026 12:{i:02d}:00 GMT</pubDate>'
                       f'<description>{"x" * 1000}</description></item>' for i in range(50))
        items = feeds.parse_feed(f'<rss><channel>{many}</channel></rss>'.encode(), limit=7)['items']
        self.assertEqual(len(items), 7)
        self.assertEqual(items[0]['title'], 'T49')
        self.assertEqual(len(items[0]['summary']), feeds.SUMMARY_CHARS)
        self.assertTrue(items[0]['summary'].endswith('…'))

    def test_not_a_feed_is_rejected(self):
        for data in [b'<html><body>domain for sale</body></html>', b'not xml', b'<rss></rss>', b'']:
            with self.assertRaises(feeds.FeedError, msg=data):
                feeds.parse_feed(data)

    def test_entity_definitions_are_rejected(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss><channel><title>&b;</title></channel></rss>'
        with self.assertRaises(feeds.FeedError):
            feeds.parse_feed(bomb)

    def test_leading_whitespace_before_xml_declaration_is_tolerated(self):
        self.assertEqual(len(feeds.parse_feed(b'\n\n  ' + RSS)['items']), 3)

    def test_parse_date(self):
        self.assertEqual(feeds.parse_date('Sun, 15 Nov 2026 12:30:00 GMT'), 1794745800)
        self.assertEqual(feeds.parse_date('2026-11-15T12:30:00Z'), 1794745800)
        self.assertEqual(feeds.parse_date('2026-11-15T12:30:00'), 1794745800)   # no zone = UTC
        for bad in ['', None, 'wczoraj', '2026-13-45']:
            self.assertIsNone(feeds.parse_date(bad))


class ItemExtrasTest(unittest.TestCase):
    """Item image, author and categories."""

    def item(self, body, ns=''):
        xml = (f'<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:dc="http://purl.org/dc/elements/1.1/" {ns}>'
               f'<channel><item><title>T</title><link>https://e.com/post/1</link>{body}</item></channel></rss>').encode()
        return feeds.parse_feed(xml)['items'][0]

    def test_image_priority_media_content_over_thumbnail_enclosure_and_html(self):
        body = ('<description>&lt;img src="https://e.com/html.jpg"&gt;</description><enclosure url="https://e.com/enc.jpg" type="image/jpeg"/>'
                '<media:thumbnail url="https://e.com/thumb.jpg"/><media:content url="https://e.com/big.jpg" medium="image"/>')
        self.assertEqual(self.item(body)['image'], 'https://e.com/big.jpg')
        self.assertEqual(self.item(body.replace('<media:content url="https://e.com/big.jpg" medium="image"/>', ''))['image'], 'https://e.com/thumb.jpg')
        self.assertEqual(self.item('<enclosure url="https://e.com/enc.jpg" type="image/jpeg"/><description>x</description>')['image'], 'https://e.com/enc.jpg')

    def test_media_group_and_non_image_media_are_handled(self):
        self.assertEqual(self.item('<media:group><media:content url="https://e.com/g.png" type="image/png"/></media:group>')['image'], 'https://e.com/g.png')
        self.assertEqual(self.item('<enclosure url="https://e.com/a.mp3" type="audio/mpeg"/>')['image'], '')   # not an image

    def test_image_from_description_skips_tracking_pixels_and_joins_relative_urls(self):
        html_body = ('<description><![CDATA[<img src="https://t.example/pixel.gif" width="1" height="1">'
                     '<p><img alt="" src="/img/cover.jpg" width="640"></p>]]></description>')
        self.assertEqual(self.item(html_body)['image'], 'https://e.com/img/cover.jpg')   # a relative address is resolved against the item link

    def test_only_http_images_are_accepted(self):
        self.assertEqual(self.item('<media:content url="javascript:alert(1)" medium="image"/>')['image'], '')
        self.assertEqual(self.item('<description>&lt;img src="data:image/png;base64,AAAA"&gt;</description>')['image'], '')

    def test_no_image_is_empty_string(self):
        self.assertEqual(self.item('<description>plain text</description>')['image'], '')

    def test_author_variants(self):
        self.assertEqual(self.item('<dc:creator>Jan Kowalski</dc:creator>')['author'], 'Jan Kowalski')
        self.assertEqual(self.item('<author>editors@e.com (Anna Nowak)</author>')['author'], 'Anna Nowak')
        self.assertEqual(self.item('<author>editors@e.com</author>')['author'], '')   # an e-mail alone is not an author
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title><link href="https://e.com/1"/><author><name>Eve</name></author></entry></feed>'
        self.assertEqual(feeds.parse_feed(atom)['items'][0]['author'], 'Eve')

    def test_tags_are_unique_trimmed_and_limited(self):
        body = ''.join(f'<category>Tag{i % 3}</category><category><![CDATA[ Other {i} ]]></category>' for i in range(5))
        tags = self.item(body)['tags']
        self.assertEqual(len(tags), feeds.MAX_TAGS)
        self.assertEqual(len(set(tags)), len(tags))
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title><link href="https://e.com/1"/><category term="crypto"/></entry></feed>'
        self.assertEqual(feeds.parse_feed(atom)['items'][0]['tags'], ['crypto'])


class CatalogTest(unittest.TestCase):
    def test_catalog_file_is_valid_and_ids_are_unique(self):
        catalog = feeds.load_catalog(os.path.join(ROOT, 'config', 'feeds.json'))
        self.assertEqual(len({f['id'] for f in catalog}), len(catalog))
        self.assertTrue(all(f['url'].startswith('https://') for f in catalog))
        self.assertIn('bithub', {f['id'] for f in catalog})   # the Polish feeds are on the list

    def test_id_is_made_from_name_when_missing(self):
        self.assertEqual(feeds.normalize_catalog([{'name': 'Café Crypto 24', 'url': 'https://e.com/feed'}])[0]['id'], 'caf-crypto-24')

    def test_bad_catalogs_are_rejected(self):
        bad = [[], 'x', [1], [{'name': 'A'}], [{'name': 'A', 'url': 'ftp://e.com/x'}], [{'name': 'A', 'url': 'file:///etc/passwd'}],
               [{'name': 'A', 'url': 'https://e.com', 'id': 'Zle ID'}],
               [{'name': 'A', 'url': 'https://e.com'}, {'name': 'a', 'url': 'https://f.com'}],   # to samo id z nazwy
               [{'name': '  ', 'url': 'https://e.com'}]]
        for raw in bad:
            with self.assertRaises(ValueError, msg=raw):
                feeds.normalize_catalog(raw)


def png(width, height):
    """The smallest PNG header with the given dimensions (for recognition and aspect-ratio checks)."""
    return b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', width, height) + b'\x08\x06\x00\x00\x00'


def ico(size=16):
    return b'\x00\x00\x01\x00\x01\x00' + bytes([size, size]) + b'\x00' * 20


class IconTest(unittest.TestCase):
    def test_sniff_image_recognises_formats_by_bytes(self):
        self.assertEqual(feeds.sniff_image(png(16, 16)), 'image/png')
        self.assertEqual(feeds.sniff_image(ico()), 'image/x-icon')
        self.assertEqual(feeds.sniff_image(b'GIF89a' + b'\x00' * 10), 'image/gif')
        self.assertEqual(feeds.sniff_image(b'\xff\xd8\xff\xe0' + b'\x00' * 10), 'image/jpeg')
        self.assertEqual(feeds.sniff_image(b'RIFF\x00\x00\x00\x00WEBPVP8 '), 'image/webp')
        self.assertEqual(feeds.sniff_image(b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'), 'image/svg+xml')

    def test_sniff_image_rejects_html_empty_and_active_svg(self):
        for data in [b'', b'<!doctype html><html><body>404</body></html>', b'plain text', b'<svg><script>alert(1)</script></svg>']:
            self.assertIsNone(feeds.sniff_image(data), data)

    def test_image_size_from_headers(self):
        self.assertEqual(feeds.image_size(png(32, 24), 'image/png'), (32, 24))
        self.assertEqual(feeds.image_size(ico(48), 'image/x-icon'), (48, 48))
        self.assertEqual(feeds.image_size(b'GIF89a' + struct.pack('<HH', 10, 12), 'image/gif'), (10, 12))
        self.assertIsNone(feeds.image_size(b'<svg/>', 'image/svg+xml'))

    def test_page_icon_urls_order_and_resolution(self):
        page = """<html><head>
          <link rel="apple-touch-icon" sizes="180x180" href="/touch.png">
          <link rel="mask-icon" href="/mask.svg">
          <link rel="icon" type="image/png" sizes="192x192" href="/big.png">
          <link rel="shortcut icon" href="https://cdn.example.com/s.ico">
          <link rel="icon" sizes="32x32" href="small.png">
          <link rel="stylesheet" href="/x.css"></head></html>"""
        self.assertEqual(feeds.page_icon_urls(page, 'https://example.com/dir/'), [
            'https://example.com/dir/small.png',      # closest to 32 px
            'https://cdn.example.com/s.ico',          # rozmiar nieznany
            'https://example.com/big.png',            # 192 px
            'https://example.com/favicon.ico',        # the default address after the declared ones
            'https://example.com/touch.png'])         # apple-touch-icon last; mask-icon skipped

    def find(self, files, feed_icons=(), site='https://example.com/feed', check_host=lambda host: True):
        calls = []

        def fetch(url, max_bytes, truncate=False):
            calls.append(url)
            if url not in files:
                raise feeds.FeedError('HTTP 404')
            return files[url]

        icon = feeds.find_icon(list(feed_icons), site, fetch, check_host)
        return icon, calls

    def test_icon_declared_in_feed_wins(self):
        icon, calls = self.find({'https://example.com/i.png': png(32, 32)}, ['https://example.com/i.png'])
        self.assertEqual(icon, 'data:image/png;base64,' + base64.b64encode(png(32, 32)).decode())
        self.assertEqual(calls, ['https://example.com/i.png'])   # the home page is not needed

    def test_wide_feed_logo_is_skipped_for_the_site_icon(self):
        page = b'<link rel="icon" href="/fav.png">'
        icon, calls = self.find({'https://example.com/logo.png': png(500, 94), 'https://example.com/': page,
                                 'https://example.com/fav.png': png(32, 32)}, ['https://example.com/logo.png'])
        self.assertTrue(icon.startswith('data:image/png'))
        self.assertEqual(calls, ['https://example.com/logo.png', 'https://example.com/', 'https://example.com/fav.png'])

    def test_non_images_are_skipped_even_when_status_is_ok(self):
        icon, _ = self.find({'https://example.com/': b'<link rel="icon" href="/a.ico">', 'https://example.com/a.ico': b'<html>404</html>',
                             'https://example.com/favicon.ico': ico()})
        self.assertTrue(icon.startswith('data:image/x-icon'))

    def test_unreadable_homepage_falls_back_to_default_favicon(self):
        icon, calls = self.find({'https://example.com/favicon.ico': ico()})
        self.assertTrue(icon)
        self.assertEqual(calls, ['https://example.com/', 'https://example.com/favicon.ico'])

    def test_nothing_found_is_none(self):
        self.assertIsNone(self.find({})[0])

    def test_private_hosts_are_never_fetched(self):
        icon, calls = self.find({'http://127.0.0.1/i.png': png(16, 16)}, ['http://127.0.0.1/i.png'],
                                check_host=lambda host: host not in ('127.0.0.1',))
        self.assertNotIn('http://127.0.0.1/i.png', calls)
        self.assertFalse(feeds.is_public_host('127.0.0.1'))
        self.assertFalse(feeds.is_public_host('localhost'))
        self.assertFalse(feeds.is_public_host('does-not-exist.invalid'))

    def test_feed_declares_icons(self):
        rss = b'<rss><channel><title>t</title><image><url>https://e.com/i.png</url></image></channel></rss>'
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title><icon>https://e.com/a.ico</icon><logo>ftp://x/l.png</logo></feed>'
        self.assertEqual(feeds.parse_feed(rss)['icons'], ['https://e.com/i.png'])
        self.assertEqual(feeds.parse_feed(atom)['icons'], ['https://e.com/a.ico'])   # http/https only

class FetchTest(unittest.TestCase):
    """fetch_url against a local server: the size limit and truncating of pages (no external network)."""

    @classmethod
    def setUpClass(cls):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'<head>' + b'x' * 5000 + b'</head>' if self.path == '/big' else b'ok'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_too_big_is_an_error_but_truncate_returns_the_head(self):
        self.assertEqual(feeds.fetch_url(self.base + '/small', max_bytes=100), b'ok')
        with self.assertRaises(feeds.FeedError):
            feeds.fetch_url(self.base + '/big', max_bytes=100)
        head = feeds.fetch_url(self.base + '/big', max_bytes=100, truncate=True)
        self.assertEqual(len(head), 100)
        self.assertTrue(head.startswith(b'<head>'))


class BuildSiteTest(unittest.TestCase):
    """build_site with a fake fetcher: the output files, the "stale" result, skipping unchanged feeds, removing feeds, icons."""

    CATALOG = [{'name': 'One', 'url': 'https://e.com/1'}, {'name': 'Two', 'url': 'https://e.com/2'}]
    FEED = b'<rss><channel><item><title>x</title><link>https://e.com/x</link><pubDate>Sun, 15 Nov 2026 12:30:00 GMT</pubDate></item></channel></rss>'
    NO_ICONS = dict(fetch_binary=lambda url, n, truncate=False: (_ for _ in ()).throw(feeds.FeedError('x')), check_host=lambda host: True)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = self._tmp.name
        self.clock = [1_800_000_000]

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, fetch, catalog=None):
        return feeds.build_site(catalog or self.CATALOG, self.out, fetch=fetch, now=lambda: self.clock[0], workers=1, **self.NO_ICONS)

    def read(self, *parts):
        with open(os.path.join(self.out, *parts), encoding='utf-8') as f:
            return json.load(f)

    def test_writes_listing_feed_files_and_meta(self):
        results = self.build(lambda url: self.FEED)
        self.assertEqual({k: v['ok'] for k, v in results.items()}, {'one': True, 'two': True})
        listing = self.read('feeds.json')
        self.assertEqual([e['id'] for e in listing], ['one', 'two'])
        self.assertEqual(set(listing[0]), {'id', 'name', 'icon', 'icon_checked'})
        feed = self.read('feed', 'one.json')
        self.assertEqual((feed['id'], feed['name'], feed['fetched']), ('one', 'One', 1_800_000_000))
        self.assertEqual(feed['items'][0]['link'], 'https://e.com/x')
        self.assertEqual(self.read('meta.json')['feeds']['two'], {'ok': True, 'stale': False, 'error': None})
        self.assertNotIn('e.com', json.dumps(listing))   # the catalog for the browser does not reveal the feed addresses

    def test_failure_keeps_stale_result_then_writes_error(self):
        self.build(lambda url: self.FEED)

        def broken(url):
            raise feeds.FeedError('HTTP 503')

        self.clock[0] += 3600
        results = self.build(broken)
        self.assertEqual(results['one'], {'ok': False, 'stale': True, 'error': 'HTTP 503'})
        stale = self.read('feed', 'one.json')
        self.assertTrue(stale['stale'])
        self.assertEqual(len(stale['items']), 1)
        self.clock[0] += 7 * 3600   # older than 6 h: only the error remains
        results = self.build(broken)
        self.assertFalse(results['one']['stale'])
        failed = self.read('feed', 'one.json')
        self.assertEqual((failed['items'], failed['error']), ([], 'HTTP 503'))

    def test_unchanged_feed_is_not_rewritten_for_a_while(self):
        self.build(lambda url: self.FEED)
        path = os.path.join(self.out, 'feed', 'one.json')
        before = os.stat(path).st_mtime_ns
        self.clock[0] += 900
        self.build(lambda url: self.FEED)
        self.assertEqual(os.stat(path).st_mtime_ns, before)
        self.assertEqual(self.read('feed', 'one.json')['fetched'], 1_800_000_000)   # the old fetch time
        self.clock[0] += 4 * 3600   # after KEEP_UNCHANGED the result is refreshed even without new items
        self.build(lambda url: self.FEED)
        self.assertEqual(self.read('feed', 'one.json')['fetched'], self.clock[0])

    def test_removed_feed_disappears(self):
        self.build(lambda url: self.FEED)
        self.build(lambda url: self.FEED, catalog=self.CATALOG[:1])
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, 'feed'))), ['one.json'])
        self.assertEqual([e['id'] for e in self.read('feeds.json')], ['one'])

    def test_icon_is_found_and_reused_until_the_check_is_due(self):
        calls = []
        files = {'https://e.com/i.png': png(32, 32)}

        def binary(url, n, truncate=False):
            calls.append(url)
            if url in files:
                return files[url]
            raise feeds.FeedError('x')

        rss = b'<rss><channel><item><title>x</title><link>https://e.com/1</link></item><image><url>https://e.com/i.png</url></image></channel></rss>'
        kwargs = dict(fetch=lambda url: rss, fetch_binary=binary, check_host=lambda host: True, now=lambda: self.clock[0], workers=1)
        feeds.build_site(self.CATALOG[:1], self.out, **kwargs)
        icon = self.read('feeds.json')[0]['icon']
        self.assertTrue(icon.startswith('data:image/png;base64,'))
        calls.clear()
        self.clock[0] += 3600
        feeds.build_site(self.CATALOG[:1], self.out, **kwargs)
        self.assertEqual(calls, [])   # the icon was checked an hour ago: no new requests
        self.assertEqual(self.read('feeds.json')[0]['icon'], icon)
        self.clock[0] += 24 * 3600
        feeds.build_site(self.CATALOG[:1], self.out, **kwargs)
        self.assertTrue(calls)   # a day has passed: the icon is looked up again


if __name__ == '__main__':
    unittest.main()
