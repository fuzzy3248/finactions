"""RSS/Atom feeds for the "RSS feeds" panel of the dashboard, as static JSON files.

A browser cannot fetch most feeds itself (the sites do not send CORS headers), so this program does it (from GitHub Actions) and
publishes the result as plain files that the dashboard reads from GitHub Pages (which sends an open CORS header):

  feeds.json            catalog: [{"id", "name", "icon"}], icon is a data URI (16x16 in the dashboard) or null; the same shape as
                        the former /api/feeds endpoint of the yfinance service
  feed/<id>.json        latest items of a feed: {"id", "name", "fetched", "items": [{title, link, ts, summary, image, author, tags}]}
                        (like the former /api/feed?id=); when a feed is temporarily down and we have an older result (not older
                        than 6 h), it is kept with an extra "stale": true; when we have nothing, the file holds
                        {"id", "name", "error": "…", "fetched"}
  meta.json             {"generated", "feeds": {id: {"ok", "stale", "error"}}}: a status overview for monitoring

The feed catalog is in config/feeds.json (a list of {name, url, id?} objects); only those addresses are fetched. From a feed we take
only plain text (title, short description without HTML), an http/https link and the date; everything else is discarded so that content
from a third-party site never reaches the dashboard as code.
"""
import base64
import gzip
import html
import io
import json
import ipaddress
import logging
import os
import re
import socket
import struct
import threading
import time
import zlib
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

MAX_BYTES = 3 * 1024 * 1024   # a larger response is an error (feeds are usually 20–150 KB)
MAX_ITEMS = 30                # this many of the newest items of a feed
TITLE_CHARS = 200
SUMMARY_CHARS = 500           # item description (the dashboard's Full view shows it in a few lines)
MAX_TAGS = 4                  # this many item categories
FETCH_TIMEOUT = 10            # seconds
STALE_MAX = 6 * 3600          # on a fetch error we keep the older result if it is not older than 6 h
USER_AGENT = 'Mozilla/5.0 (compatible; finactions/1.0; RSS reader)'
ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}$')
TAG_RE = re.compile(r'<[^>]*>')


log = logging.getLogger('finactions')

ICON_MAX_BYTES = 32 * 1024         # a larger icon is skipped (it is 16x16 px in the dashboard and travels in JSON as a data URI)
PAGE_MAX_BYTES = 256 * 1024        # we look for <link rel="icon"> in this many leading bytes of the home page (it is in <head>)
ICON_TRIES = 8                     # this many icon addresses are tried per feed
ICON_PREFERRED_SIZE = 32           # of several declared sizes we take the closest to this one


class FeedError(Exception):
    """A feed could not be fetched or read."""


def slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40]


def normalize_catalog(raw):
    """Validates a feed catalog ([{name, url, id?}]) and returns a list of {id, name, url}. ValueError with a description when it is bad."""
    if not isinstance(raw, list) or not raw:
        raise ValueError('expected a non-empty list of feeds')
    catalog, seen = [], set()
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name'].strip() \
                or not isinstance(item.get('url'), str):
            raise ValueError(f'feed #{i}: expected an object with string fields name and url')
        name, url = item['name'].strip(), item['url'].strip()
        if urlparse(url).scheme not in ('http', 'https') or not urlparse(url).netloc:
            raise ValueError(f'feed {name}: the address must start with http:// or https://')
        feed_id = item.get('id') or slugify(name)
        if not isinstance(feed_id, str) or not ID_RE.match(feed_id):
            raise ValueError(f'feed {name}: the id must consist of lowercase letters, digits and hyphens (e.g. my-feed)')
        if feed_id in seen:
            raise ValueError(f'id {feed_id} is repeated (give another one in the id field)')
        seen.add(feed_id)
        catalog.append({'id': feed_id, 'name': name, 'url': url})
    return catalog


