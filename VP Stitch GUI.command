#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -x ".venv/bin/vpstitch-gui" ]; then
  echo "VP Stitch GUI is not installed in .venv."
  echo "Run: python3.12 -m venv .venv"
  echo "Then: .venv/bin/python -m pip install -e ."
  printf "Press Return to close..."
  read -r _
  exit 1
fi

exec ".venv/bin/vpstitch-gui"
