#!/usr/bin/env bash
#
# Construye el ejecutable de macOS (.app y binario) con PyInstaller.
#
# IMPORTANTE: PyInstaller NO compila de forma cruzada. Este script DEBE
# ejecutarse en un Mac (no en Windows). Genera:
#   dist/ImageAnalyzer.app   -> aplicación de macOS (doble clic)
#   dist/ImageAnalyzer       -> binario equivalente
#
# Uso:
#   chmod +x build_macos.sh
#   ./build_macos.sh
#
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "==> Creando entorno virtual (.venv) si no existe…"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
source .venv/bin/activate

echo "==> Instalando dependencias…"
pip install --upgrade pip >/dev/null
pip install -r requirements.txt >/dev/null

echo "==> Compilando con PyInstaller…"
pyinstaller --noconfirm --windowed --onefile --name ImageAnalyzer \
  --icon assets/logo.icns \
  --add-data "assets/logo.png:assets" \
  main.py

echo
echo "==> Listo:"
echo "    dist/ImageAnalyzer.app"
echo "    dist/ImageAnalyzer"
echo
echo "Si Gatekeeper bloquea la app por no estar firmada, ábrela con clic"
echo "derecho -> Abrir, o ejecuta:"
echo "    xattr -dr com.apple.quarantine dist/ImageAnalyzer.app"
