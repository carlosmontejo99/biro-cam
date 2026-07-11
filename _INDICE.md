# 📷 Cámara EMEET SmartCam S600 — Hub del tema

Hogar canónico de TODO lo relacionado con la cámara EMEET S600 en el Orange Pi 5 Max.
**Siempre trabajar este tema desde aquí:**
`~/Documentos/B.I.O.R./5. PROJECTS/CAMARA_EMEET_S600/` (código + AppImage + docs, todo junto).

---

## 📄 Documentación (subcarpeta `docs/`)

- **[Bitácora: Diagnóstico, Arreglos y Plan](./docs/Cámara%20EMEET%20S600%20—%20Diagnóstico,%20Arreglos%20y%20Plan%20(Bitácora).html)**
  — causa raíz del colapso (bug RGA >4GB), arreglos con rutas exactas, verificación,
  recuperación manual y roadmap. Abrir en navegador.
- **docs/EMEET S600 WEBCAM.pdf** — manual/ficha de la webcam.

## 💻 Código (en esta misma carpeta)

- `camara_s600.py` — **panel de control Qt/PySide6**. Lanza mpv (software, sin RGA) + IPC;
  sliders → `v4l2-ctl` en vivo. Icono en `assets/`.
- **AppImage:** `dist/Biro-Cam-aarch64.AppImage` (141 MB) — autocontenida, va a cualquier lado.
- Lanzador en el menú: `~/.local/share/applications/camara-s600.desktop` → "Cámara S600".
- Reconstruir: `bash packaging/build_appimage.sh` (entorno conda `camara-s600-build`).
- Re-registrar en el menú: `bash packaging/install_desktop.sh`.

## 🛠️ Otros archivos del sistema (fuera de B.I.O.R., no mover)

- `~/.local/share/applications/org.gnome.Snapshot.desktop` — **arreglo de la Cámara por
  defecto** (fuerza decode por software, evita el RGA).
- `~/.config/mpv/scripts/sistema_camara.lua` — app mpv mejorada (zoom/foco/timer/galería).
  Respaldo original: `sistema_camara.lua.bak-20260615`.
- `~/.local/share/applications/camara-4k.desktop` — visor mpv "EMEET 4K Carlos".

---

## ✅ Estado del roadmap

| Paso | Estado | Nota |
|------|--------|------|
| 0 · Diagnóstico + bitácora | ✅ | Bug RGA >4GB identificado y documentado |
| 1 · Arreglar Cámara por defecto | ✅ | Override `org.gnome.Snapshot.desktop` (decode software) |
| 1b · Probado en la práctica | ✅ | Foto tomada sin colgar el sistema (15-jun) |
| 2 · Mejorar app mpv | ✅ | `sistema_camara.lua` con controles nativos |
| 3 · Panel Qt/PySide6 | ✅ | `camara_s600.py` |
| 4 · Empaquetar AppImage aarch64 | ✅ | `dist/Biro-Cam-aarch64.AppImage` (141 MB), en el menú |
| 5 · Consolidar en 5. PROJECTS | ✅ | Todo en esta carpeta (16-jun) |
| 6 · Vídeo incrustado en la ventana | ✅ | mpv `--wid` (xcb/XWayland); cámara dentro del panel |
| 7 · Pulido | ✅ | Atajos, recordar ajustes (QSettings); pantalla completa retirada |
| 8 · Zoom + grabación + icono | ✅ | Zoom `video-zoom`; grabación `stream-record`→MP4; logo en barra GNOME; Fotos/Vídeos |
| 9 · Guardado robusto v2.2 | ✅ | Marca de tiempo corregida; FFmpeg se valida y conserva originales si falla |

## 🔜 Próximas funciones (pendientes, sin prisa)

- ✅ ~~🎙️ Grabación de audio personalizada~~ **HECHO (16-jun):** selector de micrófono en la UI
  (Sin audio / integrada OPi / K66 / cámara EMEET, por defecto el de la cámara). Al grabar,
  captura audio en paralelo con `ffmpeg -f pulse -i <source>` (independiente de la cámara que
  tiene mpv) y al detener mezcla vídeo+audio → MP4 (`h264_rkmpp` + `aac`). Verificado end-to-end.
- ⚙️ **Optimización de recursos:** el decode software a 4K es pesado; evaluar preview a menor
  resolución vs captura a 4K, hilos de mpv, uso de CPU/RAM.
- ✅ ~~Refresco al volver a la ventana~~ **HECHO (16-jun):** al reactivar la ventana se recarga
  el stream (`changeEvent`/`ActivationChange` → `loadfile`), quitando el retraso acumulado de la
  cámara en vivo mientras estuvo oculta. NO se hace durante la grabación.
- ✅ ~~Bloqueo de controles al grabar~~ **HECHO (16-jun):** resolución y micrófono se deshabilitan
  mientras se graba (evita romper la grabación por accidente).
- ✅ ~~Encuadre + efectos + layout~~ **HECHO (16-jun):** guía de composición (Tercios / Áurea)
  por OSD — solo visual, NO sale en foto (`screenshot video`) ni en vídeo. Efectos (B/N, Sepia,
  Vívido, Cálido, Negativo) por filtro `vf` de mpv → se ven en preview y foto, y se aplican a la
  grabación al convertir (`-vf` + `h264_rkmpp`). Layout: `addStretch` final para que no se
  repartan los controles al maximizar.