# ---------------------------------------------------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------------------------------------------------
class _HttpOnly(urllib.request.HTTPRedirectHandler):
    """Redirects only to http/https (urllib would by default also follow to ftp)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme not in ('http', 'https'):
            raise urllib.error.HTTPError(newurl, code, 'redirect to a disallowed address', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url, timeout=FETCH_TIMEOUT, max_bytes=MAX_BYTES, truncate=False):
    """Bytes of a GET response (after gunzipping); FeedError on an HTTP or network error or an oversized response. With truncate=True
    an oversized response is not an error: its beginning is returned (home pages, of which only <head> is needed)."""
    request = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT, 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml, */*',
        'Accept-Encoding': 'gzip'})
    try:
        with urllib.request.build_opener(_HttpOnly).open(request, timeout=timeout) as response:
            data = response.read(max_bytes if truncate else max_bytes + 1)
            gzipped = response.headers.get('Content-Encoding', '').lower() == 'gzip'
    except urllib.error.HTTPError as e:
        raise FeedError(f'HTTP {e.code}')
    except (urllib.error.URLError, OSError) as e:
        raise FeedError(f'no connection ({getattr(e, "reason", e)})')
    if truncate:
        if gzipped:
            try:
                return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, max_bytes)   # tolerates a truncated stream
            except zlib.error:
                raise FeedError('corrupt response (gzip)')
        return data[:max_bytes]
    if len(data) > max_bytes:
        raise FeedError('the response is too large')
    if gzipped:
        try:
            data = gzip.GzipFile(fileobj=io.BytesIO(data)).read(max_bytes + 1)
        except (OSError, EOFError):
            raise FeedError('corrupt response (gzip)')
        if len(data) > max_bytes:
            raise FeedError('the response is too large after decompression')
    return data


def fetch_bytes(url, max_bytes, truncate=False):
    """fetch_url with just a byte limit (the signature used when fetching icons and home pages; pages are truncated)."""
    return fetch_url(url, max_bytes=max_bytes, truncate=truncate)


# ---------------------------------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------------------------------
def _local(tag):
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ''


def _text(elem, *names):
    """Text of the first child with one of the names (the order of names is the priority), ignoring XML namespaces."""
    for name in names:
        for child in elem:
            if _local(child.tag) == name and (child.text or '').strip():
                return child.text
    return ''


def clean_text(value, limit=None):
    """Plain text from an HTML fragment: no tags, entities expanded, single spaces."""
    if not value:
        return ''
    text = ' '.join(html.unescape(TAG_RE.sub(' ', value)).split())
    if limit and len(text) > limit:
        text = text[:limit - 1].rstrip() + '…'
    return text


IMG_TAG_RE = re.compile(r'<img\b[^>]*>', re.I)
SRC_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.I)
SIZE_RE = re.compile(r"""\b(width|height)\s*=\s*["']?(\d+)""", re.I)
IMAGE_EXT_RE = re.compile(r'\.(jpe?g|png|gif|webp|avif)(\?|$)', re.I)


def _image_from_html(fragment):
    """Address of the first sensible <img> in an HTML fragment (an item description); skips tracking pixels and thumbnails below 20 px."""
    for tag in IMG_TAG_RE.findall(fragment or ''):
        match = SRC_RE.search(tag)
        if not match:
            continue
        if min((int(v) for _, v in SIZE_RE.findall(tag)), default=100) < 20:
            continue
        return html.unescape(match.group(1)).strip()
    return ''


def _entry_image(entry, link):
    """Item image: media:content (image) > media:thumbnail > enclosure/link rel=enclosure of type image/* > the first <img> in the description.
    The address is absolute (a relative one is resolved against the item link) and http/https only; no image is an empty string."""
    found = {}   # priority -> address

    def consider(elem):
        name, url = _local(elem.tag), (elem.get('url') or elem.get('href') or '').strip()
        kind = (elem.get('type') or '').lower()
        if not url:
            return
        if name == 'content' and (elem.get('medium') == 'image' or kind.startswith('image/') or IMAGE_EXT_RE.search(url)):
            found.setdefault(0, url)
        elif name == 'thumbnail':
            found.setdefault(1, url)
        elif name in ('enclosure', 'link') and (elem.get('rel') in (None, 'enclosure')) and kind.startswith('image/'):
            found.setdefault(2, url)

    for child in entry:
        if _local(child.tag) == 'group':   # <media:group> collects variants of the same image
            for inner in child:
                consider(inner)
        else:
            consider(child)
    url = found[min(found)] if found else _image_from_html(_text(entry, 'encoded', 'content', 'description', 'summary'))
    url = urljoin(link, url) if url else ''
    return url if urlparse(url).scheme in ('http', 'https') else ''


