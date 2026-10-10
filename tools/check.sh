#!/bin/sh
# Load the mod in a throwaway Factorio profile, without a window:
#   1. --dump-data runs the data stage (data.lua, the generated sheet .lua files)
#      and reports prototype fields the engine ignored.
#   2. A headless server runs the lab scenario, kills its biters, and counts what
#      is left, which exercises scenarios/lab/control.lua and creates corpses.
# Neither step decodes sprites (that only happens with graphics), so a sheet that
# is the wrong size or layout still has to be checked in game with tools/lab.sh.
#
#   tools/check.sh [seconds]     (default 10)
#
# The profile lives in $TMPDIR so the real one in ~/Library/Application
# Support/factorio (saves, mod list, settings) is never written to.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP="${FACTORIO_APP:-$HOME/Library/Application Support/Steam/steamapps/common/Factorio/factorio.app}"
BIN="$APP/Contents/MacOS/factorio"
SECONDS_TO_RUN="${1:-10}"

PROFILE=$(mktemp -d "${TMPDIR:-/tmp}/minnesota-bugs-check.XXXXXX")
trap 'rm -rf "$PROFILE"' EXIT
mkdir -p "$PROFILE/mods"
ln -s "$ROOT" "$PROFILE/mods/minnesota-bugs"
printf '[path]\nread-data=__PATH__system-read-data__\nwrite-data=%s\n' "$PROFILE" > "$PROFILE/config.ini"
# Same official mods as the real profile, so Space Age's data stage runs too.
cat > "$PROFILE/mods/mod-list.json" <<'EOF'
{"mods": [
  {"name": "base", "enabled": true},
  {"name": "elevated-rails", "enabled": true},
  {"name": "quality", "enabled": true},
  {"name": "space-age", "enabled": true},
  {"name": "minnesota-bugs", "enabled": true}
]}
EOF
# The server insists on every key, so start from the shipped example. Unlisted so
# nothing is announced to the matchmaking server or LAN, and no auto_pause because
# an empty server would otherwise never tick the scenario.
jq '. + {name: "minnesota-bugs-check", visibility: {public: false, lan: false},
         require_user_verification: false, auto_pause: false}' \
  "$APP/Contents/data/server-settings.example.json" > "$PROFILE/server-settings.json"

fail=0

echo "== data stage"
if "$BIN" -c "$PROFILE/config.ini" --mod-directory "$PROFILE/mods" \
     --dump-data --check-unused-prototype-data >"$PROFILE/dump.out" 2>&1; then
  echo "ok"
else
  echo "FAILED"; fail=1
fi
rg -i 'error|unused' "$PROFILE/factorio-current.log" || true
# Loading cleanly is not the same as taking effect: a later mod could overwrite
# the animations, so confirm the final prototypes point at this mod's sheets.
sheets=$(jq -r '
  (.unit["small-biter", "medium-biter"] | .run_animation, .attack_parameters.animation),
  (.corpse["small-biter-corpse", "medium-biter-corpse"] | .animation, .decay_animation)
  | .layers[0].filenames[0]' "$PROFILE/script-output/data-raw-dump.json" 2>/dev/null) || sheets=""
printf '%s\n' "$sheets" | sed 's/^/  /'
if [ "$(printf '%s\n' "$sheets" | rg -c '^__minnesota-bugs__/')" != 8 ]; then
  echo "FAILED: expected all 8 animations to use __minnesota-bugs__ sheets"; fail=1
fi

echo "== lab scenario, ${SECONDS_TO_RUN}s headless"
# The server reads console commands from stdin and shuts down on EOF, so stdin is a
# FIFO held open on fd 3. That also lets this script send it a probe at the end.
mkfifo "$PROFILE/console"
"$BIN" -c "$PROFILE/config.ini" --mod-directory "$PROFILE/mods" \
  --start-server-load-scenario minnesota-bugs/lab --bind 127.0.0.1 \
  --server-settings "$PROFILE/server-settings.json" <"$PROFILE/console" >"$PROFILE/server.out" 2>&1 &
pid=$!
# The server ignores SIGTERM while running, so anything that goes wrong below must
# not leave it holding the port.
trap 'kill -9 "$pid" 2>/dev/null; rm -rf "$PROFILE"' EXIT
exec 3>"$PROFILE/console"
sleep "$SECONDS_TO_RUN"

if kill -0 "$pid" 2>/dev/null; then
  # Kept short and one per read: the console treats whatever is buffered as a
  # single command and cut off a longer one.
  echo '/silent-command local s = game.surfaces["boxelder-lab"]; print("PROBE units=" .. s.count_entities_filtered{type = "unit"}); for _, u in pairs(s.find_entities_filtered{type = "unit"}) do u.die() end' >&3
  sleep 2
  echo '/silent-command print("PROBE corpses=" .. game.surfaces["boxelder-lab"].count_entities_filtered{type = "corpse", name = {"small-biter-corpse", "medium-biter-corpse"}})' >&3
  sleep 1
  # 2.0 has no /quit console command; SIGINT is the clean shutdown.
  exec 3>&-
  kill -INT "$pid" 2>/dev/null || true
  i=0
  while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 30 ]; do
    sleep 0.5; i=$((i + 1))
  done
  rg '^PROBE' "$PROFILE/server.out" || { echo "no probe output"; tail -5 "$PROFILE/server.out"; fail=1; }
  # "Got EOF on stdin" is the server noticing the console FIFO closing.
  if rg 'Error|error while running' "$PROFILE/factorio-current.log" | rg -v 'EOF on stdin'; then
    echo "FAILED"; fail=1
  else
    echo "ok (no script errors)"
  fi
else
  exec 3>&-
  echo "FAILED (server exited early)"; fail=1
  tail -20 "$PROFILE/factorio-current.log"
fi

exit "$fail"
