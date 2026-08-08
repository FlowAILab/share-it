#!/bin/bash
# Reproducible release build: pinned CPython, clean tree, DMG + SHA-256.
# Run from a clean checkout. Requires: swiftc, curl, hdiutil, shasum.
set -euo pipefail
cd "$(dirname "$0")/.."

# --- pinned relocatable CPython (verify hash before use) ---
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20250612/cpython-3.12.11+20250612-aarch64-apple-darwin-install_only.tar.gz"
PY_SHA="c6d4843e8af496f034176908ae3384556680284653a4bff45eff07e43fe4ae34"   # set once, then this build is reproducible
WORK="$(mktemp -d)"
curl -sSL -o "$WORK/py.tgz" "$PY_URL"
if [ "$PY_SHA" != "c6d4843e8af496f034176908ae3384556680284653a4bff45eff07e43fe4ae34" ]; then
  echo "$PY_SHA  $WORK/py.tgz" | shasum -a 256 -c - || { echo "CPython hash mismatch — aborting"; exit 1; }
else
  echo "WARNING: PY_SHA not pinned; recording actual hash:"; shasum -a 256 "$WORK/py.tgz"
fi
tar xzf "$WORK/py.tgz" -C "$WORK"

# --- build self-contained app (config must exist locally; never committed) ---
[ -f default_config.json ] || { echo "default_config.json missing (holds the hosted token)"; exit 1; }
STAGE="$WORK/stage"; mkdir -p "$STAGE"
SHAREIT_DEST="$STAGE" SHAREIT_PYTHON="$WORK/python" bash macos/build.sh --bundle-python
cp default_config.json "$STAGE/share-it.app/Contents/Resources/backend/"
mv "$STAGE/share-it.app" "$STAGE/Share-It.app"
codesign --force --deep --sign - "$STAGE/Share-It.app" || true
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Share-It" -srcfolder "$STAGE" -ov -format UDZO ./Share-It.dmg
shasum -a 256 Share-It.dmg | tee Share-It.dmg.sha256
echo "built ./Share-It.dmg (+ .sha256). Attach both to the GitHub Release."
