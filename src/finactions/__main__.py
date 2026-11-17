"""Command line: python -m finactions {etf|feeds} …  (with PYTHONPATH=src, no package installation needed).

  etf    reads btc-etf.html and eth-etf.html from --html-dir and writes crypto-etfs.json in --out
  feeds  fetches the feeds from --config (default config/feeds.json) and writes feeds.json, feed/<id>.json and meta.json in --out

The --out directory is a checkout of the branch with the published data (scripts/site-prepare.sh): its existing contents are reused
(icons, the "stale" result, skipping the rewrite of unchanged files).
Exit code: 0 - success; 1 - an error that should stop publishing (the workflow then turns red and pushes nothing).
"""
import argparse
import logging
import os
import sys

from . import etf, feeds


def main(argv=None):
    parser = argparse.ArgumentParser(prog='finactions', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    e = sub.add_parser('etf', help='crypto-etfs.json from the saved Farside pages')
    e.add_argument('--html-dir', required=True, help='directory with btc-etf.html and eth-etf.html')
    e.add_argument('--out', required=True, help='output directory (crypto-etfs.json)')
    f = sub.add_parser('feeds', help='RSS/Atom feeds as JSON files')
    f.add_argument('--config', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'config', 'feeds.json'),
                   help='feed catalog (default config/feeds.json)')
    f.add_argument('--out', required=True, help='output directory (feeds.json, feed/, meta.json)')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s %(message)s')

    if args.command == 'etf':
        try:
            document = etf.run(args.html_dir, args.out)
        except (etf.ParseError, OSError) as exc:
            print(f'::error::ETF: {exc}')   # an annotation visible in GitHub Actions
            return 1
        for key, asset in document['assets'].items():
            pending = asset.get('preliminary')
            print(f'ETF {key}: {len(asset["rows"])} days, last one {asset["last_date"]}'
                  + (f', preliminary data (missing: {", ".join(pending["funds"])})' if pending else ''))
        return 0

    try:
        catalog = feeds.load_catalog(args.config)
    except (OSError, ValueError) as exc:
        print(f'::error::feed catalog {args.config}: {exc}')
        return 1
    results = feeds.build_site(catalog, args.out)
    failed = {k: v for k, v in results.items() if not v['ok']}
    for feed_id, status in failed.items():
        print(f'::warning::feed {feed_id}: {status["error"]}' + (' (keeping the older result)' if status['stale'] else ''))
    print(f'RSS: {len(results) - len(failed)} of {len(results)} feeds fetched')
    return 1 if len(failed) == len(results) else 0   # a total failure (e.g. no network) stops publishing


if __name__ == '__main__':
    sys.exit(main())
