#!/bin/bash
# Build ZoomGHL.app and ZoomGHL.dmg on macOS.
#
# Usage:  bash build_macos.sh
# Output: dist/ZoomGHL.app  +  dist/ZoomGHL.dmg
set -euo pipefail

cd "$(dirname "$0")"

# 1) Ensure deps are installed in a build venv.
if [ ! -d ".venv" ]; then
  python3.12 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements-build.txt --quiet

# 2) Render the icons (.icns, .ico, .png).
python build_icon.py

# 3) Clean previous build/dist.
rm -rf build dist

# 4) Build the .app via PyInstaller.
pyinstaller zghl.spec --noconfirm --clean

if [ ! -d "dist/ZoomGHL.app" ]; then
  echo "❌ ZoomGHL.app was not produced — check PyInstaller output above."
  exit 1
fi

# 5) Bake a DMG. Use a simple read-only DMG (no fancy background).
DMG_PATH="dist/ZoomGHL.dmg"
rm -f "$DMG_PATH"

DMG_STAGING="dist/dmg-stage"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
cp -R "dist/ZoomGHL.app" "$DMG_STAGING/"
ln -s /Applications "$DMG_STAGING/Applications"

hdiutil create \
  -volname "ZoomGHL" \
  -srcfolder "$DMG_STAGING" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

rm -rf "$DMG_STAGING"

echo ""
echo "✅ Built:"
echo "   - dist/ZoomGHL.app"
echo "   - dist/ZoomGHL.dmg"
echo ""
echo "Test the .app:"
echo "   open dist/ZoomGHL.app"
echo ""
echo "Distribute the .dmg to employees."
