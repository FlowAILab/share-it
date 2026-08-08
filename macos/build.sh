#!/bin/bash
# Build Share-It.app. With --bundle-python it embeds a relocatable CPython so the
# packaged app needs no system Python (used for the distributable DMG).
set -euo pipefail
cd "$(dirname "$0")"
DEST="${SHAREIT_DEST:-$HOME/Applications}/share-it.app"
BUNDLE_PY=""
[ "${1:-}" = "--bundle-python" ] && BUNDLE_PY=1

command -v swiftc >/dev/null || { echo "swiftc not found — install Xcode Command Line Tools"; exit 1; }

echo "compiling…"
swiftc -O main.swift -o share-it-bin \
  -framework Cocoa -framework WebKit -framework Carbon -framework Quartz

echo "assembling ${DEST}…"
rm -rf "$DEST"
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources/backend/static" "$DEST/Contents/Resources/backend/tests"
mv share-it-bin "$DEST/Contents/MacOS/share-it"
cp ../*.py "$DEST/Contents/Resources/backend/"   # every backend module — never hand-list
cp ../static/index.html "$DEST/Contents/Resources/backend/static/"

if [ -n "$BUNDLE_PY" ]; then
  PYROOT="${SHAREIT_PYTHON:-/tmp/shareit-py/python}"
  [ -x "$PYROOT/bin/python3" ] || { echo "bundled python not at $PYROOT - set SHAREIT_PYTHON"; exit 1; }
  echo "embedding standalone python from $PYROOT"
  cp -R "$PYROOT" "$DEST/Contents/Resources/python"
fi

cat > "$DEST/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Share-It</string>
  <key>CFBundleIdentifier</key><string>ai.flow.shareit</string>
  <key>CFBundleVersion</key><string>2.0.0</string>
  <key>CFBundleShortVersionString</key><string>2.0.0</string>
  <key>CFBundleExecutable</key><string>share-it</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --deep --sign - "$DEST" 2>/dev/null || true
echo "done → $DEST   (⌥S toggles the panel)"
