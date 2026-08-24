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
  --collect-all cv2"

"$VENV/bin/pyinstaller" $COMMON_ARGS \
  --paths "$ROOT_DIR" \
  --windowed \
  --name "VP Stitch" \
  --add-data "configs:configs" \
  packaging/gui_entry.py

"$VENV/bin/pyinstaller" $COMMON_ARGS \
  --paths "$ROOT_DIR" \
  --console \
  --name vpstitch-cli \
  packaging/cli_entry.py

cp "dist/vpstitch-cli/vpstitch-cli" "dist/VP Stitch.app/Contents/MacOS/vpstitch-cli"
chmod +x "dist/VP Stitch.app/Contents/MacOS/vpstitch-cli"
codesign --deep --force --sign - "dist/VP Stitch.app"

ditto -c -k --sequesterRsrc --keepParent \
  "dist/VP Stitch.app" \
  "dist/VP-Stitch-macOS-$(uname -m).zip"

if command -v hdiutil >/dev/null 2>&1; then
  hdiutil create -volname "VP Stitch" -srcfolder "dist/VP Stitch.app" \
    -ov -format UDZO "dist/VP-Stitch-macOS-$(uname -m).dmg" >/dev/null
fi

echo "Built: dist/VP Stitch.app"
echo "Archive: dist/VP-Stitch-macOS-$(uname -m).zip"