def _entry_author(entry):
    """Item author: dc:creator, Atom <author><name>, and in RSS <author> only when it has a name (an e-mail is ignored)."""
    for child in entry:
        if _local(child.tag) == 'creator' and (child.text or '').strip():
            return clean_text(child.text, 60)
        if _local(child.tag) == 'author':
            name = _text(child, 'name') or (child.text or '')
            named = re.search(r'\(([^)]+)\)', name)   # RSS: "address@mail (First Last)"
            name = named.group(1) if named else ('' if '@' in name else name)
            if name.strip():
                return clean_text(name, 60)
    return ''


def _entry_tags(entry):
    tags = []
    for child in entry:
        if _local(child.tag) == 'category':
            tag = clean_text(child.get('term') or child.text, 30)
            if tag and tag not in tags:
                tags.append(tag)
    return tags[:MAX_TAGS]


def parse_date(value):
    """UNIX seconds from an RFC 822 (RSS) or ISO 8601 (Atom) date; None when it cannot be parsed."""
    value = (value or '').strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _link(item):
    """Item address: RSS <link>, Atom <link href> (rel alternate), as a last resort a <guid> that is an address."""
    for child in item:
        if _local(child.tag) != 'link':
            continue
        if child.get('href') and child.get('rel', 'alternate') == 'alternate':
            return child.get('href').strip()
        if (child.text or '').strip():
            return child.text.strip()
    guid = _text(item, 'guid', 'id').strip()
    return guid if guid.startswith(('http://', 'https://')) else ''


def parse_feed(data, limit=MAX_ITEMS):
    """{title, items: [{title, link, ts, summary, image, author, tags}], icons: [addresses]} from an RSS 2.0 or Atom feed; items newest first. FeedError when it is not a feed."""
    data = data.lstrip()
    if b'<!ENTITY' in data:   # entities defined in the document are an "XML bomb" risk; feeds do not need them
        raise FeedError('the feed contains disallowed entity definitions')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FeedError(f'not a valid XML feed ({e})')
    kind = _local(root.tag)
    icons = []   # icon addresses declared in the feed header: Atom <icon>/<logo>, RSS <image><url>
    if kind == 'rss':
        channel = next((c for c in root if _local(c.tag) == 'channel'), None)
        if channel is None:
            raise FeedError('an RSS feed without a channel element')
        entries = [c for c in channel if _local(c.tag) == 'item']
        title = _text(channel, 'title')
        icons = [_text(img, 'url').strip() for img in channel if _local(img.tag) == 'image']
    elif kind == 'feed':
        entries = [c for c in root if _local(c.tag) == 'entry']
        title = _text(root, 'title')
        icons = [_text(root, 'icon').strip(), _text(root, 'logo').strip()]
    else:
        raise FeedError('this is neither an RSS nor an Atom feed')
    items = []
    for entry in entries:
        link = _link(entry)
        item_title = clean_text(_text(entry, 'title'), TITLE_CHARS)
        if not item_title or urlparse(link).scheme not in ('http', 'https'):
            continue   # an item without a title or without an openable address is not useful
        items.append({
            'title': item_title,
            'link': link,
            'ts': parse_date(_text(entry, 'pubDate', 'published', 'updated', 'date')),
            'summary': clean_text(_text(entry, 'description', 'summary', 'encoded', 'content'), SUMMARY_CHARS),
            'image': _entry_image(entry, link),
            'author': _entry_author(entry),
            'tags': _entry_tags(entry),
        })
    items.sort(key=lambda it: it['ts'] or 0, reverse=True)
    return {'title': clean_text(title, TITLE_CHARS), 'items': items[:limit],
            'icons': [u for u in icons if urlparse(u).scheme in ('http', 'https')]}


# ---------------------------------------------------------------------------------------------------------------------
# Feed icons (16x16 in the dashboard)
# ---------------------------------------------------------------------------------------------------------------------
def sniff_image(data):
    """MIME type of an image recognised by its leading bytes (we do not trust headers: sites serve a 404 as HTML with status 200,
    and favicon.ico is sometimes sent as text/html) or None when it is not a supported image."""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:4] == b'\x00\x00\x01\x00':
        return 'image/x-icon'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    head = data[:2048].lstrip(b'\xef\xbb\xbf \t\r\n').lower()
    if b'<svg' in head and head.startswith((b'<svg', b'<?xml', b'<!--', b'<!doctype svg')) and b'<script' not in data.lower():
        return 'image/svg+xml'
    return None


