# finactions

Static data for the dashboard, refreshed by GitHub Actions and served from GitHub Pages:

- **ETFs on BTC and ETH**: daily flows (Farside Investors) as `crypto-etfs.json`,
- **RSS/Atom feeds**: a catalog and the latest items as `feeds.json` and `feed/<id>.json`.

This replaces two functions of the `yfinance/` service of the dashboard (the endpoints `/api/crypto-etfs`, `/api/feeds`, `/api/feed`)
where there is nowhere to host a container. Stock prices (Yahoo) stay in that service, because they need a password and a server.

## How it works

```
 GitHub Actions (cron)                          "data" branch (one commit)           browser (dashboard)
 ┌──────────────────────────┐   forced push    ┌────────────────────────┐   CORS: *  ┌────────────────────┐
 │ etf.yml   every hour     │ ───────────────▶ │ crypto-etfs.json       │ ─────────▶ │ Crypto ETFs panel  │
 │ feeds.yml every 15 min   │                  │ feeds.json, feed/*.json│            │ RSS feeds panel    │
 └──────────────────────────┘                  │ meta.json, index.html  │            └────────────────────┘
        code: the main branch                  └────────────────────────┘  GitHub Pages (from the data branch)
```

- The code is on the `main` branch, the **data on the `data` branch**. Every publication replaces the `data` branch with one new
  parentless commit (orphan + `git push --force`), so the repository does not grow by thousands of data commits. GitHub Pages serves
  the latest state.
- Nothing is pushed when the data has not changed (fewer Pages rebuilds and less noise).
- Both workflows share the group `concurrency: publish-data`, so they never publish at the same time.
- No secrets: the built-in `GITHUB_TOKEN` is enough (the `contents: write` permission).

## Layout

```
.github/workflows/   etf.yml, feeds.yml (publishing), ci.yml (tests)
config/feeds.json    the catalog of RSS feeds (name, url, optionally id): add and remove feeds here
scripts/             scrape-etf.js (Playwright -> Farside HTML), site-prepare.sh and site-publish.sh (the data branch)
src/finactions/      etf.py (HTML -> crypto-etfs.json), feeds.py (RSS -> JSON files), __main__.py (command line)
static/              index.html added to the data branch
tests/               tests without a network (45)
```

Python: the standard library only (3.12). Node 22 and Playwright are used only to fetch the Farside page.

## One-time setup on GitHub

1. Create a **public** repository (Pages from a branch on a free account requires a public repository; the data is public anyway) and
   push `main` (you decide when and where; this directory has no commit and no remote yet).
2. **Settings -> Actions -> General -> Workflow permissions**: "Read and write permissions".
3. Run the workflows manually (the Actions tab -> a workflow -> "Run workflow"), first **RSS feeds**, then **ETF flows**. The first run
   creates the `data` branch.
4. **Settings -> Pages**: Source "Deploy from a branch", branch `data`, folder `/ (root)`. After a moment the data is available at
   `https://<account>.github.io/<repository>/` (e.g. `.../crypto-etfs.json`).
5. Optionally: a custom domain, or a neutral account/repository name if the address should not reveal what it is used for (the address
   is visible in the browser of anyone who opens the dashboard, so it cannot be hidden).

## The files and their shape (the contract with the dashboard)

| File | Counterpart in the `yfinance/` service | Contents |
|---|---|---|
| `crypto-etfs.json` | `/api/crypto-etfs` | unchanged: `{generated, unit, assets: {btc, eth}}`, an asset has e.g. `funds`, `rows`, `totals`, `file_modified`, `preliminary` |
| `feeds.json` | `/api/feeds` | `[{id, name, icon, icon_checked}]`, `icon` is a data URI or `null` |
| `feed/<id>.json` | `/api/feed?id=<id>` | `{id, name, fetched, items: [{title, link, ts, summary, image, author, tags}]}` |
| `meta.json` | - | `{generated, feeds: {id: {ok, stale, error}}}` for monitoring |

Differences the dashboard must take into account:

- **A feed error** is not an HTTP status but a file: `feed/<id>.json` then has `error` (and empty `items`), and when we have a result
  younger than 6 h, it is kept with `"stale": true`.
- **No password** (`X-Password`): the files are public.
- GitHub Pages answers with `Access-Control-Allow-Origin: *` and a `Cache-Control: max-age=600` header, so the data may be up to ~10 min
  older than what was published.

Hooking it up in the dashboard (a separate change, not done yet): a "static data address" setting next to the YFinance address, from
which the ETF and RSS panels read the files above instead of `/api/...`.

## Schedule

- `feeds.yml`: `*/15 * * * *` (UTC).
- `etf.yml`: `7 6-19 * * *`, i.e. every hour at :07 between 6:00 and 19:00 UTC. Farside fills in the data of the latest day during the
  day (e.g. IBIT is sometimes added later), so later fetches correct it, and while the day is incomplete the JSON has `preliminary`
  ({date, funds}), which the dashboard shows as "partial".
- You change them in the `cron:` entries of the workflow files. GitHub may delay scheduled runs by several to a dozen or so minutes.

## Limitations and risks

- **Cloudflare/Farside.** The Farside page is protected by Cloudflare, which can be sensitive to data-center addresses, so fetching it on
  GitHub-hosted runners **may not work** (the workflow then fails and the previously published data stays unchanged). Workaround: set the
  repository variable `ETF_RUNNER` (Settings -> Secrets and variables -> Actions -> Variables) to the label of your own runner (e.g.
  `self-hosted` on the same server where the scraper runs today); `etf.yml` will use it instead of the default `ubuntu-24.04`. This could not be
  verified without running on GitHub.
- **Schedule deactivation.** GitHub disables scheduled workflows in a public repository after 60 days without activity. If the data stops
  refreshing, check in the Actions tab whether a workflow has been disabled, and enable it again.
- **Limits.** A public repository has free Actions minutes. Pages from a branch has a soft limit of 10 rebuilds per hour: here there are
  at most about 5 (feeds every 15 min + etf every hour), and unchanged data is not published at all.
- **Third-party feed content** ends up in the files only as plain text (no HTML), with http/https links; the dashboard inserts it as text.

## Running locally

```bash
export PYTHONPATH=src
python3 -m unittest discover -s tests            # tests, no network
python3 -m finactions feeds --out /tmp/site      # real RSS feeds into the directory /tmp/site
python3 -m finactions etf --html-dir out --out /tmp/site   # needs btc-etf.html and eth-etf.html in the directory out/

npm ci && npx playwright install chromium        # once: the scraper's dependencies
node scripts/scrape-etf.js out                   # fetches btc-etf.html and eth-etf.html into out/
```

The scripts `site-prepare.sh` and `site-publish.sh` are meant for Actions (they need `GITHUB_TOKEN` and `GITHUB_REPOSITORY`); for local
trials `FINACTIONS_REMOTE_URL` may point to a bare repository instead of GitHub.
The workflows call them as `bash scripts/...`, so it does not matter whether the executable bit survived the upload to GitHub (an upload
through the web interface drops it, which made a plain `scripts/site-prepare.sh` fail with exit code 126).
