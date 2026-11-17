"""Tests of the Farside page processing (no network): PYTHONPATH=src python3 -m unittest tests.test_etf -v"""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from finactions import etf


def make_html(kind='btc', days=60, extra_row=None):
    """A page in the Farside layout: header, (for ETH Fee and Seed), an empty row, days, Total and the footer tables."""
    funds = ['IBIT', 'FBTC', 'GBTC'] if kind == 'btc' else ['ETHA', 'FETH', 'ETHE']
    cell = lambda t: f'<td>{t}</td>'
    rows = [f'<tr>{cell("Date" if kind == "btc" else "")}{"".join(cell(f) for f in funds)}{cell("Total")}</tr>']
    if kind == 'eth':
        rows.append('<tr>' + cell('Fee') + cell('0.25%') + cell('0.25%') + cell('2.50%') + cell('') + '</tr>')
        rows.append('<tr>' + cell('Seed') + cell('10.6') + cell('') + cell('9,199.3*') + cell('10,360') + '</tr>')
    rows.append('<tr></tr>')
    start = date(2024, 1, 11)
    for i in range(days):
        d = start + timedelta(days=i)
        label = f'{d.day:02d} {d.strftime("%b")} {d.year}'
        if i == 0:
            values = ['111.7', '-', '(95.1)']; total = '16.6'
        elif i == 1:
            values = ['1,234.5', '(27.7)', '0.0']; total = '1,206.8'
        else:
            values = ['-', '-', '-']; total = '0.0'
        rows.append(f'<tr>{cell(label)}{"".join(cell(v) for v in values)}{cell(total)}</tr>')
    if extra_row:
        rows.append(extra_row)
    rows.append(f'<tr>{cell("Total")}{"".join(cell("1,000") for _ in funds)}{cell("(3,000)")}</tr>')
    return ('<html><head><script>var x = "<table class=\\"etf\\">";</script></head><body>'
            f'<table class="etf">{"".join(rows)}</table>'
            '<table class="tfooter"><tr><td>How can we help?</td></tr></table></body></html>')


class ConversionTest(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(etf.parse_date('19 Aug 2024'), '2024-08-19')
        self.assertEqual(etf.parse_date(' 1 Jan 2025 '), '2025-01-01')
        for bad in ['2024-08-19', '19 Sie 2024', '31 Foo 2024', '']:
            with self.assertRaises(etf.ParseError):
                etf.parse_date(bad)

    def test_numbers(self):
        n = etf.parse_number
        self.assertEqual(n('(27.7)'), -27.7)
        self.assertEqual(n('1,234.5'), 1234.5)
        self.assertEqual(n('(5,530.0)'), -5530.0)
        self.assertEqual(n('9,199.3*'), 9199.3)
        self.assertEqual(n('0.25%'), 0.25)
        self.assertEqual(n('0.0'), 0.0)
        self.assertEqual(n('-12.5'), -12.5)
        self.assertIsNone(n('-'))
        self.assertIsNone(n(''))
        with self.assertRaises(etf.ParseError):
            n('abc')


class ParseTest(unittest.TestCase):
    def test_btc_table(self):
        t = etf.parse_html(make_html('btc'))
        self.assertEqual(t['funds'], ['IBIT', 'FBTC', 'GBTC'])
        self.assertEqual(len(t['rows']), 60)
        self.assertEqual(t['rows'][0], {'date': '2024-01-11', 'IBIT': 111.7, 'FBTC': None, 'GBTC': -95.1, 'total': 16.6})
        self.assertEqual(t['rows'][1], {'date': '2024-01-12', 'IBIT': 1234.5, 'FBTC': -27.7, 'GBTC': 0.0, 'total': 1206.8})
        self.assertEqual(t['totals'], {'IBIT': 1000.0, 'FBTC': 1000.0, 'GBTC': 1000.0, 'total': -3000.0})
        self.assertNotIn('fees', t)

    def test_eth_table_has_fee_and_seed(self):
        t = etf.parse_html(make_html('eth'))
        self.assertEqual(t['funds'], ['ETHA', 'FETH', 'ETHE'])
        self.assertEqual(t['fees'], {'ETHA': 0.25, 'FETH': 0.25, 'ETHE': 2.5})
        self.assertEqual(t['seed'], {'ETHA': 10.6, 'FETH': None, 'ETHE': 9199.3})

    def test_rows_are_sorted_and_deduplicated(self):
        html = make_html('btc', extra_row='<tr><td>11 Jan 2024</td><td>5</td><td>6</td><td>7</td><td>18</td></tr>')
        t = etf.parse_html(html)
        self.assertEqual(len(t['rows']), 60)
        self.assertEqual(t['rows'][0]['IBIT'], 5.0)   # the later row with the same date wins
        dates = [r['date'] for r in t['rows']]
        self.assertEqual(dates, sorted(dates))

    def test_wrong_pages_are_rejected(self):
        challenge = '<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>'
        for html, fragment in [(challenge, 'table'), (make_html('btc', days=5), 'days of data'),
                               (make_html('btc', extra_row='<tr><td>12 Feb 2024</td><td>1</td></tr>'), 'cells')]:
            with self.assertRaises(etf.ParseError) as ctx:
                etf.parse_html(html)
            self.assertIn(fragment, str(ctx.exception))


class BuildTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.html = os.path.join(self._tmp.name, 'html')
        self.out = os.path.join(self._tmp.name, 'out')
        os.makedirs(self.html)

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, name, text):
        with open(os.path.join(self.html, name), 'w', encoding='utf-8') as f:
            f.write(text)

    def test_run_writes_crypto_etfs_json_with_both_assets(self):
        self.put('btc-etf.html', make_html('btc'))
        self.put('eth-etf.html', make_html('eth'))
        doc = etf.run(self.html, self.out, now=1_800_000_000)
        with open(os.path.join(self.out, 'crypto-etfs.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f), doc)
        self.assertEqual(sorted(doc['assets']), ['btc', 'eth'])
        self.assertEqual(doc['assets']['btc']['funds'], ['IBIT', 'FBTC', 'GBTC'])
        self.assertEqual(doc['unit'], 'US$ million')
        self.assertIn('preliminary', doc['assets']['btc'])

    def test_missing_page_keeps_previous_data_of_that_asset(self):
        self.put('btc-etf.html', make_html('btc'))
        self.put('eth-etf.html', make_html('eth'))
        first = etf.run(self.html, self.out, now=1_800_000_000)
        os.remove(os.path.join(self.html, 'eth-etf.html'))
        second = etf.run(self.html, self.out, now=1_800_003_600)
        self.assertEqual(second['assets']['eth'], first['assets']['eth'])   # unchanged
        self.assertEqual(second['generated'], '2027-01-15T09:00:00Z')

    def test_missing_page_without_previous_data_is_an_error(self):
        self.put('btc-etf.html', make_html('btc'))
        with self.assertRaises(etf.ParseError):
            etf.run(self.html, self.out)

    def test_bad_page_does_not_touch_the_previous_json(self):
        self.put('btc-etf.html', make_html('btc'))
        self.put('eth-etf.html', make_html('eth'))
        etf.run(self.html, self.out, now=1_800_000_000)
        path = os.path.join(self.out, 'crypto-etfs.json')
        with open(path, 'rb') as f:
            before = f.read()
        self.put('btc-etf.html', '<html><title>Just a moment...</title></html>')   # a Cloudflare challenge instead of data
        with self.assertRaises(etf.ParseError):
            etf.run(self.html, self.out)
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), before)


if __name__ == '__main__':
    unittest.main()
