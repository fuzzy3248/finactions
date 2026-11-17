#!/usr/bin/env bash
# Prepares a directory with the last published data (the "data" branch of the same repository), or an empty one if the branch does
# not exist yet. The `python -m finactions …` commands write into this directory, and scripts/site-publish.sh then publishes it.
#   scripts/site-prepare.sh [directory=site] [branch=data]
# Requires the variables GITHUB_TOKEN and GITHUB_REPOSITORY (in a workflow GitHub provides them; the token has contents: write).
set -euo pipefail
dir="${1:-site}"
branch="${2:-data}"
# FINACTIONS_REMOTE_URL (for local trials only) replaces the GitHub address, e.g. with a directory holding a bare repository.
if [ -n "${FINACTIONS_REMOTE_URL:-}" ]; then
  url="$FINACTIONS_REMOTE_URL"
else
  : "${GITHUB_TOKEN:?GITHUB_TOKEN is not set}" "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
  url="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
fi

rm -rf "$dir"
if git ls-remote --exit-code --heads "$url" "$branch" >/dev/null 2>&1; then
  git clone --quiet --depth 1 --branch "$branch" "$url" "$dir"
  echo "fetched branch $branch ($(git -C "$dir" rev-parse --short HEAD))"
else
  mkdir -p "$dir"
  git -C "$dir" init --quiet -b "$branch"
  git -C "$dir" remote add origin "$url"
  echo "branch $branch does not exist yet: starting from an empty directory"
fi
git -C "$dir" config user.name "github-actions[bot]"
git -C "$dir" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
