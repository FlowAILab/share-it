#!/bin/bash
# Reproducible release build: pinned CPython, clean tree, styled DMG + SHA-256.
# Run from a clean checkout. Requires: swiftc, curl, hdiutil, shasum, osascript.
set -euo pipefail
cd "$(dirname "$0")/.."

# --- pinned relocatable CPython (hash always verified) ---
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250612/cpython-3.12.11+20250612-aarch64-apple-darwin-install_only.tar.gz"
PY_SHA="c6d4843e8af496f034176908ae3384556680284653a4bff45eff07e43fe4ae34"
WORK="$(mktemp -d)"
curl -sSL -o "$WORK/py.tgz" "$PY_URL"
echo "$PY_SHA  $WORK/py.tgz" | shasum -a 256 -c - || { echo "CPython hash mismatch — aborting"; exit 1; }
tar xzf "$WORK/py.tgz" -C "$WORK"

# --- build self-contained app (config must exist locally; never committed) ---
[ -f default_config.json ] || { echo "default_config.json missing (holds the hosted token)"; exit 1; }
STAGE="$WORK/stage"; mkdir -p "$STAGE"
SHAREIT_DEST="$STAGE" SHAREIT_PYTHON="$WORK/python" bash macos/build.sh --bundle-python
cp default_config.json "$STAGE/share-it.app/Contents/Resources/backend/"
mv "$STAGE/share-it.app" "$STAGE/Share-It.app"
codesign --force --deep --sign - "$STAGE/Share-It.app" || true
ln -s /Applications "$STAGE/Applications"

# --- installer window: background that says what to do + fixed icon layout ---
swift macos/make-dmg-bg.swift "$WORK/dmg-bg.png"
mkdir -p "$STAGE/.background"
cp "$WORK/dmg-bg.png" "$STAGE/.background/bg.png"

rm -f "$WORK/rw.dmg"
# a stale "Share-It" volume would make Finder style the wrong disk — clear it
while [ -d "/Volumes/Share-It" ]; do
  hdiutil detach "/Volumes/Share-It" -force || break
done
hdiutil create -volname "Share-It" -srcfolder "$STAGE" -ov -format UDRW "$WORK/rw.dmg"
MNT="$(hdiutil attach -readwrite -noverify -noautoopen "$WORK/rw.dmg" | awk -F'\t' '/\/Volumes\//{print $NF}')"
[ -d "$MNT" ] || { echo "mount failed"; exit 1; }
trap 'hdiutil detach "$MNT" -force >/dev/null 2>&1 || true' EXIT
osascript <<OSA
tell application "Finder"
  tell disk "Share-It"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {200, 140, 840, 580}
    set vopts to the icon view options of container window
    set arrangement of vopts to not arranged
    set icon size of vopts to 112
    set background picture of vopts to file ".background:bg.png"
    set position of item "Share-It.app" of container window to {180, 200}
    set position of item "Applications" of container window to {460, 200}
    close
    open
    delay 1
    close
  end tell
end tell
OSA
sync
hdiutil detach "$MNT"
rm -f ./Share-It.dmg
hdiutil convert "$WORK/rw.dmg" -format UDZO -o ./Share-It.dmg
shasum -a 256 Share-It.dmg | tee Share-It.dmg.sha256
echo "built ./Share-It.dmg (+ .sha256). Attach both to the GitHub Release."