def image_size(data, mime):
    """(width, height) of an image when it can be read from the header (PNG, GIF, ICO, JPEG); otherwise None."""
    try:
        if mime == 'image/png' and len(data) >= 24:
            return struct.unpack('>II', data[16:24])
        if mime == 'image/gif':
            return struct.unpack('<HH', data[6:10])
        if mime == 'image/x-icon':
            return (data[6] or 256, data[7] or 256)   # the first directory entry; 0 means 256
        if mime == 'image/jpeg':
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack('>HH', data[i + 5:i + 9])
                    return width, height
                i += 2 + struct.unpack('>H', data[i + 2:i + 4])[0]
    except (struct.error, IndexError):
        pass
    return None


def is_public_host(host):
    """Whether a host name points only to public addresses: an icon address comes from a third-party site and must not steer the
    program into a local network (127.0.0.1, 192.168.x.x, cloud metadata 169.254.x.x…)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    try:
        return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)
    except ValueError:
        return False


class _IconLinks(HTMLParser):
    """Collects <link rel="icon" | "shortcut icon" | "apple-touch-icon"> from a page: [(kind, width|None, href)]."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != 'link':
            return
        a = {k: (v or '') for k, v in attrs}
        rel, href = a.get('rel', '').lower().split(), a.get('href', '').strip()
        if not href or 'mask-icon' in rel:
            return
        if 'apple-touch-icon' in rel or 'apple-touch-icon-precomposed' in rel:
            kind = 2   # large (usually 180x180), so last
        elif 'icon' in rel:
            kind = 0
        else:
            return
        match = re.match(r'(\d+)x\d+', a.get('sizes', '').strip().lower())
        self.links.append((kind, int(match.group(1)) if match else None, href))


def page_icon_urls(page_html, base_url):
    """Icon addresses from a home page in the order to try: declared <link rel="icon"> (closest to 32 px first), then
    /favicon.ico, finally apple-touch-icon."""
    parser = _IconLinks()
    try:
        parser.feed(page_html)
    except Exception:   # HTMLParser can be sensitive to broken HTML; what it managed to collect is enough
        pass
    # an unknown size (None) or "any" (e.g. SVG) is placed between the matching and the distant ones
    def key(link):
        kind, size, _ = link
        return kind, abs(size - ICON_PREFERRED_SIZE) if size else ICON_PREFERRED_SIZE
    ordered = sorted(parser.links, key=key)
    declared = [urljoin(base_url, h) for k, _, h in ordered if k == 0]
    touch = [urljoin(base_url, h) for k, _, h in ordered if k == 2]
    root = urlparse(base_url)
    default = f'{root.scheme}://{root.netloc}/favicon.ico'
    return list(dict.fromkeys(declared + [default] + touch))


def find_icon(feed_icon_urls, site_url, fetch, check_host=is_public_host):
    """Data URI of a feed's icon (or None). Order: the icon declared in the feed itself, and when it is missing or unsuitable, the
    icon of the home page serving the feed (<link rel="icon">, /favicon.ico, apple-touch-icon). A candidate must be an image
    (recognised by its bytes), fit in ICON_MAX_BYTES and, when its dimensions can be read, be roughly square, because a wide logo
    with text is unreadable once shrunk to 16x16. `fetch(url, max_bytes[, truncate])` returns bytes."""
    def usable(url):
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not check_host(parsed.hostname or ''):
            return None
        try:
            data = fetch(url, ICON_MAX_BYTES)
        except FeedError:
            return None
        mime = sniff_image(data) if data else None
        if not mime:
            return None
        size = image_size(data, mime)
        if size and (not size[0] or not size[1] or not 0.75 <= size[0] / size[1] <= 1.34):
            return None
        return f'data:{mime};base64,{base64.b64encode(data).decode("ascii")}'

    tried = 0
    for url in feed_icon_urls:
        if tried >= ICON_TRIES:
            return None
        tried += 1
        icon = usable(url)
        if icon:
            return icon
    root = urlparse(site_url)
    base = f'{root.scheme}://{root.netloc}/'
    try:
        page = fetch(base, PAGE_MAX_BYTES, True).decode('utf-8', 'replace')
        candidates = page_icon_urls(page, base)
    except FeedError:
        candidates = [f'{base}favicon.ico']   # the home page cannot be read: only the default address remains
    for url in candidates:
        if tried >= ICON_TRIES:
            break
        tried += 1
        icon = usable(url)
        if icon:
            return icon
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Building the static site
# ---------------------------------------------------------------------------------------------------------------------
ICON_CHECK_EVERY = 24 * 3600   # a feed's icon is looked up again at most once a day (it is several requests per feed)
KEEP_UNCHANGED = 3 * 3600      # a result without new items is not rewritten for this many seconds (fewer changes in the repository)
WORKERS = 8


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, document):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(document, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)


