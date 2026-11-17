"""Flows into crypto ETFs (Farside Investors): from saved HTML pages to a single JSON file.

Two HTML files in the input directory (saved by scripts/scrape-etf.js, because the Farside page is protected by Cloudflare and a
plain HTTP request is not enough):
  btc-etf.html   the content of https://farside.co.uk/bitcoin-etf-flow-all-data/
  eth-etf.html   the content of https://farside.co.uk/ethereum-etf-flow-all-data/
From the `table.etf` table in each of them one common file, crypto-etfs.json, is built in a form convenient for D3.js. The format is
the same as the one returned by the /api/crypto-etfs endpoint of the yfinance service of the dashboard, so the dashboard can read it
without changes in its drawing code:

  {
    "generated": "2026-11-13T13:50:00Z", "unit": "US$ million",
    "assets": {
      "btc": {
        "name": "Bitcoin", "source_url": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
        "file": "btc-etf.html", "file_modified": "2026-11-13T13:49:00Z",
        "funds": ["IBIT", "FBTC", ...],                  # column order from the page, without Total
        "fees": {"ETHA": 0.25, ...},                     # only if the page has a Fee row (percent)
        "seed": {"ETHA": 10.6, ...},                     # only if the page has a Seed row (US$ million)
        "first_date": "2024-01-11", "last_date": "2026-11-13",
        "preliminary": null,                             # or {"date", "funds": [...]}: the last finished day lacks data of some funds
        "rows": [{"date": "2024-01-11", "IBIT": 111.7, "GBTC": -95.1, "BTC": null, ..., "total": 655.3}, ...],
        "totals": {"IBIT": 64540, ..., "total": 55090}   # the Total row of the page
      },
      "eth": {...}
    }
  }

Conversions: dates "19 Aug 2024" -> "2024-08-19"; numbers in parentheses "(27.7)" (red on the page) -> -27.7; thousands
separators disappear; "-" (no data) -> null (while "0.0" is a real zero). Rows are in ascending date order; a day with only nulls
and total 0 is a day without trading or without data yet.
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

log = logging.getLogger('finactions')

JSON_NAME = 'crypto-etfs.json'
MIN_ROWS = 50              # fewer days suggest that the wrong page was saved (e.g. a Cloudflare challenge)

# file in the directory -> (id in the JSON, name, source address)
SOURCES = {
    'btc-etf.html': ('btc', 'Bitcoin', 'https://farside.co.uk/bitcoin-etf-flow-all-data/'),
    'eth-etf.html': ('eth', 'Ethereum', 'https://farside.co.uk/ethereum-etf-flow-all-data/'),
}

MONTHS = {m: i for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 1)}
DATE_RE = re.compile(r'^(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})$')
TICKER_RE = re.compile(r'^[A-Za-z][A-Za-z0-9.]*$')
NUMBER_RE = re.compile(r'^\(?-?\d[\d,]*(\.\d+)?\)?$')


class ParseError(Exception):
    pass


def parse_date(text):
    """"19 Aug 2024" -> '2024-08-19' (English month abbreviations, regardless of locale settings)."""
    m = DATE_RE.match(text.strip())
    if not m or m.group(2) not in MONTHS:
        raise ParseError(f'invalid date: {text!r}')
    return f'{int(m.group(3)):04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}'


def parse_number(text):
    """"1,234.5" -> 1234.5, "(27.7)" -> -27.7, "9,199.3*" -> 9199.3, "0.25%" -> 0.25; "-" and empty -> None."""
    t = text.strip().rstrip('*%').strip()
    if t in ('', '-'):
        return None
    if not NUMBER_RE.match(t):
        raise ParseError(f'invalid number: {text!r}')
    negative = t.startswith('(') or t.startswith('-')
    value = float(t.strip('()-').replace(',', ''))
    return -value if negative else value


class _TableParser(HTMLParser):
    """Collects table rows as lists of cell texts (only tables with the class "etf")."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []   # a list of tables; a table is a list of rows; a row is a list of cell texts
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            classes = (dict(attrs).get('class') or '').split()
            self._table = [] if 'etf' in classes else None
        elif self._table is not None and tag == 'tr':
            self._row = []
        elif self._row is not None and tag in ('td', 'th'):
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._cell is not None:
            self._row.append(' '.join(''.join(self._cell).split()))
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == 'table' and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_table(rows):
    """Rows of a Farside table -> a dict: funds, fees, seed, rows, totals (without file metadata)."""
    header = next((r for r in rows if len(r) >= 3 and r[-1] == 'Total' and TICKER_RE.match(r[1] or '')), None)
    if header is None:
        raise ParseError('header row not found (fund tickers and the Total column)')
    funds = header[1:-1]
    if not funds or len(set(funds)) != len(funds):
        raise ParseError(f'invalid fund names: {funds}')

    def by_fund(cells):
        return {name: parse_number(cell) for name, cell in zip(funds, cells[1:-1])}

    out = {'funds': funds, 'rows': [], 'totals': None}
    by_date = {}
    for cells in rows:
        if not cells or cells is header:
            continue
        label = cells[0].strip()
        if DATE_RE.match(label):
            if len(cells) != len(header):
                raise ParseError(f'row {label}: {len(cells)} cells instead of {len(header)}')
            record = {'date': parse_date(label), **by_fund(cells), 'total': parse_number(cells[-1])}
            by_date[record['date']] = record   # a repeated date: the later row wins
        elif label in ('Fee', 'Seed', 'Total') and len(cells) == len(header):
            values = {**by_fund(cells), 'total': parse_number(cells[-1])}
            if label == 'Fee':
                out['fees'] = {k: v for k, v in values.items() if k != 'total'}
            elif label == 'Seed':
                out['seed'] = {k: v for k, v in values.items() if k != 'total'}
            else:
                out['totals'] = values
    out['rows'] = [by_date[d] for d in sorted(by_date)]
    if len(out['rows']) < MIN_ROWS:
        raise ParseError(f'only {len(out["rows"])} days of data (at least {MIN_ROWS} expected)')
    return out


