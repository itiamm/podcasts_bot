#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.local.youtube-podcast-audio"
SRC="$ROOT_DIR/launchd/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/logs"
sed "s#__ROOT_DIR__#$ROOT_DIR#g" "$SRC" > "$DST"
plutil -lint "$DST" >/dev/null

launchctl bootout "gui/$(id -u)" "$DST" 2>/dev/null || launchctl unload "$DST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST" 2>/dev/null || launchctl load "$DST"
launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true

echo "Installed $LABEL. It will run every day at 08:00."
echo "Logs:"
echo "  $ROOT_DIR/logs/launchd.out.log"
echo "  $ROOT_DIR/logs/launchd.err.log"