def build_site(catalog, out_dir, fetch=fetch_url, fetch_binary=fetch_bytes, check_host=is_public_host, now=time.time, workers=WORKERS):
    """Fetches the feeds of the catalog and writes feeds.json, feed/<id>.json and meta.json in `out_dir`. The previous contents of
    `out_dir` (the last published state) serve the "stale" result and the reuse of icons. Returns a dict id -> {ok, stale, error}."""
    catalog = normalize_catalog(catalog)
    current = int(now())
    previous_listing = {e['id']: e for e in (_read_json(os.path.join(out_dir, 'feeds.json')) or []) if isinstance(e, dict) and 'id' in e}

    def one(feed):
        feed_id = feed['id']
        path = os.path.join(out_dir, 'feed', f'{feed_id}.json')
        old = _read_json(path)
        icon_entry = previous_listing.get(feed_id, {})
        status = {'ok': True, 'stale': False, 'error': None}
        icons = []
        try:
            parsed = parse_feed(fetch(feed['url']))
            icons = parsed['icons']
            result = {'id': feed_id, 'name': feed['name'], 'fetched': current, 'items': parsed['items']}
            if old and old.get('items') == result['items'] and not old.get('stale') and not old.get('error') \
                    and current - old.get('fetched', 0) < KEEP_UNCHANGED:
                result = old   # nothing new: the file is not rewritten (the repository does not get a pointless change)
        except FeedError as e:
            status = {'ok': False, 'stale': False, 'error': str(e)}
            if old and old.get('items') and current - old.get('fetched', 0) < STALE_MAX:
                result = {**old, 'stale': True}   # an old result is better than an empty panel
                status['stale'] = True
            else:
                result = {'id': feed_id, 'name': feed['name'], 'fetched': current, 'items': [], 'error': str(e)}
        if result is not old:
            _write_json(path, result)
        # icon: from the previous catalog if checked recently; otherwise looked up again (from the feed, then from the home page)
        icon, checked = icon_entry.get('icon'), icon_entry.get('icon_checked', 0)
        if not checked or current - checked >= ICON_CHECK_EVERY:
            try:
                icon = find_icon(icons, feed['url'], fetch_binary, check_host)
            except Exception:   # one icon must not stop the rest
                log.exception('icon of feed %s', feed_id)
            checked = current
        return feed_id, status, {'id': feed_id, 'name': feed['name'], 'icon': icon, 'icon_checked': checked}

    results = {}
    listing = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for feed_id, status, entry in pool.map(one, catalog):
            results[feed_id] = status
            listing[feed_id] = entry
    _write_json(os.path.join(out_dir, 'feeds.json'), [listing[f['id']] for f in catalog])
    # feeds removed from the catalog also disappear from the published files
    feed_dir = os.path.join(out_dir, 'feed')
    if os.path.isdir(feed_dir):
        known = {f['id'] for f in catalog}
        for name in os.listdir(feed_dir):
            if name.endswith('.json') and name[:-5] not in known:
                os.remove(os.path.join(feed_dir, name))
    iso = datetime.fromtimestamp(current, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    _write_json(os.path.join(out_dir, 'meta.json'), {'generated': iso, 'feeds': results})
    return results


def load_catalog(path):
    """The feed catalog from a JSON file (a list of {name, url, id?}); ValueError with a description when it is bad."""
    with open(path, encoding='utf-8') as f:
        return normalize_catalog(json.load(f))
