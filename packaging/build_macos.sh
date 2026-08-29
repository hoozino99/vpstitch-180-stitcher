#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

PYTHON_BIN=${PYTHON_BIN:-python3.12}
BUILD_ROOT=${BUILD_ROOT:-"$ROOT_DIR/.build/macos"}
VENV="$BUILD_ROOT/venv"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.12 is required. Install it with Homebrew: brew install python@3.12" >&2
  exit 1
fi

mkdir -p "$BUILD_ROOT"
AX_SHIM="$BUILD_ROOT/libvpstitch_macos_ax.dylib"
METAL_BACKEND="$BUILD_ROOT/libvpstitch_metal.dylib"
clang -dynamiclib -O2 -arch "$(uname -m)" \
  -x objective-c -framework AppKit \
  -o "$AX_SHIM" packaging/macos_ax_shim.c
clang++ -dynamiclib -O3 -std=c++17 -fobjc-arc -arch "$(uname -m)" \
  -framework Foundation -framework Metal -framework AVFoundation \
  -framework CoreVideo -framework CoreMedia -framework VideoToolbox \
  -framework IOSurface \
  -o "$METAL_BACKEND" packaging/macos_metal_backend.mm
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV/bin/python" -m pip install -e . pyinstaller

rm -rf build dist

COMMON_ARGS="\
  --noconfirm \
  --clean \
  --onedir \
  --osx-bundle-identifier com.vplab.vpstitch \
  --collect-all imageio_ffmpeg \
  --collect-all PyOpenColorIO \
  --collect-all cv2 \
  --add-binary "$METAL_BACKEND:.""

"$VENV/bin/pyinstaller" $COMMON_ARGS \
  --paths "$ROOT_DIR" \
  --windowed \
  --name "VP Stitch" \
  --icon "assets/VP-Stitch.icns" \
  --add-binary "$AX_SHIM:." \
  --add-data "configs:configs" \
  packaging/gui_entry.py

"$VENV/bin/pyinstaller" $COMMON_ARGS \
  --paths "$ROOT_DIR" \
  --console \
  --name vpstitch-cli \
  packaging/cli_entry.py

cp "dist/vpstitch-cli/vpstitch-cli" "dist/VP Stitch.app/Contents/MacOS/vpstitch-cli"
chmod +x "dist/VP Stitch.app/Contents/MacOS/vpstitch-cli"

PLIST="dist/VP Stitch.app/Contents/Info.plist"
set_plist_string() {
  key=$1
  value=$2
  if /usr/libexec/PlistBuddy -c "Print :$key" "$PLIST" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c "Set :$key $value" "$PLIST"
  else
    /usr/libexec/PlistBuddy -c "Add :$key string $value" "$PLIST"
  fi
}
set_plist_string NSDownloadsFolderUsageDescription \
  "VP Stitch needs source plate access for previews, stitching, and final renders."
set_plist_string NSDocumentsFolderUsageDescription \
  "VP Stitch needs project, cache, source plate, and render access."
set_plist_string NSDesktopFolderUsageDescription \
  "VP Stitch needs access when a project, source plate, or render is stored on the Desktop."
set_plist_string NSRemovableVolumesUsageDescription \
  "VP Stitch needs source plate and render access on external production drives."
set_plist_string NSNetworkVolumesUsageDescription \
  "VP Stitch needs source plate and render access on network production storage."

# A stable Developer ID identity keeps macOS Files & Folders approval attached
# to the app across updates. CI and local development may still fall back to
# ad-hoc signing, which macOS can legitimately treat as a new build.
CODESIGN_IDENTITY=${VPSTITCH_CODESIGN_IDENTITY:--}
if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
  # Hardened runtime library validation requires a real shared Team ID. Keep
  # ad-hoc development builds unhardened so bundled Python/Qt libraries load.
  codesign --deep --force --sign - "dist/VP Stitch.app"
  echo "Note: ad-hoc signing; set VPSTITCH_CODESIGN_IDENTITY for persistent macOS app identity."
else
  codesign --deep --force --options runtime --sign "$CODESIGN_IDENTITY" \
    "dist/VP Stitch.app"
fi

ditto -c -k --sequesterRsrc --keepParent \
  "dist/VP Stitch.app" \
  "dist/VP-Stitch-macOS-$(uname -m).zip"

if command -v hdiutil >/dev/null 2>&1; then
  hdiutil create -volname "VP Stitch" -srcfolder "dist/VP Stitch.app" \
    -ov -format UDZO "dist/VP-Stitch-macOS-$(uname -m).dmg" >/dev/null
fi

echo "Built: dist/VP Stitch.app"
echo "Archive: dist/VP-Stitch-macOS-$(uname -m).zip"
