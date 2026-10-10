#!/bin/sh
# Pack art/ into graphics/ and open Factorio straight into the art lab scenario.
#   tools/lab.sh
# Sprites are read once at startup, so this has to start a fresh game process.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP="${FACTORIO_APP:-$HOME/Library/Application Support/Steam/steamapps/common/Factorio/factorio.app}"

# `open` hands the arguments to an already-running Factorio, which ignores them,
# so a stale window would silently keep showing the old sprites.
if pgrep -f "factorio.app/Contents/MacOS/factorio" >/dev/null; then
  echo "Factorio is already running; quit it first so the new sprites load." >&2
  exit 1
fi

uv run "$ROOT/tools/pack-art.py"
open "$APP" --args --load-scenario boxelder/lab
