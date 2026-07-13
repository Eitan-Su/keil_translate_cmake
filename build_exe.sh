#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"

echo "[1/4] Checking Python..."
"$PYTHON_BIN" --version

echo "[2/4] Installing build dependencies..."
"$PYTHON_BIN" -m pip install -r requirements.txt pyinstaller

echo "[3/4] Cleaning previous executable and building KeilForge.exe..."
rm -rf build
rm -f dist/KeilForge.exe

"$PYTHON_BIN" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onefile \
  --add-data "assets;assets" \
  --icon "assets/keilforge-app-icon.ico" \
  --version-file "assets/keilforge-version.txt" \
  --name KeilForge \
  main.py

cp -f config.ini dist/config.ini

echo "[4/4] Build complete."
echo "Output: $SCRIPT_DIR/dist/KeilForge.exe"
