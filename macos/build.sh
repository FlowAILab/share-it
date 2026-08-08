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
# copy only git-TRACKED python — never an untracked/shadow module from a dirty tree
for f in $(git -C .. ls-files '*.py' 2>/dev/null | grep -v '^tests/' || echo ../*.py); do
  cp "../$f" "$DEST/Contents/Resources/backend/$(basename "$f")" 2>/dev/null || cp "$f" "$DEST/Contents/Resources/backend/"
done
cp ../static/index.html "$DEST/Contents/Resources/backend/static/"
python3 make-icon.py   # always regenerate — a stale committed icns must never ship
cp AppIcon.icns "$DEST/Contents/Resources/AppIcon.icns"

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
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSDesktopFolderUsageDescription</key><string>Share-It copies or shares files your AI session created on your Desktop.</string>
  <key>NSDocumentsFolderUsageDescription</key><string>Share-It copies or shares files your AI session created in Documents.</string>
  <key>NSDownloadsFolderUsageDescription</key><string>Share-It copies or shares files your AI session created in Downloads.</string>
</dict></plist>
PLIST
codesign --force --deep --sign - "$DEST" 2>/dev/null || true
echo "done → $DEST   (⌥S toggles the panel)"
