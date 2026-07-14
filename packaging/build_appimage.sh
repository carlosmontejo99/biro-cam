#!/bin/bash
# Construye Biro-Cam-aarch64.AppImage desde el entorno conda 'biro-cam-build'.
#
# Requisitos (ya hechos):
#   mamba create -n biro-cam-build python=3.12
#   <env>/bin/pip install PySide6
#   pip install conda-pack   (en base)
set -euo pipefail

if [ "$HOME" = "/home/carlos/snap/antigravity/6" ] || { [ ! -d "$HOME/miniforge3" ] && [ -d "/home/carlos/miniforge3" ]; }; then
    REAL_HOME="/home/carlos"
else
    REAL_HOME="$HOME"
fi

PROJECT_DIR="$(cd "$(dirname "${0}")/.." && pwd)"
PACK_DIR="$PROJECT_DIR/packaging"
ENV_NAME="biro-cam-build"
APPDIR="$PROJECT_DIR/build/BiroCam.AppDir"
APPIMAGETOOL="${APPIMAGETOOL:-$REAL_HOME/.cache/the_curator/appimagetool-aarch64.AppImage}"
OUTPUT="$PROJECT_DIR/dist/Biro-Cam-aarch64.AppImage"
CONDA_PACK="${CONDA_PACK:-$REAL_HOME/miniforge3/bin/conda-pack}"

for cmd in tar install find; do
    command -v "$cmd" >/dev/null || {
        echo "ERROR: falta la herramienta requerida: $cmd" >&2
        exit 1
    }
done
[ -x "$CONDA_PACK" ] || {
    echo "ERROR: conda-pack no es ejecutable: $CONDA_PACK" >&2
    exit 1
}
[ -f "$APPIMAGETOOL" ] || {
    echo "ERROR: no se encontró appimagetool ARM64: $APPIMAGETOOL" >&2
    exit 1
}

echo ">> Limpiando AppDir previo…"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr" "$PROJECT_DIR/dist"

echo ">> Empaquetando el entorno conda con conda-pack…"
TARBALL="$PROJECT_DIR/build/env.tar.gz"

# Si conda no está en el PATH, usamos la ruta física del entorno si existe
ENV_PATH="$REAL_HOME/miniforge3/envs/$ENV_NAME"
if [ -d "$ENV_PATH" ]; then
    "$CONDA_PACK" -p "$ENV_PATH" -o "$TARBALL" --force --ignore-missing-files
else
    "$CONDA_PACK" -n "$ENV_NAME" -o "$TARBALL" --force --ignore-missing-files
fi

tar -xzf "$TARBALL" -C "$APPDIR/usr"
rm -f "$TARBALL"

echo ">> conda-unpack (corrige rutas absolutas)…"
"$APPDIR/usr/bin/conda-unpack" || true

echo ">> Copiando la app, AppRun, .desktop e iconos…"
install -Dm644 "$PROJECT_DIR/camara_s600.py" "$APPDIR/usr/share/biro-cam/camara_s600.py"
cp -r "$PROJECT_DIR/assets" "$APPDIR/usr/share/biro-cam/assets"
install -m 755 "$PACK_DIR/AppRun" "$APPDIR/AppRun"
install -m 644 "$PACK_DIR/biro-cam.desktop" "$APPDIR/biro-cam.desktop"
install -Dm644 "$PACK_DIR/biro-cam.desktop" "$APPDIR/usr/share/applications/biro-cam.desktop"
install -Dm644 "$PACK_DIR/org.biro.BiroCam.appdata.xml" "$APPDIR/usr/share/metainfo/org.biro.BiroCam.appdata.xml"
for size in 48 64 128 256; do
    install -Dm644 "$PROJECT_DIR/assets/biro-cam-${size}.png" \
        "$APPDIR/usr/share/icons/hicolor/${size}x$size/apps/biro-cam.png"
done
install -Dm644 "$PROJECT_DIR/assets/icon.svg" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/biro-cam.svg"
cp "$PROJECT_DIR/assets/biro-cam-256.png" "$APPDIR/biro-cam.png"
cp "$PROJECT_DIR/assets/icon.svg" "$APPDIR/biro-cam.svg" 2>/dev/null || true
ln -sf biro-cam.png "$APPDIR/.DirIcon"

