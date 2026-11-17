#!/usr/bin/env bash
# Publishes the data directory as the "data" branch: always ONE commit without history (orphan + forced push), so the repository does
# not bloat with thousands of data commits, while GitHub Pages (set to the data branch, root folder) serves the latest state.
#   scripts/site-publish.sh [directory=site] [branch=data] [note]
# When nothing has changed compared with what is already published, nothing is pushed (fewer Pages rebuilds).
set -euo pipefail
dir="${1:-site}"
branch="${2:-data}"
note="${3:-update}"
static_dir="$(cd "$(dirname "$0")/.." && pwd)/static"

cd "$dir"
cp -r "$static_dir"/. .            # index.html and other static files from the code repository
touch .nojekyll                    # Pages should serve the files without Jekyll processing
git add -A
if git rev-parse --verify -q HEAD >/dev/null && git diff --cached --quiet; then
  echo "no changes: nothing to publish"
  exit 0
fi
if git rev-parse --verify -q HEAD >/dev/null; then
  git checkout --quiet --orphan publish-tmp   # a new commit without a parent: the history does not grow
else
  git checkout --quiet -b publish-tmp         # first publication: the branch has no commit yet
fi
git add -A
git commit --quiet -m "data: ${note} ($(date -u +%Y-%m-%dT%H:%MZ))"
git push --quiet --force origin "publish-tmp:${branch}"
echo "published branch ${branch}"
