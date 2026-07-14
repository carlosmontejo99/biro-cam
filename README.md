# B.I.O.R. Cam v2.9 — Cámara 4K, seguridad NPU y Kinect para RK3588

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21304061.svg)](https://doi.org/10.5281/zenodo.21304061)


Aplicación de control y grabación para cámaras UVC en Orange Pi 5 Max (RK3588),
diseñada para la EMEET SmartCam S600, con modo Seguridad mediante YOLOv5 acelerado
por NPU y soporte opcional para Xbox 360 Kinect.

> 📍 **Hogar único del proyecto:**
> `~/Documentos/B.I.O.R./5. PROJECTS/CAMARA_EMEET_S600/`
> El índice maestro está en `_INDICE.md`; los documentos en `docs/`.

## Ejecutar
- **Usuario final:** desde el menú → **"B.I.O.R. Cam"** (lanza la AppImage), o doble clic a
  `dist/Biro-Cam-aarch64.AppImage`.
- **Desarrollo:** `/home/carlos/miniforge3/envs/biro-cam-build/bin/python camara_s600.py`

## Características
- 6 guías de encuadre: Tercios, Proporción áurea, Espiral (Fibonacci), Cruz centrada, Diagonales
- Grabación con bitrate ajustable (1-50 Mbps), duración límite, códec H.264/H.265
- Inicio confirmado de vídeo y micrófono: la interfaz no anuncia audio si la fuente falla
- MP4 atómico: se escribe como archivo temporal y solo aparece al superar FFprobe
- H.264 etiquetado `avc1` y H.265 `hvc1`, ambos reproducibles por RKMPP/MPV
- Sincronización de audio según el instante real de apertura de PulseAudio
- Cierre seguro: si se intenta salir grabando, finaliza y valida el clip antes de cerrar
- Marca de agua de fecha/hora opcional en el vídeo (quemada con `drawtext`)
- Temporizador de foto (3s / 10s) con cuenta atrás
- Sonido de obturador configurable (generado como WAV, reproducido con `paplay`)
- Modo seguridad: detección de movimiento por OpenCV, grabación automática con bitrate/configuración independiente
- Detección de personas con YOLOv5 en la NPU RK3588 (~43 ms por inferencia), con fallback CPU
- Soporte opcional para Xbox 360 Kinect: RGB, IR, profundidad, inclinación y LED; solo aparece si está conectado
- Recuperación de conversiones interrumpidas y rechazo de MP4 truncados por comparación de duración
- Overlay HUD profesional en modo seguridad: panel semi-transparente 520×340px con fuentes grandes, barra de progreso, estado y parámetros
- Efectos de imagen (B/N, sepia, vívido, cálido, negativo)
- Espejo, zoom digital, auto-exposición/auto-foco/auto-blancos
- Todos los ajustes se persisten entre sesiones (QSettings)
- Atajos de teclado: `Espacio`/`S` foto, `R` grabar, `F` autofoco, `M` espejo,
  `G` galería, `V` vídeos, `T` timestamp, `0` reset, `+`/`-` zoom

## Arquitectura
- `camara_s600.py` — panel Qt/PySide6. Lanza mpv con `--hwdec=no` (decode software →
  **nunca toca el RGA**, evita el bug >4GB que cuelga el kernel) y se comunica por socket
  IPC (`/tmp/mpv-biro-cam.sock`) para foto/grabar/resolución. Los ajustes de imagen
  (brillo, contraste, saturación, gamma, ganancia, zoom, foco, exposición) van por
  `v4l2-ctl` en vivo.

## Empaquetado (AppImage aarch64)
- Construir: `bash packaging/build_appimage.sh` → `dist/Biro-Cam-aarch64.AppImage` (~147 MB).
- Entorno de build: conda `biro-cam-build` (Python 3.12, PySide6 6.11.1 y RKNNLite 2.3.2).
- El modelo `assets/yolov5s-640-640-rk3588.rknn` va incluido en la AppImage.

## Dependencias en runtime
- `mpv`, `v4l2-ctl` (v4l-utils) del sistema. PySide6/Python van bundleados en la AppImage.

## Vídeo incrustado
La cámara se muestra DENTRO de la ventana (mpv embebido con `--wid`). Requiere X11, así que
la app corre bajo **xcb/XWayland** (`QT_QPA_PLATFORM=xcb`, forzado en AppRun y `main()`). El
plugin xcb necesita `libxcb-cursor.so.0`, que se bundlea en la AppImage automáticamente.

## Guías de bitrate (H.264 / H.265)

| Calidad | 1080p | 4K |
|---------|-------|----|
| Buena | 5-10 Mbps | 15-25 Mbps |
| Excelente | 10-20 Mbps | 30-50 Mbps |
| Sin pérdida apreciable | 20-40 Mbps | 50 Mbps+ |

## Flujo de grabación validado

1. `mpv stream-record` conserva el MJPEG original de la cámara en un MKV temporal.
2. Si hay micrófono seleccionado, FFmpeg confirma primero que la fuente PulseAudio existe.
3. Al detener, el audio se cierra con SIGINT para conservar una cabecera WAV válida.
4. FFmpeg convierte por RKMPP a H.264/H.265 y mezcla AAC estéreo a 48 kHz.
5. FFprobe exige vídeo, duración válida y audio cuando fue solicitado.
6. La duración final se compara con los temporales y se rechazan salidas truncadas.
7. Solo entonces el MP4 temporal sustituye al destino y se eliminan MKV/WAV.
8. Si RKMPP está ocupado, se reintenta por software sin perder los originales.

El modo Seguridad utiliza exactamente el mismo conversor y no inicia otro codificador
mientras el clip anterior sigue procesándose.

## ¿Por qué existe este proyecto? (Justificación Técnica)

Las aplicaciones de cámara por defecto en Linux (como **Cheese** o **GNOME Snapshot**) suelen fallar o congelarse al usar webcams 4K de alta velocidad (como la EMEET S600) en placas ARM64 como la **Orange Pi 5 Max (RK3588)**. Esto se debe a dos problemas principales:

1. **Saturación del bus USB por formato YUV:** Por defecto, Cheese y Snapshot intentan solicitar transmisiones de vídeo sin comprimir en formato **YUYV (YUV 4:2:2)**. A resoluciones altas como 1080p a 60 fps o 4K a 30 fps, el flujo de datos raw YUV excede el ancho de banda físico del bus USB, lo que causa que la imagen se congele, caiga a 2-3 fps o no se abra. Para obtener altas tasas de refresco y resolución, la cámara debe transmitir en formato comprimido **MJPEG**.
2. **Cuelgues del Kernel (Bug de RGA >4GB):** Si las aplicaciones de GNOME intentan decodificar por hardware usando GStreamer (`rockchipmpp`), estas delegan el procesamiento al motor gráfico 2D de Rockchip (**RGA**). Dado que el RGA tiene una MMU de 32 bits limitada a 4 GB, en placas con 16 GB de RAM el mapeo de memoria suele caer fuera de rango, corrompiendo la memoria del sistema y provocando un kernel panic inmediato (cuelgue total de la placa).

**B.I.O.R. Cam** soluciona esto de manera brillante:
* Fuerza a la cámara a entregar un flujo comprimido en **MJPEG** de alta velocidad.
* Delega la visualización en vivo a **mpv** mediante decodificación por **software** (`--hwdec=no`), eludiendo por completo el motor RGA del RK3588 y garantizando estabilidad absoluta en el kernel.
* Ajusta los parámetros de hardware (foco, zoom, brillo) directamente usando llamadas a bajo nivel con `v4l2-ctl`.
* Solo usa el codificador físico del chip (`h264_rkmpp` / `hevc_rkmpp`) para la compresión final del vídeo grabado en segundo plano, maximizando el rendimiento sin poner en riesgo la estabilidad del sistema.