echo ">> Podando Qt (la app solo usa Core/Gui/Widgets)…"
SP="$APPDIR/usr/lib/python3.12/site-packages/PySide6"
# Módulos Qt no usados: bindings (PySide6/QtX.*) y libs (Qt/lib/libQt6X.*)
UNUSED="WebEngineCore WebEngineWidgets WebEngineQuick WebEngineQuickDelegatesQml \
WebChannel WebChannelQuick WebSockets WebView \
Quick Quick3D Quick3DRuntimeRender Quick3DUtils Quick3DAssetImport Quick3DAssetUtils \
Quick3DEffects Quick3DHelpers Quick3DParticles Quick3DPhysics Quick3DXr Quick3DSpatialAudio \
QuickControls2 QuickControls2Impl QuickControls2Basic QuickControls2BasicStyleImpl \
QuickControls2Fusion QuickControls2FusionStyleImpl QuickControls2Imagine \
QuickControls2ImagineStyleImpl QuickControls2Material QuickControls2MaterialStyleImpl \
QuickControls2Universal QuickControls2UniversalStyleImpl QuickControls2Windows \
QuickWidgets QuickTest QuickShapes QuickLayouts QuickDialogs2 QuickDialogs2Utils \
QuickDialogs2QuickImpl QuickTemplates2 QuickParticles QuickEffects QuickTimeline \
QuickVectorImage QuickVectorImageGenerator QuickControlsTestUtilsPrivate \
Qml QmlModels QmlWorkerScript QmlMeta QmlLocalStorage QmlXmlListModel QmlCompiler \
QmlCore QmlIntegration QmlNetwork QmlToolingSettings QmlTypeRegistrar QmlLint QmlDom QmlDebug \
3DCore 3DRender 3DInput 3DLogic 3DAnimation 3DExtras 3DQuick 3DQuickScene2D \
3DQuickRender 3DQuickInput 3DQuickExtras 3DQuickAnimation 3DQuickPhysics 3DQuickRender \
Charts ChartsQml DataVisualization DataVisualizationQml Graphs GraphsWidgets \
Multimedia MultimediaWidgets MultimediaQuick SpatialAudio \
Designer DesignerComponents Pdf PdfWidgets PdfQuick \
Sql Test Help UiTools Bluetooth Nfc SerialPort SerialBus Sensors SensorsQuick \
Positioning PositioningQuick Location RemoteObjects RemoteObjectsQml Scxml StateMachine \
TextToSpeech Concurrent ShaderTools Quick3DGlslParser"
for m in $UNUSED; do
    rm -f "$SP/Qt$m.abi3.so" "$SP/Qt/lib/libQt6$m.so"* 2>/dev/null || true
done
# Recursos/QML/translations/ejemplos pesados que no usamos
rm -rf "$SP/Qt/resources" "$SP/Qt/qml" "$SP/Qt/translations" "$SP/examples" \
       "$SP/Qt/libexec/QtWebEngineProcess" "$SP/scripts" 2>/dev/null || true
# Plugins no usados (mantener: platforms, imageformats, iconengines, styles, platformthemes)
for p in sqldrivers multimedia webview position sensors texttospeech \
         renderers geometryloaders sceneparsers assetimporters qmltooling; do
    rm -rf "$SP/Qt/plugins/$p" 2>/dev/null || true
done
# av* solo lo usa Multimedia/WebEngine (ya removidos)
rm -f "$SP/Qt/lib/"libav*.so* "$SP/Qt/lib/"libsw*.so* 2>/dev/null || true

echo ">> Bundleando libxcb-cursor (la necesita el plugin xcb para incrustar el vídeo bajo XWayland)…"
QTLIB="$SP/Qt/lib"
if [ ! -e "$QTLIB/libxcb-cursor.so.0" ]; then
    XCBCUR=$(find "$REAL_HOME/miniforge3/pkgs" -path \
        '*/xcb-util-cursor-*/lib/libxcb-cursor.so.0' -print -quit 2>/dev/null || true)
    if [ -n "$XCBCUR" ]; then cp "$XCBCUR"* "$QTLIB/" && echo "   + libxcb-cursor copiada"
    else echo "   ! AVISO: no se encontró libxcb-cursor en pkgs de conda"; fi
fi

echo ">> Adelgazando el bundle (cachés, tests, headers)…"
find "$APPDIR/usr" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$APPDIR/usr" -type d -name "tests" -path "*/site-packages/*" -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$APPDIR/usr/share/man" "$APPDIR/usr/include" 2>/dev/null || true

echo ">> Tamaño del AppDir tras podar:"; du -sh "$APPDIR" | cut -f1

echo ">> Construyendo el AppImage con appimagetool…"
if [ -f "$OUTPUT" ]; then
    for p in $(fuser "$OUTPUT" 2>/dev/null); do kill "$p" 2>/dev/null; done
    sleep 1
fi
chmod +x "$APPIMAGETOOL" 2>/dev/null || true
ARCH=aarch64 NO_APPSTREAM=1 "$APPIMAGETOOL" "$APPDIR" "$OUTPUT" \
    || ARCH=aarch64 NO_APPSTREAM=1 "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUTPUT"

echo ">> Listo: $OUTPUT"
ls -lh "$OUTPUT"
