#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DEST="${1:-macmini:/Users/tony/projects/podcasts/}"

rsync -az \
  README.md \
  requirements.txt \
  .env \
  .env.example \
  src \
  scripts \
  launchd \
  config \
  docs \
  "$DEST"

echo "Synced project code and config to $DEST"
