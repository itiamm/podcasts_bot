#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)}"

if [[ ! -d ".venv" ]] || ! ".venv/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  rm -rf .venv
  "$PYTHON_BIN" -m venv .venv
fi

".venv/bin/python" -m pip install -r requirements.txt >/dev/null
".venv/bin/python" -m src.podcast_sync "$@"