def parse_html(text):
    """A whole HTML page -> the parse_table result for its "etf" table."""
    parser = _TableParser()
    parser.feed(text)
    parser.close()
    if not parser.tables:
        raise ParseError('no table with the class "etf" found')
    return parse_table(max(parser.tables, key=len))


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def preliminary_day(rows, funds, today):
    """The last finished day (a date earlier than `today`, because today's row is an empty placeholder) may still be incomplete:
    Farside adds funds during the day ("-" = no data, "0.0" = zero). Returns {'date', 'funds'} (funds without data that had a number
    the day before) or None when the day is complete."""
    done = [r for r in rows if r['date'] < today]
    if len(done) < 2:
        return None
    last, prev = done[-1], done[-2]
    pending = [f for f in funds if last.get(f) is None and prev.get(f) is not None]
    return {'date': last['date'], 'funds': pending} if pending else None


def build_document(directory, now, previous=None):
    """Builds the JSON document from the HTML files in `directory` (a file's modification time = the moment it was fetched). A
    missing file is not an error if `previous` (the previous document) has data of that asset: it stays unchanged (e.g. one of the
    pages is temporarily down). ParseError when a file exists but cannot be read, or when a file is missing and there is no previous data."""
    assets = {}
    today = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
    for filename, (key, name, url) in SOURCES.items():
        path = os.path.join(directory, filename)
        if not os.path.isfile(path):
            old = ((previous or {}).get('assets') or {}).get(key)
            if not old:
                raise ParseError(f'{filename}: the file is missing and there is no previous data either')
            log.warning('%s: the file is missing, keeping the previous data (%s)', filename, old.get('last_date'))
            assets[key] = old
            continue
        with open(path, encoding='utf-8', errors='replace') as f:
            try:
                table = parse_html(f.read())
            except ParseError as e:
                raise ParseError(f'{filename}: {e}')
        rows = table['rows']
        assets[key] = {'name': name, 'source_url': url, 'file': filename, 'file_modified': _iso(os.path.getmtime(path)),
                       'first_date': rows[0]['date'], 'last_date': rows[-1]['date'],
                       'preliminary': preliminary_day(rows, table['funds'], today), **table}
    return {'generated': _iso(now), 'unit': 'US$ million', 'assets': assets}


def write_json(path, document):
    """Atomic write (a temporary file and a rename), compact JSON in UTF-8."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(document, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)


def run(html_dir, out_dir, now=None):
    """Reads the HTML files and builds crypto-etfs.json in `out_dir` (the previous file there fills in a missing asset)."""
    now = time.time() if now is None else now
    target = os.path.join(out_dir, JSON_NAME)
    previous = None
    if os.path.isfile(target):
        try:
            with open(target, encoding='utf-8') as f:
                previous = json.load(f)
        except (OSError, ValueError):
            previous = None
    document = build_document(html_dir, now, previous)
    os.makedirs(out_dir, exist_ok=True)
    write_json(target, document)
    return document
