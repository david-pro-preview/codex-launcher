#!/bin/bash
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$SOURCE_DIR/dist"
BUILD_DIR="$SOURCE_DIR/.build"
APP_BUNDLE="$OUTPUT_DIR/Codex Launcher.app"
if [ -d /Library/Developer/CommandLineTools/SDKs ]; then
  export DEVELOPER_DIR=/Library/Developer/CommandLineTools
fi
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  printf 'This build targets Apple Silicon Macs (macOS 14+).\n' >&2
  exit 1
fi
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'
xcrun --find swiftc >/dev/null
mkdir -p "$BUILD_DIR/AppIcon.iconset" "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
xcrun swiftc -swift-version 5 -O -module-name CodexLauncher \
  -target arm64-apple-macosx14.0 -module-cache-path "$BUILD_DIR/module-cache" \
  -framework AppKit -framework SwiftUI -framework Combine \
  "$SOURCE_DIR/Launcher.swift" -o "$APP_BUNDLE/Contents/MacOS/Codex Launcher"
for SIZE in 16 32 128 256 512; do
  sips -z "$SIZE" "$SIZE" "$SOURCE_DIR/Assets/AppIcon.png" --out "$BUILD_DIR/AppIcon.iconset/icon_${SIZE}x${SIZE}.png" >/dev/null
  DOUBLE=$((SIZE * 2))
  sips -z "$DOUBLE" "$DOUBLE" "$SOURCE_DIR/Assets/AppIcon.png" --out "$BUILD_DIR/AppIcon.iconset/icon_${SIZE}x${SIZE}@2x.png" >/dev/null
done
iconutil -c icns "$BUILD_DIR/AppIcon.iconset" -o "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
cp "$SOURCE_DIR/Assets/AppIcon.png" "$APP_BUNDLE/Contents/Resources/AppIcon.png"
cp "$SOURCE_DIR/Assets/MenuBarTemplate.png" "$APP_BUNDLE/Contents/Resources/MenuBarTemplate.png"
cp "$SOURCE_DIR/Assets/MenuBarTemplate@2x.png" "$APP_BUNDLE/Contents/Resources/MenuBarTemplate@2x.png"
cp "$SOURCE_DIR/account_manager.py" "$APP_BUNDLE/Contents/Resources/account_manager.py"
"$PYTHON_BIN" - "$APP_BUNDLE" <<'PY'
from pathlib import Path
import plistlib,sys
p=Path(sys.argv[1])/'Contents/Info.plist'
data={
    'CFBundleDevelopmentRegion':'zh_CN', 'CFBundleDisplayName':'Codex Launcher',
    'CFBundleName':'Codex Launcher', 'CFBundleExecutable':'Codex Launcher',
    'CFBundleIdentifier':'local.kuner.codex-launcher', 'CFBundlePackageType':'APPL',
    'CFBundleShortVersionString':'1.1.0', 'CFBundleVersion':'2',
    'CFBundleIconFile':'AppIcon', 'LSMinimumSystemVersion':'14.0',
    'LSUIElement':True, 'NSHighResolutionCapable':True,
    'NSPrincipalClass':'NSApplication', 'LSApplicationCategoryType':'public.app-category.utilities',
}
with p.open('wb') as f:plistlib.dump(data,f)
PY
chmod 755 "$APP_BUNDLE/Contents/MacOS/Codex Launcher"
codesign --force --sign - "$APP_BUNDLE"
printf 'Built %s\n' "$APP_BUNDLE"
