#!/bin/sh
# Build the mod portal zip: build/<name>_<version>.zip, holding one folder of the
# same name. Upload it at https://mods.factorio.com (new mod, or the mod's
# Versions tab for an update).
#   tools/release.sh
# Bump "version" in info.json and add a changelog.txt section for it first. The
# portal and the in-game mod browser both show changelog.txt, so this refuses to
# build a version the changelog doesn't mention.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
NAME=$(jq -r .name "$ROOT/info.json")
VERSION=$(jq -r .version "$ROOT/info.json")
DIR="${NAME}_${VERSION}"
OUT="$ROOT/build/release"

if ! rg -qx "Version: $VERSION" "$ROOT/changelog.txt"; then
  echo "changelog.txt has no 'Version: $VERSION' section." >&2
  exit 1
fi
if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  echo "warning: uncommitted changes; the zip may not match any commit." >&2
fi

uv run "$ROOT/tools/pack-art.py"

rm -rf "$OUT"
mkdir -p "$OUT/$DIR"
# Only what the game loads, plus the licence. art/, tools/, scenarios/ and
# reference/ are dev-only, and the lab scenario's commands would show up in
# players' scenario list.
cp -R "$ROOT/info.json" "$ROOT/data.lua" "$ROOT/locale" "$ROOT/graphics" \
  "$ROOT/thumbnail.png" "$ROOT/changelog.txt" "$ROOT/LICENSE" "$OUT/$DIR/"
# -X leaves out the Unix uid/gid and extra timestamp fields; the game has no use
# for them.
(cd "$OUT" && zip -rqX "$OUT/$DIR.zip" "$DIR" -x '*.DS_Store')

echo "$OUT/$DIR.zip ($(du -h "$OUT/$DIR.zip" | cut -f1))"
