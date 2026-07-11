#!/bin/bash
# Integra "B.I.O.R. Cam" en el menú (instalación a nivel de usuario).
# Crea el lanzador .desktop apuntando al AppImage y registra el icono.
#
# Uso:   bash packaging/install_desktop.sh [ruta/al/AppImage]
# Por defecto usa dist/Biro-Cam-aarch64.AppImage del proyecto.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${0}")/.." && pwd)"
APPIMAGE="${1:-$PROJECT_DIR/dist/Biro-Cam-aarch64.AppImage}"

if [ ! -f "$APPIMAGE" ]; then
    echo "No se encontró el AppImage: $APPIMAGE" >&2
    exit 1
fi
APPIMAGE="$(readlink -f "$APPIMAGE")"
chmod +x "$APPIMAGE"

APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor"
mkdir -p "$APPS_DIR" "$ICON_DIR/256x256/apps" "$ICON_DIR/scalable/apps"

install -m 644 "$PROJECT_DIR/assets/biro-cam-256.png" "$ICON_DIR/256x256/apps/biro-cam.png"
install -m 644 "$PROJECT_DIR/assets/icon.svg" "$ICON_DIR/scalable/apps/biro-cam.svg"

cat > "$APPS_DIR/biro-cam.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=B.I.O.R. Cam
GenericName=Cámara USB
Comment=Panel de control de cámara USB (Webcam)
Exec="$APPIMAGE"
Icon=biro-cam
StartupWMClass=biro-cam
Categories=AudioVideo;Video;
Terminal=false
Keywords=camera;webcam;B.I.O.R.;EMEET;S600;4K;
EOF
chmod 644 "$APPS_DIR/biro-cam.desktop"

# El lanzador antiguo apuntaba al script de desarrollo; lo retiramos.
rm -f "$APPS_DIR/camara-s600-panel.desktop" "$APPS_DIR/camara-s600.desktop"

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$ICON_DIR" 2>/dev/null || true

echo "✔ B.I.O.R. Cam instalada en el menú."
echo "  Lanzador: $APPS_DIR/biro-cam.desktop"
echo "  AppImage: $APPIMAGE"