- ✨ **Otras pulidas futuras:** recordar tamaño/posición de ventana, afinar preset poca luz, etc.

## 🔑 Datos técnicos clave

- Cámara: EMEET SmartCam S600, USB UVC 4K, `328f:00ad`, `/dev/video0`.
- Formatos MJPEG: 4K30 · 1440p30 · **1080p60** · 720p60 · 480p30.
- Controles v4l2 nativos: zoom_absolute (0-100), brillo/contraste/saturación/gamma/ganancia,
  autofoco + focus_absolute, auto_exposure.
- ⚠️ **Nunca** decodificar esta cámara con el plugin GStreamer `rockchipmpp` (hardware) →
  dispara el bug RGA >4GB y cuelga el kernel. Siempre software (mpv `--hwdec=no` o
  `GST_PLUGIN_FEATURE_RANK=...:NONE`).
- 🎬 **Vídeo incrustado:** mpv se embebe en el QWidget con `--wid`. Requiere X11 → la app
  corre en **xcb/XWayland** (`QT_QPA_PLATFORM=xcb`). El plugin xcb necesita
  `libxcb-cursor.so.0` (no está en el sistema): se bundlea en la AppImage automáticamente.

## Corrección v2.2 · 21-jun-2026

- La marca de fecha/hora ya no inserta sus `:` dentro de la sintaxis de `drawtext`; usa un
  archivo de texto temporal, válido tanto en grabación normal como en modo seguridad.
- La conversión normal invoca FFmpeg con argumentos estructurados, comprueba el resultado y
  solo borra MKV/WAV después de crear un MP4 válido.
- El modo seguridad ahora detecta si FFmpeg falla y no anuncia falsamente «Vídeo guardado».
- Si el codificador RKMPP no puede inicializarse, ambos modos reintentan automáticamente con
  `libx264`/`libx265` por software; así el archivo se conserva sin depender del estado de MPP.

_Última actualización: 2026-06-21_

## Corrección v2.3 · 21-jun-2026

- Seguridad ya no alterna `/dev/video0` entre OpenCV, FFmpeg y mpv: el kernel fallaba al
  reservar memoria DMA UVC y terminaba perdiendo el dispositivo.
- La detección usa capturas internas de mpv y `stream-record`; una sola conexión a la cámara.
- Seguridad usa exactamente la resolución normal seleccionada.
- Preview con caché reducida, descarte de cuadros atrasados y FPS de exposición estable.

## Corrección v2.4 · 21-jun-2026

- Las capturas del detector alternan archivos y esperan confirmación de mpv antes de leer;
  elimina los JPEG truncados (`Premature end of JPEG file`).
- Seguridad lee la respuesta IPC real al iniciar `stream-record` y muestra su causa exacta.
- Limpia explícitamente el estado de una grabación normal antes de armar seguridad.
- En 4K espera hasta 4.5 segundos y confirma la propiedad antes de declarar un fallo.

## Corrección v2.5 · 21-jun-2026

- Un evento de movimiento mantiene un único clip mientras continúe la actividad; ya no crea
  grabaciones nuevas cada 10 segundos ni satura el conversor.
- Todo fallo detiene inmediatamente el FFmpeg de audio y elimina MKV/WAV incompletos.

## Corrección v2.6 · 10-jul-2026

- Grabación normal confirma que `stream-record` fue aceptado antes de mostrar REC.
- Las fuentes PulseAudio se validan y FFmpeg debe seguir activo antes de anunciar audio.
- Audio estéreo PCM 48 kHz con compensación del instante real de inicio; salida AAC 160 kbps.
- Salida atómica `*.procesando.mp4`: el destino final aparece únicamente tras pasar FFprobe.
- FFprobe exige vídeo, duración válida y audio cuando este fue solicitado.
- H.264 se etiqueta `avc1`; H.265 se etiqueta `hvc1` para mayor compatibilidad.
- Grabación normal y Seguridad comparten el mismo conversor validado.
- Seguridad espera la conversión anterior para no competir por el codificador RKMPP.
- Al cerrar durante una grabación, la aplicación finaliza y valida el clip antes de salir.
- Pipeline físico verificado en RK3588: H.264/H.265 RKMPP, AAC y decodificación RKMPP.
- Recuperación automática del flujo una vez si mpv conserva la ruta pero deja de escribir.
- Estado persistente visible en el panel lateral, botón y overlay nativo sobre el vídeo.

## Corrección v2.7 · 10-jul-2026

- Se corrige el comando de verificación de PulseAudio usando `pactl list short sources` (evitando el comando inexistente `get-source-info` que provocaba que la app siempre creyera que el micro no estaba disponible).
- Se implementa `clean_env()` para aislar todas las llamadas a subprocesos externos (`ffmpeg`, `ffprobe`, `pactl`, `v4l2-ctl`, `mpv`, `xdg-open`, `paplay`, `notify-send`) de las variables de entorno de la AppImage (como `LD_LIBRARY_PATH`), previniendo errores de carga de librerías incompatibles.
- Se restablece la redirección de errores a `subprocess.PIPE` en `ffmpeg` de audio para capturar y mostrar detalles en la barra de estado en caso de fallos.

