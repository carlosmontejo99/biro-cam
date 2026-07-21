#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B.I.O.R. Cam · Panel de control de cámara USB
Orange Pi 5 Max (RK3588) · Carlos Montejo Dávila · 2026-06-21

Arquitectura:
  · El panel lanza el visor mpv (decodificación por SOFTWARE -> nunca toca el
    motor RGA del RK3588, evitando el bug >4GB que colgaba el kernel).
  · Foto / Grabar / Resolución se mandan a mpv por su socket IPC.
  · Brillo, contraste, saturación, zoom, foco y exposición se ajustan EN VIVO
    con v4l2-ctl (funcionan aunque mpv esté transmitiendo).
Requisitos del sistema: mpv, v4l2-ctl (v4l-utils), PySide6.
"""

import array
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import traceback
import wave
from datetime import datetime

import cv2
import numpy as np
import threading

from PySide6.QtCore import Qt, QTimer, QSettings, QEvent, QProcess, QObject, Signal, QThread
from PySide6.QtGui import QIcon, QPixmap, QShortcut, QKeySequence, QImage
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QProgressBar, QPushButton, QScrollArea, QSlider,
    QVBoxLayout, QWidget,
)

# Intento de importación defensiva de freenect
try:
    import freenect
    FREENECT_AVAILABLE = True
except ImportError:
    freenect = None
    FREENECT_AVAILABLE = False


def check_kinect_connected():
    if not FREENECT_AVAILABLE:
        return False
    try:
        ctx = freenect.init()
        if not ctx:
            return False
        try:
            return freenect.num_devices(ctx) > 0
        finally:
            freenect.shutdown(ctx)
    except Exception:
        return False


# ----------------------------------------------------------------------------- Config
APP_DIR   = os.path.dirname(os.path.abspath(__file__))
VERSION   = "v2.10.0"


def clean_env(env=None):
    """Retorna un diccionario de entorno limpio de variables de AppImage que contaminan subprocesses."""
    if env is None:
        env = dict(os.environ)
    else:
        env = dict(env)
    # Si la AppImage guardó el LD_LIBRARY_PATH original, lo restauramos; de lo contrario, lo removemos.
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def mpv_executable():
    """Prefiere un mpv del host con X11; Conda puede carecer de ese backend."""
    system_mpv = shutil.which("mpv")
    if system_mpv:
        return system_mpv
    for path in (
            "/var/lib/flatpak/exports/bin/io.mpv.Mpv",
            os.path.expanduser("~/.local/share/flatpak/exports/bin/io.mpv.Mpv")):
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    bundled = os.path.join(os.path.dirname(sys.executable), "mpv")
    return bundled if os.path.exists(bundled) else "mpv"

ICON_PATH = os.path.join(APP_DIR, "assets", "icon_256.png")
SHUTTER_SOUND = os.path.join(os.path.expanduser("~/.cache/biro-cam"), "shutter.wav")


def _v4l2_output(device, *args):
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", device, *args], capture_output=True,
            text=True, timeout=3, env=clean_env())
        return result.stdout + result.stderr
    except (OSError, subprocess.SubprocessError):
        return ""


def list_camera_devices():
    """Devuelve solo nodos capaces de capturar imagen, con una ruta estable."""
    stable_paths = {}
    for directory in ("/dev/v4l/by-id", "/dev/v4l/by-path"):
        try:
            for name in sorted(os.listdir(directory)):
                if "video-index0" not in name:
                    continue
                link = os.path.join(directory, name)
                real = os.path.realpath(link)
                if os.path.exists(real):
                    stable_paths.setdefault(real, link)
        except OSError:
            pass

    cameras = []
    for index in range(32):
        real = f"/dev/video{index}"
        if not os.path.exists(real):
            continue
        info = _v4l2_output(real, "--all")
        device_caps = info.split("Device Caps", 1)[-1]
        device_caps = re.split(
            r"\n(?:Media Driver Info|Priority|Video input|Format )",
            device_caps, maxsplit=1)[0]
        if "Video Capture" not in device_caps:
            continue
        card_match = re.search(r"Card type\s*:\s*(.+)", info)
        label = card_match.group(1).strip() if card_match else f"Cámara {index + 1}"
        path = stable_paths.get(real, real)
        cameras.append((path, label, real))

    cameras.sort(key=lambda item: (
        0 if "emeet" in item[1].lower() else 1,
        1 if "infrared" in item[1].lower() or item[1].rstrip().endswith(" I") else 0,
        item[1].lower(), item[2]))
    return cameras


DEFAULT_RESOLUTIONS = [
    (3840, 2160, 30, "4K · 30"),
    (2560, 1440, 30, "1440p · 30"),
    (1920, 1080, 60, "1080p · 60"),
    (1280,  720, 60, "720p · 60"),
    ( 640,  480, 30, "480p · 30"),
]


def camera_modes(device):
    """Detecta el mejor formato y las resoluciones reales de una cámara V4L2."""
    output = _v4l2_output(device, "--list-formats-ext")
    formats = {}
    current = None
    current_size = None
    for line in output.splitlines():
        fmt = re.search(r"\[\d+\]:\s+'([^']+)'", line)
        if fmt:
            current = fmt.group(1)
            formats.setdefault(current, {})
            current_size = None
            continue
        size = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if size and current:
            current_size = (int(size.group(1)), int(size.group(2)))
            formats[current].setdefault(current_size, [])
            continue
        fps = re.search(r"\((\d+(?:\.\d+)?)\s+fps\)", line, re.IGNORECASE)
        if fps and current and current_size:
            formats[current][current_size].append(max(1, round(float(fps.group(1)))))

    selected = next((fmt for fmt in ("MJPG", "YUYV", "GREY") if fmt in formats), None)
    if not selected and formats:
        selected = next(iter(formats))
    input_formats = {"MJPG": "mjpeg", "YUYV": "yuyv422", "GREY": "gray"}
    input_format = input_formats.get(selected, selected.lower() if selected else "mjpeg")
    modes = []
    for (width, height), rates in formats.get(selected, {}).items():
        fps = max(rates) if rates else 30
        label = (f"4K · {fps}" if (width, height) == (3840, 2160)
                 else f"{height}p · {fps}")
        modes.append((width, height, fps, label))
    modes.sort(key=lambda mode: (mode[0] * mode[1], mode[2]), reverse=True)
    return (modes or list(DEFAULT_RESOLUTIONS)), input_format


CAMERAS = list_camera_devices()
DEV = CAMERAS[0][0] if CAMERAS else ""
CAM_URL = f"av://v4l2:{DEV}" if DEV else ""
RESOLUTIONS, CAM_INPUT_FORMAT = camera_modes(DEV) if DEV else (list(DEFAULT_RESOLUTIONS), "mjpeg")
IPC_SOCK  = "/tmp/mpv-biro-cam.sock"
PHOTO_DIR = os.path.expanduser("~/Imágenes/Camera")
VIDEO_DIR = os.path.expanduser("~/Vídeos/Camera")
SECURITY_DIR = os.path.join(VIDEO_DIR, "Seguridad")
RUNTIME_DIR = os.path.expanduser("~/.cache/biro-cam")
SECURITY_FRAMES = ("/tmp/biro-cam-security-frame-a.jpg",
                   "/tmp/biro-cam-security-frame-b.jpg")

# Controles v4l2: (id, etiqueta, min, max, default)
CONTROLS = [
    ("brightness",  "Brillo",      -64, 191,   0),
    ("contrast",    "Contraste",     0, 255,  57),
    ("saturation",  "Saturación",    0, 128,  82),
    ("gamma",       "Gamma",        72, 500, 214),
    ("gain",        "Ganancia",      0, 100,   0),
    ("sharpness",   "Nitidez",       1, 128,  32),
]

# Efectos: (etiqueta, filtro libavfilter). "" = sin efecto. Se aplican al preview y
# a la foto vía vf de mpv, y a la grabación al convertir (mismo filtro en ffmpeg).
EFFECTS = [
    ("Sin efecto", ""),
    ("B/N (grises)", "hue=s=0"),
    ("Sepia", "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"),
    ("Vívido", "eq=saturation=1.6:contrast=1.08"),
    ("Cálido", "colortemperature=temperature=4500"),
    ("Negativo", "negate"),
]

# Encuadre (guía de composición, solo visual — no sale en foto/vídeo). 0=off.
GRID_NAMES = ["Sin encuadre", "Tercios", "Proporción áurea",
              "Espiral (Fibonacci)", "Cruz centrada", "Diagonales"]

# Calidad / formato de foto: (etiqueta, formato_mpv, calidad_jpg (0 si no aplica))
PHOTO_QUALITY = [
    ("JPG 100%", "jpg", 100),
    ("JPG 80%",  "jpg",  80),
    ("JPG 60%",  "jpg",  60),
    ("JPG 40%",  "jpg",  40),
]

# Duración máxima de grabación: (etiqueta, minutos, 0 = ilimitado)
REC_DURATIONS = [
    ("Sin límite", 0),
    ("5 min", 5),
    ("15 min", 15),
    ("30 min", 30),
    ("60 min", 60),
]

# RKMPP solo existe en equipos Rockchip. En PC, los codificadores software son
# más universales y no dependen de que la GPU dedicada esté activa.
IS_ROCKCHIP = platform.machine().lower() in ("aarch64", "arm64")
CODECS = ([
    ("H.264 (RKMPP)", "h264_rkmpp"),
    ("H.265 (RKMPP)", "hevc_rkmpp"),
] if IS_ROCKCHIP else [
    ("H.264 (compatible)", "libx264"),
    ("H.265 (más compacto)", "libx265"),
])


def timestamp_filter(prefix: str):
    """Crea un drawtext robusto; el texto va en archivo para que ':' no rompa el filtro."""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    text_path = os.path.join(
        RUNTIME_DIR, f"timestamp-{prefix}-{os.getpid()}-{time.time_ns()}.txt")
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    vf = (f"drawtext=textfile={text_path}:fontsize=60:fontcolor=white:"
          "x=w-tw-10:y=20:box=1:boxcolor=black@0.5")
    return vf, text_path


# ----------------------------------------------------------------------------- Cámara
def v4l2_set(ctrl: str, value) -> None:
    """Ajusta un control de la cámara sin bloquear la UI."""
    try:
        subprocess.Popen(
            ["v4l2-ctl", "-d", DEV, "-c", f"{ctrl}={value}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=clean_env()
        )
    except FileNotFoundError:
        print("v4l2-ctl no encontrado (instala v4l-utils)", file=sys.stderr)


def v4l2_get(ctrl: str):
    """Lee el valor actual de un control; None si falla."""
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", DEV, "-C", ctrl],
            capture_output=True, text=True, timeout=2,
            env=clean_env()
        ).stdout.strip()
        return int(out.split(":")[1])
    except Exception:
        return None


def v4l2_control_details(device=None):
    """Obtiene rango, valor y predeterminado de los controles de una cámara."""
    details = {}
    output = _v4l2_output(device or DEV, "--list-ctrls")
    pattern = re.compile(
        r"^\s*(\w+)\s+0x[0-9a-f]+\s+\([^)]*\)\s*:\s*"
        r".*?min=(-?\d+)\s+max=(-?\d+)\s+step=(-?\d+)\s+"
        r"default=(-?\d+)\s+value=(-?\d+)")
    for line in output.splitlines():
        match = pattern.search(line)
        if match:
            name, lo, hi, step, default, value = match.groups()
            details[name] = tuple(map(int, (lo, hi, step, default, value)))
    return details


def list_mics():
    """Lista las fuentes de entrada (micrófonos) reales del sistema (sin monitores).
    Devuelve [(source_name, etiqueta_amigable)]."""
    mics = []
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True, timeout=3,
                             env=clean_env()).stdout
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[1]
            if name.endswith(".monitor"):
                continue
            low = name.lower()
            if "emeet" in low:   label = "🎥 Micro de la cámara"
            elif "k66" in low:   label = "🎧 K66 (USB)"
            elif "es83" in low:  label = "🔊 Entrada integrada (OPi)"
            else:                label = "🎙 " + name.split(".")[-1]
            mics.append((name, label))
    except Exception:
        pass
    return mics


def mpv_ipc(command: list) -> None:
    """Envía un comando a mpv por su socket IPC (JSON IPC protocol)."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(IPC_SOCK)
        s.sendall((json.dumps({"command": command}) + "\n").encode("utf-8"))
        s.close()
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        pass  # mpv aún no arranca o ya cerró; se ignora


def mpv_ipc_result(command: list, timeout=3.0):
    """Ejecuta IPC y devuelve la respuesta de mpv; nunca oculta un error."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(IPC_SOCK)
            s.sendall((json.dumps({"command": command}) + "\n").encode("utf-8"))
            data = b""
            while b"\n" not in data:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        if not data:
            return {"error": "mpv no respondió"}
        return json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc)}



# ----------------------------------------------------------------------------- Hilo de Adquisición de Kinect
class KinectWorker(QThread):
    frame_ready = Signal(np.ndarray, np.ndarray)  # rgb, depth
    status = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.ctx = None
        self.dev = None
        self.video_mode = "RGB"  # "RGB" o "IR"
        self._target_tilt = None
        self._target_led = None
        self._rgb_frame = None
        self._depth_frame = None
        self._lock = threading.Lock()

    def set_tilt(self, angle):
        self._target_tilt = max(-27, min(27, angle))

    def set_led(self, led_val):
        self._target_led = led_val

    def set_mode(self, mode):
        self.video_mode = mode

    def run(self):
        if not FREENECT_AVAILABLE:
            self.error.emit("freenect no está instalado")
            return

        streams_started = False
        try:
            self.ctx = freenect.init()
            if not self.ctx:
                raise RuntimeError("No se pudo crear el contexto de libfreenect")
            if freenect.num_devices(self.ctx) <= 0:
                raise RuntimeError("No hay ningún dispositivo Kinect conectado")
            self.dev = freenect.open_device(self.ctx, 0)
            if not self.dev:
                raise RuntimeError("libfreenect no pudo abrir el dispositivo")
        except Exception as e:
            self.error.emit(f"No se pudo iniciar Kinect: {e}")
            if self.ctx:
                try:
                    freenect.shutdown(self.ctx)
                except Exception:
                    pass
            self.ctx = None
            return

        # Callbacks
        def video_cb(dev, data, timestamp):
            with self._lock:
                self._rgb_frame = data.copy()

        def depth_cb(dev, data, timestamp):
            with self._lock:
                self._depth_frame = data.copy()

        freenect.set_video_callback(self.dev, video_cb)
        freenect.set_depth_callback(self.dev, depth_cb)

        # Configurar formatos iniciales
        current_mode = freenect.VIDEO_RGB
        try:
            freenect.set_video_mode(self.dev, freenect.RESOLUTION_MEDIUM, current_mode)
            freenect.set_depth_mode(self.dev, freenect.RESOLUTION_MEDIUM, freenect.DEPTH_11BIT)
            freenect.start_video(self.dev)
            freenect.start_depth(self.dev)
            streams_started = True
        except Exception as e:
            self.error.emit(f"Error al configurar flujos de Kinect: {e}")
            self._release_device(streams_started)
            return

        self.running = True
        self.status.emit("Kinect conectado y transmitiendo")

        while self.running and not self.isInterruptionRequested():
            # Procesar eventos de USB
            try:
                freenect.process_events(self.ctx)
            except Exception as e:
                print("Error process_events:", e)
                break

            # Aplicar inclinación si hay cambio pendiente
            if self._target_tilt is not None:
                try:
                    freenect.set_tilt_degs(self.dev, self._target_tilt)
                except Exception as e:
                    print("Error set_tilt_degs:", e)
                self._target_tilt = None

            # Aplicar LED si hay cambio pendiente
            if self._target_led is not None:
                try:
                    freenect.set_led(self.dev, self._target_led)
                except Exception as e:
                    print("Error set_led:", e)
                self._target_led = None

            # Aplicar cambio de modo de video
            target_vid_mode = freenect.VIDEO_IR_8BIT if self.video_mode == "IR" else freenect.VIDEO_RGB
            if target_vid_mode != current_mode:
                try:
                    freenect.stop_video(self.dev)
                    freenect.set_video_mode(self.dev, freenect.RESOLUTION_MEDIUM, target_vid_mode)
                    freenect.start_video(self.dev)
                    current_mode = target_vid_mode
                except Exception as e:
                    print("Error al cambiar modo de vídeo:", e)

            # Obtener y emitir frames
            with self._lock:
                rgb = self._rgb_frame
                depth = self._depth_frame
                self._rgb_frame = None
                self._depth_frame = None

            if rgb is not None and depth is not None:
                self.frame_ready.emit(rgb, depth)

            self.msleep(15)  # Evitar saturar el procesador

        self._release_device(streams_started)
        self.status.emit("Kinect apagado")

    def _release_device(self, streams_started):
        """Libera libusb exactamente una vez, incluso tras una inicialización parcial."""
        try:
            if self.dev and streams_started:
                try:
                    freenect.stop_video(self.dev)
                finally:
                    freenect.stop_depth(self.dev)
        except Exception:
            pass
        try:
            if self.dev:
                freenect.close_device(self.dev)
        except Exception:
            pass
        finally:
            self.dev = None
        try:
            if self.ctx:
                freenect.shutdown(self.ctx)
        except Exception:
            pass
        finally:
            self.ctx = None
            self.running = False


# ----------------------------------------------------------------------------- Motor de Seguridad

class SecurityEngine(QObject):
    """Detecta movimiento en capturas de mpv sin reabrir el dispositivo UVC."""

    frame_ready = Signal(object)     # numpy array BGR
    motion_detected = Signal()
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False
        self.capturing = False
        self._prev_gray = None
        self._frame_index = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._process)
        self.sensitivity = 25
        self.cooldown = 10
        self.intelligent_detection = True
        self._rknn_model = None
        self._hog_model = None
        self.backend_name = "CPU"
        self._init_npu()

    def _init_npu(self):
        try:
            from rknnlite.api import RKNNLite
            self._rknn_model = RKNNLite()
            model_path = os.path.join(
                APP_DIR, "assets", "yolov5s-640-640-rk3588.rknn")
            if os.path.exists(model_path):
                ret = self._rknn_model.load_rknn(model_path)
                if ret == 0 and self._rknn_model.init_runtime(
                        core_mask=RKNNLite.NPU_CORE_AUTO) == 0:
                    self.backend_name = "NPU RK3588"
                    print("SecurityEngine: NPU RK3588 cargado para seguridad ✓")
                    return
            self._release_npu()
            self._rknn_model = None
            self.backend_name = "CPU"
        except Exception:
            self._release_npu()
            self._rknn_model = None
            self.backend_name = "CPU"

    def _release_npu(self):
        model = self._rknn_model
        if model is not None:
            try:
                model.release()
            except Exception:
                pass

    def shutdown(self):
        self.stop()
        self._release_npu()
        self._rknn_model = None
        self._hog_model = None

    def _detect_humans(self, frame):
        h, w, _ = frame.shape
        # 1. Usar NPU de Rockchip si el modelo RKNN fue cargado
        if self._rknn_model:
            try:
                img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img_resized = cv2.resize(img_rgb, (640, 640))
                outputs = self._rknn_model.inference(
                    inputs=[np.expand_dims(img_resized, axis=0)],
                    data_format=["nhwc"])
                return self._yolov5_person_boxes(outputs, w, h)
            except Exception as e:
                print("Error NPU en detector de seguridad:", e)
                self._release_npu()
                self._rknn_model = None
                self.backend_name = "CPU"
                self.status.emit("⚠ NPU no disponible; usando detector CPU")

        # 2. Fallback a detector HOG (CPU OpenCV) integrado
        if self._hog_model is None:
            self._hog_model = cv2.HOGDescriptor()
            self._hog_model.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

        scale = 1.0
        if w > 640:
            scale = 640.0 / w
            img_small = cv2.resize(frame, (640, int(h * scale)))
        else:
            img_small = frame.copy()

        gray = cv2.cvtColor(img_small, cv2.COLOR_BGR2GRAY)
        boxes, _ = self._hog_model.detectMultiScale(gray, winStride=(8, 8), padding=(8, 8), scale=1.05)

        real_boxes = []
        for (bx, by, bw, bh) in boxes:
            real_boxes.append((int(bx / scale), int(by / scale), int(bw / scale), int(bh / scale)))
        return real_boxes

    @staticmethod
    def _yolov5_person_boxes(outputs, frame_w, frame_h,
                              conf_threshold=0.35, nms_threshold=0.45):
        """Postprocesa las tres salidas YOLOv5 de RKNN y conserva clase person=0."""
        if not outputs or len(outputs) != 3:
            raise ValueError("YOLOv5 RKNN debe producir tres salidas")
        anchors = (((10, 13), (16, 30), (33, 23)),
                   ((30, 61), (62, 45), (59, 119)),
                   ((116, 90), (156, 198), (373, 326)))
        candidates = []
        for raw, scale_anchors in zip(outputs, anchors):
            data = np.asarray(raw)
            if data.ndim != 4:
                raise ValueError(f"Salida YOLOv5 inesperada: {data.shape}")
            # RKNN devuelve NCHW: (1, 255, grid_h, grid_w).
            if data.shape[1] == 255:
                data = data[0].reshape(3, 85, data.shape[2], data.shape[3])
                data = data.transpose(2, 3, 0, 1)
            # También tolerar NHWC: (1, grid_h, grid_w, 255).
            elif data.shape[-1] == 255:
                data = data[0].reshape(data.shape[1], data.shape[2], 3, 85)
            else:
                raise ValueError(f"Canales YOLOv5 inesperados: {data.shape}")
            gh, gw = data.shape[:2]
            for anchor_idx, (aw, ah) in enumerate(scale_anchors):
                pred = data[:, :, anchor_idx, :]
                score = pred[:, :, 4] * pred[:, :, 5]  # objectness * person
                ys, xs = np.where(score >= conf_threshold)
                for gy, gx in zip(ys, xs):
                    px = pred[gy, gx]
                    cx = (px[0] * 2.0 - 0.5 + gx) * (640.0 / gw)
                    cy = (px[1] * 2.0 - 0.5 + gy) * (640.0 / gh)
                    bw = (px[2] * 2.0) ** 2 * aw
                    bh = (px[3] * 2.0) ** 2 * ah
                    x1 = (cx - bw / 2.0) * frame_w / 640.0
                    y1 = (cy - bh / 2.0) * frame_h / 640.0
                    x2 = (cx + bw / 2.0) * frame_w / 640.0
                    y2 = (cy + bh / 2.0) * frame_h / 640.0
                    candidates.append((x1, y1, x2, y2, float(score[gy, gx])))
        if not candidates:
            return []
        boxes = np.array([[c[0], c[1], c[2] - c[0], c[3] - c[1]]
                          for c in candidates], dtype=np.float32)
        scores = np.array([c[4] for c in candidates], dtype=np.float32)
        keep = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(),
                                conf_threshold, nms_threshold)
        result = []
        for idx in np.asarray(keep).reshape(-1):
            x, y, bw, bh = boxes[int(idx)]
            x = max(0, min(frame_w - 1, int(x)))
            y = max(0, min(frame_h - 1, int(y)))
            bw = max(1, min(frame_w - x, int(bw)))
            bh = max(1, min(frame_h - y, int(bh)))
            result.append((x, y, bw, bh))
        return result

    # -- control de vida --
    def start(self, sensitivity=25, cooldown=10):
        self.sensitivity = sensitivity
        self.cooldown = cooldown
        self.active = True
        self.capturing = True
        self._prev_gray = None
        self._frame_index = 0
        for path in SECURITY_FRAMES:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        self._timer.start(250)    # 4 fps, suficiente para detectar movimiento

    def stop(self):
        self.active = False
        self._timer.stop()
        self.capturing = False
        self._prev_gray = None

    def pause_cam(self):
        self._timer.stop()
        self.capturing = False

    def resume_cam(self):
        self._prev_gray = None
        self.capturing = True
        self._timer.start(250)

    def set_sensitivity(self, v):
        self.sensitivity = max(1, min(100, v))

    def set_cooldown(self, v):
        self.cooldown = max(3, min(60, v))

    def _process(self):
        if not self.capturing:
            return
        try:
            self._process_inner()
        except Exception as e:
            print("Error en proceso de seguridad:", e)

    def _process_inner(self):
        if not self.capturing:
            return
        path = SECURITY_FRAMES[self._frame_index]
        self._frame_index = 1 - self._frame_index
        reply = mpv_ipc_result(["screenshot-to-file", path, "video"])
        if reply.get("error") != "success":
            self.status.emit(f"Error captura detector: {reply.get('error')}")
            return
        frame = cv2.imread(path)
        if frame is None:
            self.status.emit("Error captura detector: JPEG inválido")
            return

        motion = False
        if self.intelligent_detection:
            # Detección inteligente de personas
            boxes = self._detect_humans(frame)
            if len(boxes) > 0:
                motion = True
                for (bx, by, bw, bh) in boxes:
                    cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), (0, 255, 0), 2)
                    cv2.putText(frame, "HUMANO", (bx, by - 8),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
            else:
                motion = False
        else:
            # Detección clásica por diferencia de píxeles
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            if self._prev_gray is None:
                self._prev_gray = gray
                self.frame_ready.emit(frame)
                return

            diff = cv2.absdiff(self._prev_gray, gray)
            threshold = max(1, 101 - self.sensitivity)
            thresh = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)[1]
            thresh = cv2.dilate(thresh, None, iterations=2)
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL,
                                            cv2.CHAIN_APPROX_SIMPLE)
            self._prev_gray = gray
            motion = any(cv2.contourArea(c) > 500 for c in contours)

            if motion:
                for c in contours:
                    if cv2.contourArea(c) > 500:
                        (bx, by, bw, bh) = cv2.boundingRect(c)
                        cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), (0, 0, 255), 1)

        # Emitir el frame con rectángulos al preview HUD
        self.frame_ready.emit(frame)

        if motion:
            self.motion_detected.emit()


# ----------------------------------------------------------------------------- UI
class Panel(QMainWindow):
    def __init__(self):
        super().__init__()
        sys.excepthook = self._exception_hook
        self.setWindowTitle("B.I.O.R. Cam · Carlos")
        self.recording = False
        self.exposure_auto = True
        self.focus_auto = True
        self.mpv_proc = None
        self._started = False
        self._photo_timer = None
        self._photo_timer_active = False
        self._photo_countdown = 0
        self._shutter_enabled = True

        os.makedirs(PHOTO_DIR, exist_ok=True)
        self._ensure_shutter_sound()

        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))

        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- Vídeo incrustado de la cámara (izquierda) --------------------
        self.video = QWidget()
        self.video.setObjectName("video")
        self.video.setAttribute(Qt.WA_NativeWindow, True)   # ventana nativa -> winId para mpv
        self.video.setMinimumSize(480, 360)
        outer.addWidget(self.video, 1)

        # Algunas cámaras dobles (RGB + IR), como la del ASUS ROG, comparten
        # sensor/controlador. Al volver de IR, la exposición RGB tarda varios
        # segundos en converger y los primeros cuadros salen casi negros.
        self.camera_warmup_label = QLabel("Ajustando exposición…", self.video)
        self.camera_warmup_label.setAlignment(Qt.AlignCenter)
        self.camera_warmup_label.setAttribute(Qt.WA_NativeWindow, True)
        self.camera_warmup_label.setStyleSheet(
            "background:#000;color:#cbd5e1;font-size:24px;font-weight:600;")
        self.camera_warmup_label.hide()

        # ---- Overlay profesional HUD del modo seguridad (semi-transparente) ---
        self.security_overlay = QWidget(self.video)
        self.security_overlay.setAttribute(Qt.WA_NativeWindow, True)
        self.security_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.security_overlay.setStyleSheet("background:rgba(6,11,22,0.55);border:1px solid #1e3a5f;border-radius:10px;")
        so_lay = QVBoxLayout(self.security_overlay)
        so_lay.setAlignment(Qt.AlignCenter)
        so_lay.setContentsMargins(20, 16, 20, 16)
        so_lay.setSpacing(12)
        so_icon = QLabel("🛡️"); so_icon.setAlignment(Qt.AlignCenter)
        so_icon.setStyleSheet("font-size:64px;background:transparent;")
        so_lay.addWidget(so_icon)
        so_title = QLabel("MODO SEGURIDAD"); so_title.setAlignment(Qt.AlignCenter)
        so_title.setStyleSheet("font-size:36px;font-weight:bold;color:#e6edf6;background:transparent;")
        so_lay.addWidget(so_title)
        self.sec_progress = QProgressBar()
        self.sec_progress.setRange(0, 0)
        self.sec_progress.setFixedHeight(4)
        self.sec_progress.setTextVisible(False)
        self.sec_progress.setStyleSheet(
            "QProgressBar{background:#0c1320;border:1px solid #1b2536;border-radius:2px;}"
            "QProgressBar::chunk{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #3b82f6,stop:1 #60a5fa);border-radius:1px;}")
        so_lay.addWidget(self.sec_progress)
        self.sec_status_lbl = QLabel("Iniciando detector…")
        self.sec_status_lbl.setAlignment(Qt.AlignCenter)
        self.sec_status_lbl.setStyleSheet("font-size:26px;color:#94a3b8;background:transparent;")
        so_lay.addWidget(self.sec_status_lbl)
        self.sec_params_lbl = QLabel("")
        self.sec_params_lbl.setAlignment(Qt.AlignCenter)
        self.sec_params_lbl.setWordWrap(True)
        self.sec_params_lbl.setStyleSheet("font-size:20px;color:#64748b;background:transparent;")
        so_lay.addWidget(self.sec_params_lbl)
        self.security_overlay.hide()

        # ---- Preview del modo seguridad (OpenCV) ----------------------------
        self.security_label = QLabel(self.video)
        self.security_label.setAlignment(Qt.AlignCenter)
        self.security_label.setStyleSheet("background:#000;")
        self.security_label.hide()

        # ---- Preview del modo Kinect (OpenCV) -------------------------------
        self.kinect_label = QLabel(self.video)
        self.kinect_label.setAlignment(Qt.AlignCenter)
        self.kinect_label.setStyleSheet("background:#000;")
        self.kinect_label.hide()


        # ---- Panel de controles (derecha) ---------------------------------
        self.panel = QWidget()
        self.panel.setObjectName("panel")
        self.panel.setFixedWidth(390)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.panel)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setObjectName("panel_scroll")
        outer.addWidget(scroll)
        self.emeet_widgets = []

        # ---- Header: logo + título + estado del visor ---------------------
        head = QHBoxLayout()
        logo = QLabel()
        if os.path.exists(ICON_PATH):
            logo.setPixmap(QPixmap(ICON_PATH).scaled(
                44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        head.addWidget(logo)
        tbox = QVBoxLayout(); tbox.setSpacing(0)
        t1 = QLabel("B.I.O.R. Cam"); t1.setObjectName("title")
        t2 = QLabel(f"Panel de control · 4K · {VERSION}"); t2.setObjectName("subtitle")
        t2.setToolTip("B.I.O.R. Webcam Control · Carlos Montejo Dávila")
        tbox.addWidget(t1); tbox.addWidget(t2)
        head.addLayout(tbox)
        head.addStretch(1)
        self.rec_label = QLabel(""); self.rec_label.setObjectName("rec")
        self.rec_label.setVisible(False)
        head.addWidget(self.rec_label)
        self.dot = QLabel("●"); self.dot.setObjectName("dot_off")
        self.dot.setToolTip("Visor desconectado")
        head.addWidget(self.dot)
        lay.addLayout(head)
        lay.addWidget(self._sep())

        # ---- Selector de Cámara / Dispositivo ----
        self.dev_select_box = QWidget()
        dev_select_lay = QHBoxLayout(self.dev_select_box)
        dev_select_lay.setContentsMargins(0, 0, 0, 4)
        dev_select_lay.addWidget(QLabel("Dispositivo:"))
        self.dev_combo = QComboBox()
        for path, label, real in CAMERAS:
            suffix = f" ({real})" if real not in label else ""
            self.dev_combo.addItem(f"🎥 {label}{suffix}", path)
        if not CAMERAS:
            self.dev_combo.addItem("⚠ No se detectaron cámaras", "")
        if check_kinect_connected():
            self.dev_combo.addItem("🛡️ Xbox 360 Kinect", "kinect")
        self.dev_combo.currentIndexChanged.connect(self._on_device_changed)
        dev_select_lay.addWidget(self.dev_combo, 1)
        lay.addWidget(self.dev_select_box)
        lay.addWidget(self._sep())


        # ---- Acciones: captura ---------------------------------------------
        top = QHBoxLayout()
        self.btn_photo = QPushButton("● Foto")
        self.btn_rec   = QPushButton("⏺ Grabar")
        self.btn_photo.setToolTip("Tomar foto (Espacio / S)")
        self.btn_rec.setToolTip("Iniciar / Detener grabación (R)")
        self.timer_combo = QComboBox()
        self.timer_combo.addItems(["Ahora", "3s", "10s"])
        self.timer_combo.setMinimumWidth(85)
        self.timer_combo.setToolTip("Temporizador: toma la foto con retardo")
        self.btn_photo.clicked.connect(self.take_photo)
        self.btn_rec.clicked.connect(self.toggle_record)
        for b in (self.btn_photo, self.btn_rec):
            b.setMinimumHeight(38)
            top.addWidget(b)
        top.addSpacing(6)
        top.addWidget(self.timer_combo)
        lay.addLayout(top)

        # ---- Acciones: abrir carpetas (fotos / vídeos) ---------------------
        gal = QHBoxLayout()
        self.btn_fotos  = QPushButton("🖼 Fotos")
        self.btn_videos = QPushButton("🎬 Vídeos")
        self.btn_fotos.setToolTip("Abrir carpeta de fotos (G)")
        self.btn_videos.setToolTip("Abrir carpeta de vídeos (V)")
        self.btn_fotos.clicked.connect(lambda: subprocess.Popen(["xdg-open", PHOTO_DIR], env=clean_env()))
        self.btn_videos.clicked.connect(self._open_videos)
        for b in (self.btn_fotos, self.btn_videos):
            b.setMinimumHeight(34)
            gal.addWidget(b)
        lay.addLayout(gal)

        # ---- Calidad de foto -----------------------------------------------
        pq_row = QHBoxLayout()
        pq_row.addWidget(QLabel("Calidad foto"))
        self.photo_quality_combo = QComboBox()
        for label, _, _ in PHOTO_QUALITY:
            self.photo_quality_combo.addItem(label)
        self.photo_quality_combo.setCurrentIndex(1)  # JPG 80%
        self.photo_quality_combo.setToolTip("A mayor calidad, mayor tamaño de archivo")
        pq_row.addWidget(self.photo_quality_combo, 1)
        lay.addLayout(pq_row)

        # ---- Sonido de obturador -------------------------------------------
        shutter_row = QHBoxLayout()
        self.shutter_check = QCheckBox("🔊 Sonido obturador")
        self.shutter_check.setChecked(True)
        self.shutter_check.toggled.connect(lambda v: setattr(self, "_shutter_enabled", v))
        self.shutter_check.setToolTip("Reproduce un click al tomar foto")
        shutter_row.addWidget(self.shutter_check)
        lay.addLayout(shutter_row)

        # ---- Micrófono para la grabación (se bloquea al grabar) ------------
        self.mic_box = QWidget(); self.mic_box.setObjectName("lockrow")
        mic_row = QHBoxLayout(self.mic_box); mic_row.setContentsMargins(0, 0, 0, 0)
        self.mic_label = QLabel("Micrófono")
        mic_row.addWidget(self.mic_label)
        self.mic_combo = QComboBox()
        self.mic_combo.addItem("🔇 Sin audio", None)
        emeet_idx = -1
        for name, label in list_mics():
            self.mic_combo.addItem(label, name)
            if "emeet" in name.lower():
                emeet_idx = self.mic_combo.count() - 1
        if emeet_idx >= 0:                       # por defecto, el micro de la cámara
            self.mic_combo.setCurrentIndex(emeet_idx)
        self.mic_combo.setToolTip("Fuente de audio que se mezcla en la grabación")
        mic_row.addWidget(self.mic_combo, 1)
        lay.addWidget(self.mic_box)

        # ---- Medidor de nivel del micrófono (VU) ---------------------------
        self.vu = QProgressBar()
        self.vu.setObjectName("vu")
        self.vu.setRange(0, 100)
        self.vu.setTextVisible(False)
        self.vu.setFixedHeight(7)
        self.vu.setToolTip("Nivel de audio del micrófono seleccionado")
        lay.addWidget(self.vu)
        self._vu_proc = None
        self._vu_level = 0.0
        self.mic_combo.currentIndexChanged.connect(self._restart_vu)

        # ---- Resolución (se bloquea al grabar) -----------------------------
        self.res_box = QWidget(); self.res_box.setObjectName("lockrow")
        res_row = QHBoxLayout(self.res_box); res_row.setContentsMargins(0, 0, 0, 0)
        self.res_label = QLabel("Resolución")
        res_row.addWidget(self.res_label)
        self.res_combo = QComboBox()
        for _, _, _, label in RESOLUTIONS:
            self.res_combo.addItem(label)
        self.res_combo.currentIndexChanged.connect(self.change_resolution)
        res_row.addWidget(self.res_combo, 1)
        lay.addWidget(self.res_box)
        self.emeet_widgets.append(self.res_box)

        # ---- Bitrate de grabación (se bloquea al grabar) --------------------
        self.bitrate_box = QWidget(); self.bitrate_box.setObjectName("lockrow")
        bitrate_row = QHBoxLayout(self.bitrate_box)
        bitrate_row.setContentsMargins(0, 0, 0, 0)
        self.bitrate_label = QLabel("Bitrate")
        bitrate_row.addWidget(self.bitrate_label)
        self.bitrate_slider = QSlider(Qt.Horizontal)
        self.bitrate_slider.setRange(1, 50)
        self.bitrate_slider.setValue(6)
        self.bitrate_value_label = QLabel("6 Mbps")
        self.bitrate_value_label.setMinimumWidth(60)
        self.bitrate_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.bitrate_slider.valueChanged.connect(self._on_bitrate)
        bitrate_row.addWidget(self.bitrate_slider, 1)
        bitrate_row.addWidget(self.bitrate_value_label)
        lay.addWidget(self.bitrate_box)

        # ---- Duración + Códec (se bloquean al grabar) ----------------------
        self.dur_codec_box = QWidget(); self.dur_codec_box.setObjectName("lockrow")
        dc_row = QHBoxLayout(self.dur_codec_box)
        dc_row.setContentsMargins(0, 0, 0, 0)
        self.dur_label = QLabel("Duración")
        dc_row.addWidget(self.dur_label)
        self.dur_combo = QComboBox()
        for label, _ in REC_DURATIONS:
            self.dur_combo.addItem(label)
        dim = self.dur_combo.sizeHint()
        self.dur_combo.setFixedWidth(max(dim.width(), 85))
        dc_row.addWidget(self.dur_combo)
        dc_row.addSpacing(10)
        self.codec_label = QLabel("Códec")
        dc_row.addWidget(self.codec_label)
        self.codec_combo = QComboBox()
        for label, _ in CODECS:
            self.codec_combo.addItem(label)
        dc_row.addWidget(self.codec_combo, 1)
        lay.addWidget(self.dur_codec_box)

        lay.addSpacing(4)
        lay.addWidget(self._sep())
        lay.addSpacing(2)

        # ---- Deslizadores de imagen ---------------------------------------
        self.sliders_grid_widget = QWidget()
        grid = QGridLayout(self.sliders_grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(6)
        self.sliders = {}
        self.value_labels = {}
        control_details = v4l2_control_details()
        for row, (cid, label, lo, hi, default) in enumerate(CONTROLS):
            grid.addWidget(QLabel(label), row, 0)
            current = v4l2_get(cid)
            if cid in control_details:
                lo, hi, _, default, current = control_details[cid]
            sld = QSlider(Qt.Horizontal)
            sld.setRange(lo, hi)
            sld.setValue(current if current is not None else default)
            vlab = QLabel(str(sld.value()))
            vlab.setMinimumWidth(40)
            vlab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            sld.valueChanged.connect(lambda v, c=cid, l=vlab: self._on_slider(c, v, l))
            grid.addWidget(sld, row, 1)
            grid.addWidget(vlab, row, 2)
            self.sliders[cid] = sld
            self.value_labels[cid] = vlab
        lay.addWidget(self.sliders_grid_widget)
        self.emeet_widgets.append(self.sliders_grid_widget)


        lay.addSpacing(4)
        lay.addWidget(self._sep())
        lay.addSpacing(2)

        # ---- Zoom ----------------------------------------------------------
        self.zoom_container = QWidget()
        zoom_lay = QVBoxLayout(self.zoom_container)
        zoom_lay.setContentsMargins(0, 0, 0, 0)
        self.zoom_slider, self.zoom_label = self._special_row(
            zoom_lay, "Zoom", 0, 100, 0, self._on_zoom, suffix="%")  # zoom digital de mpv
        lay.addWidget(self.zoom_container)
        self.emeet_widgets.append(self.zoom_container)

        # ---- Exposición (con auto) ----------------------------------------
        self.exp_container = QWidget()
        exp_lay = QHBoxLayout(self.exp_container)
        exp_lay.setContentsMargins(0, 0, 0, 0)
        exp_lay.addWidget(QLabel("Exposición"))
        self.exp_slider = QSlider(Qt.Horizontal)
        self.exp_slider.setRange(1, 5000)
        self.exp_slider.setValue(v4l2_get("exposure_time_absolute") or 300)
        self.exp_slider.valueChanged.connect(self._on_exposure)
        self.btn_exp_auto = QPushButton("Auto")
        self.btn_exp_auto.setCheckable(True)
        self.btn_exp_auto.setChecked(True)
        self.btn_exp_auto.clicked.connect(self._toggle_exp_auto)
        exp_lay.addWidget(self.exp_slider, 1)
        exp_lay.addWidget(self.btn_exp_auto)
        lay.addWidget(self.exp_container)
        self.emeet_widgets.append(self.exp_container)

        # ---- Foco (con auto) ----------------------------------------------
        self.foc_container = QWidget()
        foc_lay = QHBoxLayout(self.foc_container)
        foc_lay.setContentsMargins(0, 0, 0, 0)
        foc_lay.addWidget(QLabel("Foco"))
        self.foc_slider = QSlider(Qt.Horizontal)
        self.foc_slider.setRange(0, 1023)
        self.foc_slider.setValue(v4l2_get("focus_absolute") or 192)
        self.foc_slider.valueChanged.connect(self._on_focus)
        self.btn_foc_auto = QPushButton("Auto")
        self.btn_foc_auto.setCheckable(True)
        self.btn_foc_auto.setChecked(True)
        self.btn_foc_auto.clicked.connect(self._toggle_foc_auto)
        foc_lay.addWidget(self.foc_slider, 1)
        foc_lay.addWidget(self.btn_foc_auto)
        lay.addWidget(self.foc_container)
        self.emeet_widgets.append(self.foc_container)

        # ---- Balance de blancos (con auto) — neutraliza tintes de color -----
        self.wb_container = QWidget()
        wb_lay = QHBoxLayout(self.wb_container)
        wb_lay.setContentsMargins(0, 0, 0, 0)
        wb_lay.addWidget(QLabel("Blancos"))
        self.wb_slider = QSlider(Qt.Horizontal)
        self.wb_slider.setRange(2300, 6500)       # temperatura de color (K)
        self.wb_slider.setValue(v4l2_get("white_balance_temperature") or 5000)
        self.wb_slider.valueChanged.connect(self._on_wb)
        self.btn_wb_auto = QPushButton("Auto")
        self.btn_wb_auto.setCheckable(True)
        self.btn_wb_auto.setChecked(True)
        self.btn_wb_auto.clicked.connect(self._toggle_wb_auto)
        wb_lay.addWidget(self.wb_slider, 1)
        wb_lay.addWidget(self.btn_wb_auto)
        lay.addWidget(self.wb_container)
        self.emeet_widgets.append(self.wb_container)
        self.wb_auto = True


        lay.addSpacing(4)
        lay.addWidget(self._sep())
        lay.addSpacing(2)

        # ---- Efecto + Encuadre ---------------------------------------------
        ef_row = QHBoxLayout()
        ef_row.addWidget(QLabel("Efecto"))
        self.fx_combo = QComboBox()
        for label, _ in EFFECTS:
            self.fx_combo.addItem(label)
        self.fx_combo.currentIndexChanged.connect(self._on_effect)
        ef_row.addWidget(self.fx_combo, 1)
        ef_row.addSpacing(10)
        ef_row.addWidget(QLabel("Enc."))
        self.grid_combo = QComboBox()
        for label in GRID_NAMES:
            self.grid_combo.addItem(label)
        self.grid_combo.setToolTip("Guía de composición (no aparece en la foto ni el vídeo)")
        self.grid_combo.currentIndexChanged.connect(self._on_grid)
        ef_row.addWidget(self.grid_combo, 1)
        lay.addLayout(ef_row)

        # ---- Marca de agua (fecha/hora quemada en vídeo) --------------------
        self.ts_checkbox = QCheckBox("Mostrar fecha y hora en el vídeo")
        self.ts_checkbox.setToolTip("Quema la fecha y hora en la esquina del vídeo y la foto")
        self.ts_checkbox.toggled.connect(self._on_ts_toggle)
        lay.addWidget(self.ts_checkbox)

        self.mirror = False
        self.effect = ""
        self.grid = 0

        lay.addSpacing(4)
        lay.addWidget(self._sep())
        lay.addSpacing(2)

        # ---- Presets -------------------------------------------------------
        self.presets_container = QWidget()
        pre_lay = QHBoxLayout(self.presets_container)
        pre_lay.setContentsMargins(0, 0, 0, 0)
        b_low = QPushButton("🌙 Poca luz")
        b_rst = QPushButton("↺ Reset")
        b_mir = QPushButton("🪞 Espejo")
        b_low.setToolTip("Ajustes para ambientes oscuros")
        b_rst.setToolTip("Restablecer todos los ajustes (0)")
        b_mir.setToolTip("Voltear imagen horizontalmente (M)")
        b_low.clicked.connect(self.preset_lowlight)
        b_rst.clicked.connect(self.preset_reset)
        b_mir.clicked.connect(self._toggle_mirror)
        for b in (b_low, b_rst, b_mir):
            b.setMinimumHeight(34)
            pre_lay.addWidget(b)
        lay.addWidget(self.presets_container)
        self.emeet_widgets.append(self.presets_container)

        # ---- Botón de seguridad (fila completa, independiente) -------------
        self.btn_sec = QPushButton("🔒  Modo Seguridad")
        self.btn_sec.setCheckable(True)
        self.btn_sec.setMinimumHeight(38)
        self.btn_sec.clicked.connect(self._toggle_security)
        lay.addWidget(self.btn_sec)
        self.emeet_widgets.append(self.btn_sec)


        # ---- Panel de ajustes de seguridad (oculto por defecto) ------------
        self.security_panel = QWidget()
        sec_lay = QVBoxLayout(self.security_panel)
        sec_lay.setContentsMargins(8, 8, 8, 4)
        sec_lay.setSpacing(8)
        self.sec_side_status = QLabel("⚫ Seguridad inactiva")
        self.sec_side_status.setAlignment(Qt.AlignCenter)
        self.sec_side_status.setStyleSheet(
            "font-size:16px;font-weight:bold;color:#94a3b8;padding:8px;"
            "background:#111827;border:1px solid #334155;border-radius:6px;")
        sec_lay.addWidget(self.sec_side_status)
        self.sec_sens_slider, _ = self._special_row(
            sec_lay, "Sensibilidad", 1, 100, 25, self._on_sec_sens)
        sec_res_row = QHBoxLayout()
        sec_res_row.addWidget(QLabel("Resolución (igual al modo normal)"))
        self.sec_res_combo = QComboBox()
        for _, _, _, label in RESOLUTIONS:
            self.sec_res_combo.addItem(label)
        self.sec_res_combo.setEnabled(False)
        self.sec_res_combo.setToolTip("Seguridad usa la misma resolución para no reiniciar la cámara")
        sec_res_row.addWidget(self.sec_res_combo, 1)
        sec_lay.addLayout(sec_res_row)
        self.sec_bitrate_slider, _ = self._special_row(
            sec_lay, "Bitrate (Mbps)", 1, 50, 2, lambda v: None, " Mbps")
        self.sec_cooldown_slider, _ = self._special_row(
            sec_lay, "Espera (s)", 3, 30, 10, self._on_sec_cooldown, " s")
        # Micrófono para la grabación de seguridad
        sec_mic_row = QHBoxLayout()
        sec_mic_row.addWidget(QLabel("Audio"))
        self.sec_mic_combo = QComboBox()
        self.sec_mic_combo.addItem("🔇 Sin audio", None)
        for name, label in list_mics():
            self.sec_mic_combo.addItem(label, name)
        sec_mic_row.addWidget(self.sec_mic_combo, 1)
        sec_lay.addLayout(sec_mic_row)

        # Filtro inteligente de personas (NPU/CPU)
        self.sec_npu_check = QCheckBox("👤 Filtro Inteligente de Personas (NPU/CPU)")
        self.sec_npu_check.setChecked(True)
        self.sec_npu_check.toggled.connect(self._on_sec_npu_toggled)
        sec_lay.addWidget(self.sec_npu_check)

        sec_lay.addSpacing(2)
        sec_lay.addWidget(self._sep())
        # Botón para abrir la carpeta de grabaciones
        self.btn_sec_grab = QPushButton("📁 Grabaciones recientes")
        self.btn_sec_grab.setMinimumHeight(34)
        self.btn_sec_grab.clicked.connect(
            lambda: (os.makedirs(SECURITY_DIR, exist_ok=True),
                     subprocess.Popen(["xdg-open", SECURITY_DIR], env=clean_env())))
        sec_lay.addWidget(self.btn_sec_grab)
        self.security_panel.hide()
        lay.addWidget(self.security_panel)
        self.emeet_widgets.append(self.security_panel)

        # ---- Panel de controles Kinect (oculto por defecto) ------------------
        self.kinect_panel = QWidget()
        kin_lay = QVBoxLayout(self.kinect_panel)
        kin_lay.setContentsMargins(0, 0, 0, 0)
        kin_lay.setSpacing(8)

        # 1. Selector de Vista Kinect
        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("Vista Kinect:"))
        self.kin_view_combo = QComboBox()
        self.kin_view_combo.addItems([
            "Color (RGB)",
            "Infrarrojo (IR)",
            "Profundidad (Depth)",
            "Dividido (Split)",
            "Miniatura (PiP)"
        ])
        self.kin_view_combo.currentIndexChanged.connect(self._on_kinect_settings_changed)
        view_row.addWidget(self.kin_view_combo, 1)
        kin_lay.addLayout(view_row)

        # 2. Deslizador de Inclinación (Motor Tilt)
        tilt_row = QHBoxLayout()
        tilt_row.addWidget(QLabel("Inclinación:"))
        self.tilt_slider = QSlider(Qt.Horizontal)
        self.tilt_slider.setRange(-27, 27)
        self.tilt_slider.setValue(0)
        self.tilt_val_lbl = QLabel("0°")
        self.tilt_val_lbl.setMinimumWidth(30)
        self.tilt_val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.tilt_slider.valueChanged.connect(self._on_tilt_changed)
        tilt_row.addWidget(self.tilt_slider, 1)
        tilt_row.addWidget(self.tilt_val_lbl)
        kin_lay.addLayout(tilt_row)

        # 3. Selector de LED
        led_row = QHBoxLayout()
        led_row.addWidget(QLabel("LED Frontal:"))
        self.led_combo = QComboBox()
        self.led_combo.addItem("Apagado", 0)
        self.led_combo.addItem("Verde", 1)
        self.led_combo.addItem("Rojo", 2)
        self.led_combo.addItem("Amarillo", 3)
        self.led_combo.addItem("Parpadeo Verde", 4)
        self.led_combo.addItem("Parpadeo Rojo/Amarillo", 6)
        self.led_combo.currentIndexChanged.connect(self._on_kinect_settings_changed)
        led_row.addWidget(self.led_combo, 1)
        kin_lay.addLayout(led_row)

        # 4. Tracking NPU/GPU
        tracking_row = QHBoxLayout()
        self.tracking_check = QCheckBox("👤 Tracking Corporal (NPU/GPU)")
        self.tracking_check.toggled.connect(self._on_kinect_settings_changed)
        tracking_row.addWidget(self.tracking_check)
        kin_lay.addLayout(tracking_row)

        # Botón para abrir la carpeta de grabaciones Kinect (reutiliza VIDEO_DIR)
        self.btn_kin_grab = QPushButton("📁 Grabaciones Kinect")
        self.btn_kin_grab.setMinimumHeight(34)
        self.btn_kin_grab.clicked.connect(self._open_videos)
        kin_lay.addWidget(self.btn_kin_grab)

        # Registrar panel en la UI
        lay.addWidget(self.kinect_panel)
        self.kinect_panel.hide()


        self.status = QLabel("Listo")
        self.status.setStyleSheet("color: #8aa; padding-top: 4px;")
        lay.addWidget(self.status)
        self._apply_dark_theme()
        self._add_shortcuts()

        # ---- Motor de seguridad -------------------------------------------
        self.security_active = False
        self.security_recording = False
        self._sec_rec_path = ""
        self._sec_last_frame = None
        self._conversions = {}
        self.security_engine = SecurityEngine(self)
        self.security_engine.frame_ready.connect(self._security_frame_cb)
        self.security_engine.motion_detected.connect(self._on_sec_motion)
        self.security_engine.status.connect(self._flash)
        self.sec_npu_check.setText(
            f"👤 Detectar personas ({self.security_engine.backend_name})")
        self.sec_npu_check.setToolTip(
            "Usa la NPU RK3588 cuando está disponible; si falla, usa OpenCV en CPU.")

        # Restaurar ajustes guardados ANTES de arrancar mpv (para usar la última resolución).
        self.settings = QSettings("BIOR", "BiroCam")
        self._migrate_settings()
        self._restore_settings()

        QTimer.singleShot(300, self._initial_device_setup)
        QTimer.singleShot(700, self._recover_interrupted_conversions)

        # Monitor del visor: actualiza el punto de estado cada 1.5 s.
        self._mpv_timer = QTimer(self)
        self._mpv_timer.timeout.connect(self._update_status_dot)
        self._mpv_timer.start(1500)

        # Cronómetro/parpadeo del indicador de grabación.
        self._rec_timer = QTimer(self)
        self._rec_timer.timeout.connect(self._rec_tick)
        self._rec_t0 = 0.0
        self._rec_blink = True

        # Decaimiento suave + arranque del medidor VU para el micro actual.
        self._vu_decay = QTimer(self)
        self._vu_decay.timeout.connect(self._vu_update)
        self._vu_decay.start(60)
        self._restart_vu()



    # ----------------------------------------------------------------- Métodos de Kinect
    def _on_device_changed(self, idx):
        global DEV, CAM_URL, RESOLUTIONS, CAM_INPUT_FORMAT
        dev_type = self.dev_combo.currentData()
        if dev_type == "kinect":
            self._flash("Cambiando a Kinect...")
            # Detener visor de EMEET (mpv)
            if self.mpv_proc and self.mpv_proc.poll() is None:
                mpv_ipc(["quit"])
                try:
                    self.mpv_proc.wait(timeout=1.5)
                except Exception:
                    self.mpv_proc.terminate()
            self.mpv_proc = None

            # Ocultar controles EMEET
            for w in self.emeet_widgets:
                w.hide()

            # Mostrar controles Kinect
            self.kinect_panel.show()
            self.kinect_label.show()

            # Iniciar captura
            self._start_kinect()
        else:
            if not dev_type:
                self._flash("⚠ No se detectó ninguna cámara de captura")
                return
            if hasattr(self, "settings") and DEV:
                self._save_camera_controls()
            DEV = str(dev_type)
            CAM_URL = f"av://v4l2:{DEV}"
            RESOLUTIONS, CAM_INPUT_FORMAT = camera_modes(DEV)
            for combo in (self.res_combo, self.sec_res_combo):
                combo.blockSignals(True)
                combo.clear()
                for _, _, _, label in RESOLUTIONS:
                    combo.addItem(label)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)

            camera_name = self.dev_combo.currentText().removeprefix("🎥 ")
            self._flash(f"Cambiando a {camera_name}...")
            self._stop_kinect()
            self.kinect_label.hide()
            self.kinect_panel.hide()

            if self.mpv_proc and self.mpv_proc.poll() is None:
                mpv_ipc(["quit"])
                try:
                    self.mpv_proc.wait(timeout=1.5)
                except Exception:
                    self.mpv_proc.terminate()
            self.mpv_proc = None

            # Mostrar controles EMEET
            for w in self.emeet_widgets:
                w.show()

            self._load_camera_controls()

            # Lanzar visor
            self.launch_mpv()

    def _start_kinect(self):
        if not self._stop_kinect():
            self._flash("⚠ Kinect anterior aún se está apagando")
            return
        self.kinect_worker = KinectWorker(self)
        self.kinect_worker.frame_ready.connect(self._on_kinect_frame)
        self.kinect_worker.status.connect(self._flash)
        self.kinect_worker.error.connect(lambda msg: (self._flash(f"⚠ {msg}"), QTimer.singleShot(0, lambda: self.dev_combo.setCurrentIndex(0))))

        # Sincronizar inclinación, led y modo
        self.kinect_worker.set_tilt(self.tilt_slider.value())
        self.kinect_worker.set_led(self.led_combo.currentData() or 0)
        view_idx = self.kin_view_combo.currentIndex()
        self.kinect_worker.set_mode("IR" if view_idx == 1 else "RGB")

        self.kinect_worker.start()

    def _stop_kinect(self):
        if hasattr(self, "kinect_worker") and self.kinect_worker:
            worker = self.kinect_worker
            worker.running = False
            worker.requestInterruption()
            if not worker.wait(3000):
                # process_events() pertenece a libfreenect y puede tardar en retornar.
                # Conservar la referencia evita destruir un QThread todavía activo.
                self._flash("⏳ Esperando que libfreenect libere el dispositivo…")
                return False
            self.kinect_worker = None
        return True

    def _on_tilt_changed(self, val):
        self.tilt_val_lbl.setText(f"{val}°")
        if hasattr(self, "kinect_worker") and self.kinect_worker:
            self.kinect_worker.set_tilt(val)

    def _on_kinect_settings_changed(self):
        if hasattr(self, "kinect_worker") and self.kinect_worker:
            view_idx = self.kin_view_combo.currentIndex()
            self.kinect_worker.set_mode("IR" if view_idx == 1 else "RGB")
            self.kinect_worker.set_led(self.led_combo.currentData() or 0)

    def _on_kinect_frame(self, rgb, depth):
        view_idx = self.kin_view_combo.currentIndex()

        # Conversión inicial a BGR detectando dinámicamente si es escala de grises o color
        if len(rgb.shape) == 2 or rgb.shape[2] == 1:
            bgr = cv2.cvtColor(rgb, cv2.COLOR_GRAY2BGR)
        else:
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

        # Profundidad a color (mapa de calor Jet)
        depth_8 = (depth >> 3).astype(np.uint8)
        depth_colored = cv2.applyColorMap(depth_8, cv2.COLORMAP_JET)

        # Layout composiciones
        h, w, _ = bgr.shape
        if view_idx == 2: # Sólo Profundidad
            final_frame = depth_colored
        elif view_idx == 3: # Lado a lado
            final_frame = np.hstack((bgr, depth_colored))
        elif view_idx == 4: # PiP
            final_frame = bgr.copy()
            pip_w, pip_h = w // 3, h // 3
            pip_depth = cv2.resize(depth_colored, (pip_w, pip_h))
            final_frame[h - pip_h - 10:h - 10, w - pip_w - 10:w - 10] = pip_depth
        else:
            final_frame = bgr

        # Filtros (espejo/efectos/rejillas)
        final_frame = self._apply_kinect_filters(final_frame)

        # Reconocimiento corporal (NPU/GPU)
        if self.tracking_check.isChecked():
            final_frame = self._perform_skeleton_tracking(final_frame)

        # Timestamp
        if self.ts_checkbox.isChecked():
            ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(final_frame, ts_str, (10, final_frame.shape[0] - 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)

        # Respaldar frame para foto
        self._last_kinect_frame = final_frame.copy()

        # Guardar si se está grabando
        if self.recording:
            self._write_kinect_frame(final_frame)

        # Mostrar en pantalla
        self._display_kinect_frame(final_frame)

    def _apply_kinect_filters(self, frame):
        # 1. Espejo
        if self.mirror:
            frame = cv2.flip(frame, 1)

        # 2. Efecto de imagen
        effect_idx = self.fx_combo.currentIndex()
        if effect_idx == 1: # B/N
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            frame = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        elif effect_idx == 2: # Sepia
            kernel = np.array([[0.272, 0.534, 0.131],
                               [0.349, 0.686, 0.168],
                               [0.393, 0.769, 0.189]])
            frame = cv2.transform(frame, kernel)
        elif effect_idx == 3: # Vívido
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.float32)
            hsv[:, :, 1] = np.clip(hsv[:, :, 1] * 1.6, 0, 255)
            frame = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
            frame = cv2.convertScaleAbs(frame, alpha=1.08, beta=0)
        elif effect_idx == 4: # Cálido
            frame = cv2.addWeighted(frame, 0.9, np.full(frame.shape, (0, 40, 40), dtype=np.uint8), 0.1, 0)
        elif effect_idx == 5: # Negativo
            frame = 255 - frame

        # 3. Encuadre
        grid_idx = self.grid_combo.currentIndex()
        h, w, _ = frame.shape
        if grid_idx == 1: # Tercios
            cv2.line(frame, (w // 3, 0), (w // 3, h), (255, 255, 255), 1)
            cv2.line(frame, (2 * w // 3, 0), (2 * w // 3, h), (255, 255, 255), 1)
            cv2.line(frame, (0, h // 3), (w, h // 3), (255, 255, 255), 1)
            cv2.line(frame, (0, 2 * h // 3), (w, 2 * h // 3), (255, 255, 255), 1)
        elif grid_idx == 2: # Proporción áurea
            x1, x2 = int(w * 0.382), int(w * 0.618)
            y1, y2 = int(h * 0.382), int(h * 0.618)
            cv2.line(frame, (x1, 0), (x1, h), (255, 255, 255), 1)
            cv2.line(frame, (x2, 0), (x2, h), (255, 255, 255), 1)
            cv2.line(frame, (0, y1), (w, y1), (255, 255, 255), 1)
            cv2.line(frame, (0, y2), (w, y2), (255, 255, 255), 1)
        elif grid_idx == 4: # Cruz
            cv2.line(frame, (w // 2, 0), (w // 2, h), (255, 255, 255), 1)
            cv2.line(frame, (0, h // 2), (w, h // 2), (255, 255, 255), 1)
        elif grid_idx == 5: # Diagonales
            cv2.line(frame, (0, 0), (w, h), (255, 255, 255), 1)
            cv2.line(frame, (w, 0), (0, h), (255, 255, 255), 1)

        return frame

    def _display_kinect_frame(self, frame):
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_frame.shape
        bytes_per_line = ch * w
        qimg = QImage(rgb_frame.data, w, h, bytes_per_line, QImage.Format_RGB888)

        vw, vh = self.video.width(), self.video.height()
        pixmap = QPixmap.fromImage(qimg)
        scaled_pixmap = pixmap.scaled(vw, vh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.kinect_label.setPixmap(scaled_pixmap)
        self.kinect_label.setGeometry(0, 0, vw, vh)

    def _perform_skeleton_tracking(self, frame):
        if not hasattr(self, "_rknn_pose_model") and not hasattr(self, "_mp_pose_model"):
            self._init_pose_model()

        if hasattr(self, "_rknn_pose_model") and self._rknn_pose_model:
            return self._run_npu_pose(frame)
        elif hasattr(self, "_mp_pose_model") and self._mp_pose_model:
            return self._run_mp_pose(frame)
        else:
            cv2.putText(frame, "Buscando Pose Tracker...", (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            h, w, _ = frame.shape
            cv2.circle(frame, (w//2, h//3), 20, (0, 255, 0), -1)
            cv2.line(frame, (w//2, h//3 + 20), (w//2, h//2 + 50), (0, 255, 0), 3)
            cv2.line(frame, (w//2, h//3 + 40), (w//2 - 50, h//2), (0, 255, 0), 3)
            cv2.line(frame, (w//2, h//3 + 40), (w//2 + 50, h//2), (0, 255, 0), 3)
            cv2.line(frame, (w//2, h//2 + 50), (w//2 - 40, h - 80), (0, 255, 0), 3)
            cv2.line(frame, (w//2, h//2 + 50), (w//2 + 40, h - 80), (0, 255, 0), 3)
            return frame

    def _init_pose_model(self):
        # 1. Intentar NPU de Rockchip
        try:
            from rknnlite.api import RKNNLite
            self._rknn_pose_model = RKNNLite()
            rknn_path = os.path.join(APP_DIR, "assets", "yolov8n-pose_rk3588.rknn")
            if os.path.exists(rknn_path):
                ret = self._rknn_pose_model.load_rknn(rknn_path)
                if ret == 0:
                    self._rknn_pose_model.init_runtime(core_mask=RKNNLite.NPU_CORE_AUTO)
                    self._flash("NPU RK3588 Pose Tracker cargado ✓")
                    return
            self._rknn_pose_model = None
        except Exception:
            self._rknn_pose_model = None

        # 2. Intentar MediaPipe GPU/CPU fallback
        try:
            import mediapipe as mp
            self._mp_pose = mp.solutions.pose
            self._mp_draw = mp.solutions.drawing_utils
            self._mp_pose_model = self._mp_pose.Pose(
                static_image_mode=False,
                model_complexity=1,
                min_detection_confidence=0.5
            )
            self._flash("GPU/CPU MediaPipe Pose Tracker cargado ✓")
        except Exception:
            self._mp_pose_model = None

    def _run_mp_pose(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self._mp_pose_model.process(rgb)
        if results.pose_landmarks:
            self._mp_draw.draw_landmarks(
                frame,
                results.pose_landmarks,
                self._mp_pose.POSE_CONNECTIONS,
                self._mp_draw.DrawingSpec(color=(0, 255, 0), thickness=2, circle_radius=2),
                self._mp_draw.DrawingSpec(color=(0, 0, 255), thickness=2)
            )
        return frame

    def _run_npu_pose(self, frame):
        input_size = 640
        h, w, _ = frame.shape
        img_resized = cv2.resize(frame, (input_size, input_size))
        img_rgb = cv2.cvtColor(img_resized, cv2.COLOR_BGR2RGB)
        outputs = self._rknn_pose_model.inference(inputs=[img_rgb])
        try:
            if outputs and len(outputs) >= 1:
                data = outputs[0][0]
                for i in range(data.shape[1]):
                    score = data[4, i]
                    if score > 0.5:
                        for kp in range(17):
                            kx = int(data[5 + kp*3, i] * w / input_size)
                            ky = int(data[5 + kp*3 + 1, i] * h / input_size)
                            kconf = data[5 + kp*3 + 2, i]
                            if kconf > 0.5:
                                cv2.circle(frame, (kx, ky), 5, (0, 255, 0), -1)
        except Exception as e:
            print("Error postprocesamiento NPU:", e)
        return frame

    def _write_kinect_frame(self, frame):
        if hasattr(self, "kinect_writer") and self.kinect_writer:
            try:
                fh, fw, _ = frame.shape
                view_idx = self.kin_view_combo.currentIndex()
                target_w, target_h = (1280, 480) if view_idx == 3 else (640, 480)
                if fw != target_w or fh != target_h:
                    frame = cv2.resize(frame, (target_w, target_h))
                self.kinect_writer.write(frame)
            except Exception as e:
                print("Error escribiendo frame:", e)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "video"):
            vw, vh = self.video.width(), self.video.height()
            if hasattr(self, "security_label"):
                self.security_label.setGeometry(0, 0, vw, vh)
            if hasattr(self, "kinect_label"):
                self.kinect_label.setGeometry(0, 0, vw, vh)
            if hasattr(self, "camera_warmup_label"):
                self.camera_warmup_label.setGeometry(0, 0, vw, vh)
            if hasattr(self, "security_overlay") and self.security_overlay.isVisible():
                bw, bh = 520, 340
                self.security_overlay.setGeometry((vw - bw) // 2, (vh - bh) // 2, bw, bh)

    def _initial_device_setup(self):
        if self.dev_combo.currentData() == "kinect":
            self._on_device_changed(self.dev_combo.currentIndex())
        else:
            self.launch_mpv()

    # ----------------------------------------------------------------- helpers UI
    def _sep(self):

        f = QFrame(); f.setFrameShape(QFrame.HLine); f.setStyleSheet("color:#2a3550;")
        return f

    def _special_row(self, lay, label, lo, hi, val, cb, suffix=""):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        sld = QSlider(Qt.Horizontal); sld.setRange(lo, hi); sld.setValue(val)
        vlab = QLabel(f"{val}{suffix}"); vlab.setMinimumWidth(48)
        vlab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sld.valueChanged.connect(lambda v: (cb(v), vlab.setText(f"{v}{suffix}")))
        row.addWidget(sld, 1); row.addWidget(vlab)
        lay.addLayout(row)
        return sld, vlab

    def _on_slider(self, cid, value, vlab):
        v4l2_set(cid, value); vlab.setText(str(value))

    def _on_zoom(self, v):
        # Zoom digital por software de mpv (el control v4l2 de esta cámara no hace nada).
        # 0-100% -> video-zoom 0..1.585 (log2) = 1x..3x.
        mpv_ipc(["set_property", "video-zoom", round((v / 100.0) * 1.585, 3)])

    def _on_bitrate(self, v):
        self.bitrate_value_label.setText(f"{v} Mbps")

    def _apply_photo_settings(self):
        idx = self.photo_quality_combo.currentIndex()
        _, _, qual = PHOTO_QUALITY[idx]
        mpv_ipc(["set_property", "screenshot-jpeg-quality", qual])

    def _ensure_shutter_sound(self):
        if not os.path.exists(SHUTTER_SOUND):
            try:
                os.makedirs(os.path.dirname(SHUTTER_SOUND), exist_ok=True)
                sample_rate = 22050
                duration = 0.04
                num = int(sample_rate * duration)
                data = bytearray()
                for i in range(num):
                    t = i / sample_rate
                    amp = int(32767 * math.exp(-t * 180) * math.sin(2 * math.pi * 900 * t))
                    data += struct.pack('<h', amp)
                with wave.open(SHUTTER_SOUND, 'w') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(sample_rate)
                    wav.writeframes(data)
            except Exception:
                pass

    def _play_shutter(self):
        if self._shutter_enabled and os.path.exists(SHUTTER_SOUND):
            try:
                subprocess.Popen(["paplay", SHUTTER_SOUND],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=clean_env())
            except FileNotFoundError:
                pass

    def _apply_view_state(self):
        # Re-aplica zoom y la cadena de filtros (espejo+efecto+encuadre): todo se
        # resetea al (re)arrancar o recargar mpv.
        self._on_zoom(self.zoom_slider.value())
        self._apply_vf()

    def _apply_auto_settings(self):
        v4l2_set("exposure_auto_priority", 0)
        v4l2_set("auto_exposure", 3 if self.exposure_auto else 1)
        v4l2_set("focus_automatic_continuous", 1 if self.focus_auto else 0)
        v4l2_set("white_balance_automatic", 1 if self.wb_auto else 0)

    def _begin_camera_warmup(self, launch_id):
        """Oculta cuadros negros mientras RGB recupera exposición tras usar IR."""
        self.camera_warmup_label.setGeometry(0, 0, self.video.width(), self.video.height())
        self.camera_warmup_label.show()
        self.camera_warmup_label.raise_()
        self.btn_photo.setEnabled(False)
        if not self.recording:
            self.btn_rec.setEnabled(False)
        self._flash("Ajustando exposición de la cámara…")
        QTimer.singleShot(4200, lambda: self._finish_camera_warmup(launch_id))

    def _finish_camera_warmup(self, launch_id):
        if launch_id != self._camera_launch_id:
            return
        self.camera_warmup_label.hide()
        self.btn_photo.setEnabled(True)
        self.btn_rec.setEnabled(True)
        self._flash("Cámara lista")

    def _apply_auto_settings_if_current(self, launch_id):
        if launch_id == self._camera_launch_id:
            self._apply_auto_settings()

    # ----------------------------------------------------------------- filtros: espejo/efecto/encuadre
    def _grid_filters(self, idx):
        """Devuelve los filtros lavfi para el encuadre (guía de composición)."""
        if idx == 1:        # tercios
            xs, ys = ("iw/3", "2*iw/3"), ("ih/3", "2*ih/3")
            boxes = [f"drawbox=x={x}-1:y=0:w=2:h=ih:color=white@0.5:t=fill" for x in xs]
            boxes += [f"drawbox=x=0:y={y}-1:w=iw:h=2:color=white@0.5:t=fill" for y in ys]
            return ",".join(boxes)
        if idx == 2:        # proporción áurea (0.382 / 0.618)
            xs, ys = ("iw*0.382", "iw*0.618"), ("ih*0.382", "ih*0.618")
            boxes = [f"drawbox=x={x}-1:y=0:w=2:h=ih:color=white@0.5:t=fill" for x in xs]
            boxes += [f"drawbox=x=0:y={y}-1:w=iw:h=2:color=white@0.5:t=fill" for y in ys]
            return ",".join(boxes)
        if idx == 3:        # espiral de Fibonacci (rectángulos anidados en φ)
            boxes = [
                "drawbox=x=iw*0.382-1:y=0:w=2:h=ih:color=white@0.5:t=fill",
                "drawbox=x=iw*0.618-1:y=0:w=2:h=ih:color=white@0.5:t=fill",
                "drawbox=x=0:y=ih*0.382-1:w=iw:h=2:color=white@0.5:t=fill",
                "drawbox=x=0:y=ih*0.618-1:w=iw:h=2:color=white@0.5:t=fill",
                "drawbox=x=iw*0.764-1:y=ih*0.618:w=2:h=ih*0.382:color=white@0.5:t=fill",
                "drawbox=x=iw*0.618:y=ih*0.764-1:w=iw*0.382:h=2:color=white@0.5:t=fill",
                "drawbox=x=iw*0.854-1:y=ih*0.764:w=2:h=ih*0.236:color=white@0.5:t=fill",
                "drawbox=x=iw*0.764:y=ih*0.854-1:w=iw*0.236:h=2:color=white@0.5:t=fill",
                "drawbox=x=iw*0.910-1:y=ih*0.854:w=2:h=ih*0.146:color=white@0.5:t=fill",
                "drawbox=x=iw*0.854:y=ih*0.910-1:w=iw*0.146:h=2:color=white@0.5:t=fill",
            ]
            return ",".join(boxes)
        if idx == 4:        # cruz centrada
            return ("drawbox=x=iw/2-1:y=0:w=2:h=ih:color=white@0.5:t=fill,"
                    "drawbox=x=0:y=ih/2-1:w=iw:h=2:color=white@0.5:t=fill")
        if idx == 5:        # diagonales via geq
            return ("geq=lum='if(lt(abs(Y*W-X*H),2*sqrt(W*W+H*H))"
                    "|lt(abs(Y*W+X*H-W*H),2*sqrt(W*W+H*H)),255,p(X,Y))':"
                    "cr='if(lt(abs(Y*W-X*H),2*sqrt(W*W+H*H))"
                    "|lt(abs(Y*W+X*H-W*H),2*sqrt(W*W+H*H)),128,128)':"
                    "cb='if(lt(abs(Y*W-X*H),2*sqrt(W*W+H*H))"
                    "|lt(abs(Y*W+X*H-W*H),2*sqrt(W*W+H*H)),128,128)'")
        return ""

    def _vf_chain(self, with_grid=True):
        parts = []
        if self.mirror:
            parts.append("hflip")
        if self.effect:
            parts.append(self.effect)
        if self.ts_checkbox.isChecked():
            parts.append("drawtext=text='%{localtime}':fontsize=48:fontcolor=white:x=w-tw-10:y=15:box=1:boxcolor=black@0.5")
        if with_grid and self.grid:
            parts.append(self._grid_filters(self.grid))
        return ("lavfi=[" + ",".join(parts) + "]") if parts else ""

    def _apply_vf(self):
        mpv_ipc(["set_property", "vf", self._vf_chain()])

    def _toggle_mirror(self):
        self.mirror = not self.mirror
        self._apply_vf()
        self._flash("🪞 Espejo " + ("ON" if self.mirror else "OFF"))

    def _on_effect(self, idx):
        self.effect = EFFECTS[idx][1]
        self._apply_vf()
        self._flash("Efecto: " + EFFECTS[idx][0])

    def _on_grid(self, idx):
        self.grid = idx
        self._apply_vf()

    def _on_ts_toggle(self):
        self._apply_vf()
        self._flash("📅 Timestamp " + ("ON" if self.ts_checkbox.isChecked() else "OFF"))

    def changeEvent(self, event):
        # Al volver a la ventana (reactivarla), refresca el stream para quitar el
        # retraso acumulado de la cámara en vivo mientras estuvo oculta.
        # NO se hace durante la grabación (recargar la rompería).
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            if self._started and not self.recording \
               and self.mpv_proc and self.mpv_proc.poll() is None:
                QTimer.singleShot(120, self._refresh_stream)
        super().changeEvent(event)

    def _refresh_stream(self):
        if self.recording:
            return
        mpv_ipc(["loadfile", CAM_URL, "replace"])
        QTimer.singleShot(800, self._apply_view_state)

    def _on_exposure(self, v):
        if self.exposure_auto:
            self.exposure_auto = False
            self.btn_exp_auto.setChecked(False)
            v4l2_set("auto_exposure", 1)  # 1 = Manual Mode
        v4l2_set("exposure_time_absolute", v)

    def _toggle_exp_auto(self):
        self.exposure_auto = self.btn_exp_auto.isChecked()
        v4l2_set("auto_exposure", 3 if self.exposure_auto else 1)  # 3 = Aperture Priority
        self._flash("Exposición " + ("AUTO" if self.exposure_auto else "MANUAL"))

    def _on_focus(self, v):
        if self.focus_auto:
            self.focus_auto = False
            self.btn_foc_auto.setChecked(False)
            v4l2_set("focus_automatic_continuous", 0)
        v4l2_set("focus_absolute", v)

    def _toggle_foc_auto(self):
        self.focus_auto = self.btn_foc_auto.isChecked()
        v4l2_set("focus_automatic_continuous", 1 if self.focus_auto else 0)
        if not self.focus_auto:
            v4l2_set("focus_absolute", self.foc_slider.value())
        self._flash("Foco " + ("AUTO" if self.focus_auto else "MANUAL"))

    def _on_wb(self, v):
        if self.wb_auto:                              # pasar a manual al tocar el slider
            self.wb_auto = False
            self.btn_wb_auto.setChecked(False)
            v4l2_set("white_balance_automatic", 0)
        v4l2_set("white_balance_temperature", v)

    def _toggle_wb_auto(self):
        self.wb_auto = self.btn_wb_auto.isChecked()
        v4l2_set("white_balance_automatic", 1 if self.wb_auto else 0)
        if not self.wb_auto:
            v4l2_set("white_balance_temperature", self.wb_slider.value())
        self._flash("Blancos " + ("AUTO" if self.wb_auto else "MANUAL"))

    def _flash(self, msg):
        self.status.setText(msg)

    # ----------------------------------------------------------------- atajos
    def _add_shortcuts(self):
        binds = {
            "Space": self.take_photo, "S": self.take_photo,
            "R": self.toggle_record, "F": self.btn_foc_auto.click,
            "M": self._toggle_mirror,
            "G": lambda: subprocess.Popen(["xdg-open", PHOTO_DIR], env=clean_env()),
            "V": lambda: subprocess.Popen(["xdg-open", VIDEO_DIR], env=clean_env()),
            "T": lambda: self.ts_checkbox.setChecked(not self.ts_checkbox.isChecked()),
            "0": self.preset_reset,
            "+": lambda: self._bump_zoom(10), "=": lambda: self._bump_zoom(10),
            "-": lambda: self._bump_zoom(-10),
        }
        for key, fn in binds.items():
            QShortcut(QKeySequence(key), self, activated=fn)

    def _bump_zoom(self, delta):
        self.zoom_slider.setValue(max(0, min(100, self.zoom_slider.value() + delta)))

    def _open_videos(self):
        os.makedirs(VIDEO_DIR, exist_ok=True)
        subprocess.Popen(["xdg-open", VIDEO_DIR], env=clean_env())

    def _set_locked(self, locked):
        # Bloquea (visualmente atenuado + 🔒) controles de grabación.
        self.res_box.setEnabled(not locked)
        self.mic_box.setEnabled(not locked)
        self.bitrate_box.setEnabled(not locked)
        self.dur_codec_box.setEnabled(not locked)
        self.res_label.setText("Resolución  🔒" if locked else "Resolución")
        self.mic_label.setText("Micrófono  🔒" if locked else "Micrófono")
        self.bitrate_label.setText("Bitrate  🔒" if locked else "Bitrate")
        self.dur_label.setText("Duración  🔒" if locked else "Duración")
        self.codec_label.setText("Códec  🔒" if locked else "Códec")
        for box in (self.res_box, self.mic_box, self.bitrate_box,
                    self.dur_codec_box):
            box.setProperty("locked", locked)
            box.style().unpolish(box); box.style().polish(box)

    # ----------------------------------------------------------------- indicador REC
    def _rec_tick(self):
        elapsed = int(time.monotonic() - self._rec_t0)
        self._rec_blink = not self._rec_blink
        dot = "●" if self._rec_blink else "　"
        self.rec_label.setText(f"{dot} REC  {elapsed // 60:02d}:{elapsed % 60:02d}")
        # Auto-detener por límite de duración
        max_min = getattr(self, "_rec_max_duration", 0)
        if max_min > 0 and elapsed >= max_min * 60:
            self.toggle_record()

    # ----------------------------------------------------------------- medidor VU
    def _restart_vu(self):
        if self._vu_proc:
            self._vu_proc.kill()
            self._vu_proc = None
        self._vu_level = 0.0
        self.vu.setValue(0)
        mic = self.mic_combo.currentData()
        if not mic:
            return
        self._vu_proc = QProcess(self)
        self._vu_proc.readyReadStandardOutput.connect(self._vu_read)
        self._vu_proc.start("ffmpeg", [
            "-hide_banner", "-loglevel", "quiet", "-f", "pulse", "-i", mic,
            "-ac", "1", "-ar", "8000", "-f", "s16le", "-"])

    def _vu_read(self):
        # RMS del bloque MÁS RECIENTE (refleja el nivel actual, no el máximo histórico).
        data = bytes(self._vu_proc.readAllStandardOutput())
        n = (len(data) // 2) * 2
        if n < 2:
            return
        try:
            samples = array.array('h', data[:n])
            if len(samples) > 0:
                sum_sq = sum(s * s for s in samples)
                self._vu_level = math.sqrt(sum_sq / len(samples)) / 32768.0
        except Exception:
            pass

    def _vu_update(self):
        # ataque rápido (sube al instante), caída suave (baja sola al callar).
        target = min(100, int(self._vu_level * 320))
        cur = self.vu.value()
        self.vu.setValue(target if target >= cur else int(cur * 0.78))

    @staticmethod
    def _start_audio_capture(source, path):
        """Inicia PulseAudio y confirma que FFmpeg siga vivo antes de anunciar audio."""
        if not source:
            return None, ""
        try:
            # Comprobar si la fuente existe en PulseAudio/PipeWire
            try:
                info = subprocess.run(
                    ["pactl", "list", "short", "sources"],
                    capture_output=True, text=True, timeout=3, env=clean_env())
                if info.returncode == 0 and source not in info.stdout:
                    return None, f"fuente no disponible: {source}"
            except (OSError, subprocess.SubprocessError):
                # Si pactl no existe o falla, dejamos que ffmpeg lo intente directamente
                pass

            proc = subprocess.Popen([
                "/usr/bin/ffmpeg", "-nostdin", "-y", "-loglevel", "warning",
                "-thread_queue_size", "512", "-f", "pulse", "-i", source,
                "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", path,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=clean_env())
            time.sleep(0.18)
            if proc.poll() is not None:
                err_detail = ""
                try:
                    err_detail = proc.stderr.read().decode("utf-8", "replace").strip()
                except Exception:
                    pass
                return None, err_detail[-240:] or "FFmpeg no pudo abrir el micrófono"
            return proc, ""
        except (OSError, subprocess.SubprocessError) as exc:
            return None, str(exc)

    @staticmethod
    def _stop_audio_capture(proc):
        """Finaliza WAV correctamente; terminate solo como último recurso."""
        if not proc or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    @staticmethod
    def _media_is_valid(path, require_audio=False):
        """Valida contenedor y streams; el tamaño por sí solo no basta."""
        try:
            probe = subprocess.run([
                "/usr/bin/ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,duration:format=duration", "-of", "json", path,
            ], capture_output=True, text=True, timeout=15, env=clean_env())
            if probe.returncode != 0:
                return False, "ffprobe no pudo leer el archivo"
            data = json.loads(probe.stdout)
            streams = data.get("streams", [])
            has_video = any(s.get("codec_type") == "video" for s in streams)
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            duration = float(data.get("format", {}).get("duration") or 0)
            if not has_video or duration <= 0.1:
                return False, "MP4 sin vídeo reproducible"
            if require_audio and not has_audio:
                return False, "se solicitó audio pero el MP4 no lo contiene"
            return True, ""
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            return False, str(exc)

    @staticmethod
    def _media_duration(path):
        try:
            probe = subprocess.run([
                "/usr/bin/ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "default=nw=1:nk=1", path,
            ], capture_output=True, text=True, timeout=15, env=clean_env())
            return float(probe.stdout.strip()) if probe.returncode == 0 else 0.0
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0.0

    # ----------------------------------------------------------------- persistencia
    def _migrate_settings(self):
        old = QSettings("CarlosOPi", "CamaraS600")
        if old.allKeys():
            # Solo migrar si las nuevas settings están vacías
            if not self.settings.allKeys():
                for k in old.allKeys():
                    self.settings.setValue(k, old.value(k))
                old.clear()
                self._flash("↩ Ajustes migrados de versión anterior")

    @staticmethod
    def _camera_settings_id(device=None):
        path = str(device or DEV or "none")
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", path).strip("_")

    def _save_camera_controls(self):
        if not DEV or not hasattr(self, "settings"):
            return
        prefix = f"camera/{self._camera_settings_id()}/ctl"
        for cid, slider in self.sliders.items():
            self.settings.setValue(f"{prefix}/{cid}", slider.value())

    def _load_camera_controls(self):
        if not DEV or not hasattr(self, "settings"):
            return
        details = v4l2_control_details()
        prefix = f"camera/{self._camera_settings_id()}/ctl"
        is_emeet = "emeet" in self.dev_combo.currentText().lower()
        for cid, slider in self.sliders.items():
            if cid not in details:
                slider.setEnabled(False)
                continue
            slider.setEnabled(True)
            lo, hi, step, default, current = details[cid]
            slider.blockSignals(True)
            slider.setRange(lo, hi)
            slider.setSingleStep(max(1, step))
            key = f"{prefix}/{cid}"
            legacy_key = f"ctl/{cid}"
            if self.settings.contains(key):
                value = int(self.settings.value(key))
            elif is_emeet and self.settings.contains(legacy_key):
                value = int(self.settings.value(legacy_key))
            else:
                value = default
            value = max(lo, min(hi, value))
            slider.setValue(value)
            self.value_labels[cid].setText(str(value))
            slider.blockSignals(False)
            v4l2_set(cid, value)

    def _restore_settings(self):
        s = self.settings
        if s.contains("resolution"):
            ridx = int(s.value("resolution"))
            if 0 <= ridx < len(RESOLUTIONS):
                self.res_combo.setCurrentIndex(ridx)
        self._load_camera_controls()
        if s.contains("zoom"):
            self.zoom_slider.setValue(int(s.value("zoom")))
        if s.contains("mic"):
            i = self.mic_combo.findData(s.value("mic"))
            if i >= 0:
                self.mic_combo.setCurrentIndex(i)
        if s.contains("bitrate"):
            self.bitrate_slider.setValue(int(s.value("bitrate")))
        if s.contains("photo_quality"):
            self.photo_quality_combo.setCurrentIndex(int(s.value("photo_quality")))
        if s.contains("photo_timer"):
            self.timer_combo.setCurrentIndex(int(s.value("photo_timer")))
        if s.contains("timestamp"):
            self.ts_checkbox.setChecked(s.value("timestamp") == "true")
        if s.contains("codec"):
            self.codec_combo.setCurrentIndex(int(s.value("codec")))
        if s.contains("dur"):
            self.dur_combo.setCurrentIndex(int(s.value("dur")))
        if s.contains("mirror"):
            self.mirror = s.value("mirror") == "true"
        if s.contains("effect"):
            self.fx_combo.setCurrentIndex(int(s.value("effect")))
        if s.contains("grid"):
            self.grid_combo.setCurrentIndex(int(s.value("grid")))
        if s.contains("exp_auto"):
            self.exposure_auto = s.value("exp_auto") == "true"
            self.btn_exp_auto.setChecked(self.exposure_auto)
        if s.contains("foc_auto"):
            self.focus_auto = s.value("foc_auto") == "true"
            self.btn_foc_auto.setChecked(self.focus_auto)
        if s.contains("wb_auto"):
            self.wb_auto = s.value("wb_auto") == "true"
            self.btn_wb_auto.setChecked(self.wb_auto)
        if s.contains("shutter"):
            self.shutter_check.setChecked(s.value("shutter") == "true")
        # Seguridad
        if s.contains("sec_sens"):
            self.sec_sens_slider.setValue(int(s.value("sec_sens")))
        # Una sola resolución mantiene una sola conexión UVC y evita fallos DMA.
        self.sec_res_combo.setCurrentIndex(self.res_combo.currentIndex())
        if s.contains("sec_bitrate"):
            self.sec_bitrate_slider.setValue(int(s.value("sec_bitrate")))
        if s.contains("sec_cooldown"):
            self.sec_cooldown_slider.setValue(int(s.value("sec_cooldown")))
        if s.contains("sec_mic"):
            i = self.sec_mic_combo.findData(s.value("sec_mic"))
            if i >= 0:
                self.sec_mic_combo.setCurrentIndex(i)
        if s.contains("sec_npu"):
            self.sec_npu_check.setChecked(s.value("sec_npu") == "true")
        # Geometría de ventana
        if s.contains("window_geometry"):
            try:
                parts = [int(x) for x in s.value("window_geometry").split(",")]
                if len(parts) == 4:
                    self.setGeometry(*parts)
            except Exception:
                pass
        if s.contains("window_maximized") and s.value("window_maximized") == "true":
            QTimer.singleShot(100, self.showMaximized)

        # Ajustes de Kinect
        if s.contains("kinect_view"):
            self.kin_view_combo.setCurrentIndex(int(s.value("kinect_view")))
        if s.contains("kinect_tilt"):
            self.tilt_slider.setValue(int(s.value("kinect_tilt")))
        if s.contains("kinect_led"):
            # Buscar el índice correspondiente al valor guardado
            val = int(s.value("kinect_led"))
            idx = self.led_combo.findData(val)
            if idx >= 0:
                self.led_combo.setCurrentIndex(idx)
        if s.contains("kinect_tracking"):
            self.tracking_check.setChecked(s.value("kinect_tracking") == "true")
        if s.contains("device_selector"):
            dev_idx = self.dev_combo.findData(s.value("device_selector"))
            if dev_idx >= 0:
                self.dev_combo.blockSignals(True)
                self.dev_combo.setCurrentIndex(dev_idx)
                self.dev_combo.blockSignals(False)

    def _save_settings(self):
        s = self.settings
        s.setValue("resolution", self.res_combo.currentIndex())
        self._save_camera_controls()
        s.setValue("zoom", self.zoom_slider.value())
        s.setValue("mic", self.mic_combo.currentData() or "")
        s.setValue("bitrate", self.bitrate_slider.value())
        s.setValue("photo_quality", self.photo_quality_combo.currentIndex())
        s.setValue("photo_timer", self.timer_combo.currentIndex())
        s.setValue("timestamp", "true" if self.ts_checkbox.isChecked() else "false")
        s.setValue("codec", self.codec_combo.currentIndex())
        s.setValue("dur", self.dur_combo.currentIndex())
        s.setValue("mirror", "true" if self.mirror else "false")
        s.setValue("effect", self.fx_combo.currentIndex())
        s.setValue("grid", self.grid_combo.currentIndex())
        s.setValue("exp_auto", "true" if self.exposure_auto else "false")
        s.setValue("foc_auto", "true" if self.focus_auto else "false")
        s.setValue("wb_auto", "true" if self.wb_auto else "false")
        s.setValue("shutter", "true" if self.shutter_check.isChecked() else "false")
        # Seguridad
        s.setValue("sec_sens", self.sec_sens_slider.value())
        s.setValue("sec_res", self.sec_res_combo.currentIndex())
        s.setValue("sec_bitrate", self.sec_bitrate_slider.value())
        s.setValue("sec_cooldown", self.sec_cooldown_slider.value())
        s.setValue("sec_mic", self.sec_mic_combo.currentData() or "")
        s.setValue("sec_npu", "true" if self.sec_npu_check.isChecked() else "false")

        # Guardar ajustes de Kinect
        s.setValue("device_selector", self.dev_combo.currentData() or "")
        s.setValue("kinect_view", self.kin_view_combo.currentIndex())
        s.setValue("kinect_tilt", self.tilt_slider.value())
        s.setValue("kinect_led", self.led_combo.currentData() or 0)
        s.setValue("kinect_tracking", "true" if self.tracking_check.isChecked() else "false")

        # Geometría de ventana
        if not self.isMaximized():
            s.setValue("window_geometry",
                       f"{self.x()},{self.y()},{self.width()},{self.height()}")
        s.setValue("window_maximized", "true" if self.isMaximized() else "false")

    def _update_status_dot(self):
        ok = False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.3); s.connect(IPC_SOCK); s.close(); ok = True
        except OSError:
            ok = False
        self.dot.setObjectName("dot_on" if ok else "dot_off")
        self.dot.setToolTip("Visor conectado" if ok else "Visor desconectado")
        self.dot.style().unpolish(self.dot); self.dot.style().polish(self.dot)

    # ----------------------------------------------------------------- acciones
    def launch_mpv(self):
        """Incrusta el visor mpv dentro del widget de vídeo (--wid)."""
        w, h, fps, _ = RESOLUTIONS[self.res_combo.currentIndex()]
        wid = int(self.video.winId())   # ID de ventana nativa del widget de vídeo
        args = [
            mpv_executable(), CAM_URL,
            f"--wid={wid}",                       # render DENTRO de la app
            f"--input-ipc-server={IPC_SOCK}",
            "--no-config",                        # aislado de ~/.config/mpv (sin lua/HUD)
            # Baja latencia manual: el perfil 'low-latency' desactiva la caché y deja la
            # grabación (stream-record) vacía. Con caché mínima hay baja latencia Y graba.
            "--cache=yes", "--demuxer-readahead-secs=0.05", "--cache-secs=0.1",
            "--cache-pause=no", "--framedrop=vo", "--video-sync=display-desync",
            "--video-latency-hacks=yes",
            "--hwdec=no",                         # software MJPEG -> nunca toca el RGA
            "--vo=gpu", "--gpu-context=x11egl",    # X11 para honrar --wid (NO wayland)
            "--demuxer-lavf-o=" + f"video_size={w}x{h},input_format={CAM_INPUT_FORMAT},framerate={fps}",
            "--audio=no",                         # sin audio en el visor (se graba aparte)
            "--no-osc", "--osd-level=0", "--really-quiet",
            "--screenshot-directory=" + PHOTO_DIR,
            "--screenshot-format=jpg", "--screenshot-sw=yes",
        ]
        # Sin WAYLAND_DISPLAY, mpv usa X11/XWayland y respeta el --wid (si no, elige
        # Wayland, ignora el wid y pinta fuera del widget -> área de vídeo en negro).
        env = clean_env()
        env.pop("WAYLAND_DISPLAY", None)
        try:
            # Evita que la autoexposición reduzca los FPS en poca luz (si la UVC lo soporta).
            v4l2_set("exposure_auto_priority", 0)
            self.mpv_proc = subprocess.Popen(args, env=env)
            self._camera_launch_id = getattr(self, "_camera_launch_id", 0) + 1
            launch_id = self._camera_launch_id
            self._begin_camera_warmup(launch_id)
            QTimer.singleShot(1200, self._apply_view_state)
            QTimer.singleShot(2500, lambda: setattr(self, "_started", True))
            QTimer.singleShot(3000, lambda: self._apply_auto_settings_if_current(launch_id))
        except FileNotFoundError:
            self._flash("ERROR: mpv no está instalado ni incluido en la AppImage")

    def take_photo(self):
        if self._photo_timer_active:
            return
        t = self.timer_combo.currentText()
        if t == "Ahora":
            self._do_take_photo()
        else:
            seconds = int(t.replace("s", ""))
            self._photo_timer_active = True
            self.btn_photo.setEnabled(False)
            self._photo_countdown = seconds
            self.status.setText(f"⏱ Foto en {seconds}…")
            self._photo_timer = QTimer(self)
            self._photo_timer.timeout.connect(self._photo_tick)
            self._photo_timer.start(1000)

    def _photo_tick(self):
        self._photo_countdown -= 1
        if self._photo_countdown <= 0:
            self._photo_timer.stop()
            self._photo_timer = None
            self._photo_timer_active = False
            self.btn_photo.setEnabled(True)
            self._do_take_photo()
        else:
            self.status.setText(f"⏱ Foto en {self._photo_countdown}…")

    def _do_take_photo(self):
        self._apply_photo_settings()
        self._play_shutter()

        if self.dev_combo.currentData() == "kinect":
            if hasattr(self, "_last_kinect_frame") and self._last_kinect_frame is not None:
                ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                idx = self.photo_quality_combo.currentIndex()
                _, _, qual = PHOTO_QUALITY[idx]
                photo_path = os.path.join(PHOTO_DIR, f"Foto_{ts}.jpg")
                cv2.imwrite(photo_path, self._last_kinect_frame, [cv2.IMWRITE_JPEG_QUALITY, qual])
                self._flash("📷 Foto Kinect guardada")
            else:
                self._flash("⚠ No hay frame disponible")
            return

        if self.grid:
            mpv_ipc(["set_property", "vf", self._vf_chain(with_grid=False)])
            QTimer.singleShot(140, self._snap_and_restore)
        else:
            mpv_ipc(["screenshot", "video"])
            self._flash("📷 Foto guardada")

    def _snap_and_restore(self):
        mpv_ipc(["screenshot", "video"])
        self._flash("📷 Foto guardada")
        QTimer.singleShot(60, self._apply_vf)   # restaurar la rejilla

    def toggle_record(self):
        if self.dev_combo.currentData() == "kinect":
            self._toggle_record_kinect()
            return

        if self.recording:
            # 1) parar vídeo (mpv) y audio (ffmpeg) ---------------------------
            mpv_ipc_result(["set_property", "stream-record", ""])
            self.recording = False
            self.btn_rec.setText("⏺ Grabar")
            self._set_locked(False)               # desbloquear controles
            self._rec_timer.stop()                # parar cronómetro
            self.rec_label.setVisible(False)
            self._stop_audio_capture(self._audio_proc)
            self._flash("Procesando vídeo…")
            QTimer.singleShot(800, self._finish_recording)  # deja cerrar los archivos
        else:
            # 2) arrancar vídeo + (opcional) audio del micro elegido ----------
            os.makedirs(VIDEO_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self._last_video = os.path.join(VIDEO_DIR, f"Video_{ts}.mkv")
            self._audio_wav = ""
            self._audio_proc = None
            mic = self.mic_combo.currentData()    # nombre de la fuente o None
            self._rec_effect = self.effect        # efecto fijado para esta grabación
            self._rec_bitrate = self.bitrate_slider.value()
            self._rec_timestamp = self.ts_checkbox.isChecked()
            self._rec_codec = CODECS[self.codec_combo.currentIndex()][1]
            self._rec_max_duration = REC_DURATIONS[self.dur_combo.currentIndex()][1]
            video_t0 = time.monotonic()
            reply = mpv_ipc_result(["set_property", "stream-record", self._last_video])
            if reply.get("error") != "success":
                self._flash(f"⚠ No se pudo iniciar vídeo: {reply.get('error')}")
                return
            self._audio_offset = 0.0
            audio_error = ""
            if mic:
                self._audio_wav = os.path.join(VIDEO_DIR, f"Video_{ts}.wav")
                self._audio_proc, audio_error = self._start_audio_capture(
                    mic, self._audio_wav)
                self._audio_offset = max(0.0, time.monotonic() - video_t0)
                if not self._audio_proc:
                    self._audio_wav = ""
            self.recording = True
            self.btn_rec.setText("⏹ Detener")
            self._set_locked(True)                # bloquear (atenuado + 🔒)
            self._rec_t0 = time.monotonic()       # iniciar cronómetro
            self._rec_blink = True
            self.rec_label.setVisible(True)
            self._rec_tick()
            self._rec_timer.start(500)
            self._play_shutter()
            if self._audio_proc:
                self._flash("● Grabando + audio 🎙")
            elif mic:
                self._flash(f"● Grabando SIN audio · {audio_error}")
            else:
                self._flash("● Grabando (sin audio)")

    def _toggle_record_kinect(self):
        if self.recording:
            # Detener grabación
            self.recording = False
            if hasattr(self, "kinect_writer") and self.kinect_writer:
                try:
                    self.kinect_writer.release()
                except Exception:
                    pass
                self.kinect_writer = None
            self.btn_rec.setText("⏺ Grabar")
            self._set_locked(False)
            self._rec_timer.stop()
            self.rec_label.setVisible(False)
            self._stop_audio_capture(self._audio_proc)
            self._flash("Procesando vídeo Kinect…")
            QTimer.singleShot(800, self._finish_recording)
        else:
            # Iniciar grabación
            os.makedirs(VIDEO_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self._last_video = os.path.join(VIDEO_DIR, f"Video_{ts}.mkv")
            self._audio_wav = ""
            self._audio_proc = None

            # Inicializar VideoWriter
            view_idx = self.kin_view_combo.currentIndex()
            target_w, target_h = (1280, 480) if view_idx == 3 else (640, 480)
            fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            self.kinect_writer = cv2.VideoWriter(self._last_video, fourcc, 30.0, (target_w, target_h))

            mic = self.mic_combo.currentData()
            self._rec_effect = ""  # Los efectos ya se queman en vivo vía OpenCV
            self._rec_bitrate = self.bitrate_slider.value()
            self._rec_timestamp = False  # Ya se quema en vivo si ts_checkbox está marcado
            self._rec_codec = CODECS[self.codec_combo.currentIndex()][1]
            self._rec_max_duration = REC_DURATIONS[self.dur_combo.currentIndex()][1]
            video_t0 = time.monotonic()

            self._audio_offset = 0.0
            audio_error = ""
            if mic:
                self._audio_wav = os.path.join(VIDEO_DIR, f"Video_{ts}.wav")
                self._audio_proc, audio_error = self._start_audio_capture(mic, self._audio_wav)
                self._audio_offset = max(0.0, time.monotonic() - video_t0)
                if not self._audio_proc:
                    self._audio_wav = ""

            self.recording = True
            self.btn_rec.setText("⏹ Detener")
            self._set_locked(True)
            self._rec_t0 = time.monotonic()
            self._rec_blink = True
            self.rec_label.setVisible(True)
            self._rec_tick()
            self._rec_timer.start(500)
            self._play_shutter()
            if self._audio_proc:
                self._flash("● Grabando Kinect + audio 🎙")
            elif mic:
                self._flash(f"● Grabando Kinect SIN audio · {audio_error}")
            else:
                self._flash("● Grabando Kinect (sin audio)")


    def _finish_recording(self):
        """Convierte la grabación a MP4 (vídeo+audio+efecto) en segundo plano."""
        mkv = self._last_video
        wav = self._audio_wav
        if not (mkv and os.path.exists(mkv) and os.path.getsize(mkv) > 10240):
            self._flash("⚠ Grabación vacía"); return
        mp4 = os.path.splitext(mkv)[0] + ".mp4"
        temp_mp4 = os.path.splitext(mkv)[0] + ".procesando.mp4"
        fx = getattr(self, "_rec_effect", "")
        ts_enabled = getattr(self, "_rec_timestamp", False)
        codec = getattr(self, "_rec_codec", CODECS[0][1])
        timestamp_file = ""
        filters = []
        if fx:
            filters.append(fx)
        if ts_enabled:
            ts_filter, timestamp_file = timestamp_filter("normal")
            filters.append(ts_filter)
        br = getattr(self, "_rec_bitrate", 6)
        if self.dev_combo.currentData() == "kinect":
            fps = 30
        else:
            try:
                _, _, fps, _ = RESOLUTIONS[self.res_combo.currentIndex()]
            except Exception:
                fps = 30
        cmd = ["/usr/bin/ffmpeg", "-y", "-i", mkv]
        has_audio = bool(wav and os.path.exists(wav) and os.path.getsize(wav) > 4096)
        if has_audio:
            offset = max(0.0, float(getattr(self, "_audio_offset", 0.0)))
            cmd += ["-itsoffset", f"{offset:.3f}", "-i", wav]
        cmd += ["-map", "0:v:0"]
        if has_audio:
            cmd += ["-map", "1:a:0"]
        cmd += ["-c:v", codec, "-b:v", f"{br}M", "-r", str(fps)]
        if codec.startswith("hevc"):
            cmd += ["-tag:v", "hvc1"]
        else:
            cmd += ["-tag:v", "avc1"]
        if filters:
            cmd += ["-vf", ",".join(filters)]
        cmd += (["-c:a", "aac", "-b:a", "160k", "-shortest"] if has_audio else ["-an"])
        cmd += ["-movflags", "+faststart", temp_mp4]
        log_path = f"/tmp/biro-cam-ffmpeg-{time.time_ns()}.log"
        try:
            with open(log_path, "wb") as log:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log, env=clean_env())
        except (FileNotFoundError, OSError) as exc:
            if timestamp_file:
                os.unlink(timestamp_file)
            self._flash(f"⚠ No se pudo iniciar FFmpeg: {exc}")
            return
        self._conversions[proc.pid] = {
            "proc": proc, "mkv": mkv, "wav": wav if has_audio else "", "mp4": mp4,
            "temp_mp4": temp_mp4, "require_audio": has_audio,
            "timestamp": timestamp_file, "log": log_path, "cmd": cmd,
            "hardware_codec": codec, "retried_software": False,
        }
        QTimer.singleShot(500, lambda pid=proc.pid: self._poll_conversion(pid))
        self._flash("🎬 Procesando vídeo…")

    def _poll_conversion(self, pid):
        job = self._conversions.get(pid)
        if not job:
            return
        rc = job["proc"].poll()
        if rc is None:
            QTimer.singleShot(500, lambda: self._poll_conversion(pid))
            return
        if rc != 0 and not job["retried_software"] and job["hardware_codec"].endswith("_rkmpp"):
            fallback = "libx265" if job["hardware_codec"].startswith("hevc") else "libx264"
            cmd = list(job["cmd"])
            codec_pos = cmd.index("-c:v") + 1
            cmd[codec_pos] = fallback
            cmd[codec_pos + 1:codec_pos + 1] = ["-preset", "veryfast"]
            if os.path.exists(job["temp_mp4"]):
                os.unlink(job["temp_mp4"])
            try:
                with open(job["log"], "ab") as log:
                    log.write(f"\n--- Reintento automático con {fallback} ---\n".encode())
                    job["proc"] = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL, stderr=log, env=clean_env())
                job["retried_software"] = True
                self._flash("⚙ Codificador HW ocupado; reintentando por software…")
                QTimer.singleShot(500, lambda: self._poll_conversion(pid))
                return
            except OSError:
                pass
        self._conversions.pop(pid, None)
        ok = (rc == 0 and os.path.exists(job["temp_mp4"])
              and os.path.getsize(job["temp_mp4"]) > 10240)
        detail = ""
        if ok:
            ok, detail = self._media_is_valid(job["temp_mp4"], job["require_audio"])
        if ok:
            output_duration = self._media_duration(job["temp_mp4"])
            source_duration = max(self._media_duration(job["mkv"]),
                                  self._media_duration(job["wav"]) if job["wav"] else 0.0)
            if source_duration > 0 and output_duration < source_duration - 1.0:
                ok = False
                detail = (f"MP4 truncado: {output_duration:.1f} s de "
                          f"{source_duration:.1f} s esperados")
        if job["timestamp"] and os.path.exists(job["timestamp"]):
            os.unlink(job["timestamp"])
        if ok:
            os.replace(job["temp_mp4"], job["mp4"])
            for path in (job["mkv"], job["wav"]):
                if path and os.path.exists(path):
                    os.unlink(path)
            self._flash("✅ Vídeo listo")
            subprocess.Popen(["notify-send", "✅ Vídeo listo", os.path.basename(job["mp4"])], env=clean_env())
            self._maybe_close_after_conversion()
            return
        if os.path.exists(job["temp_mp4"]):
            os.unlink(job["temp_mp4"])
        detail = detail or self._log_tail(job["log"])
        self._flash("⚠ Error al guardar; se conservaron MKV/WAV")
        subprocess.Popen(["notify-send", "-u", "critical", "⚠️ Error al guardar vídeo", detail], env=clean_env())
        self._maybe_close_after_conversion()

    def _maybe_close_after_conversion(self):
        if getattr(self, "_close_when_finished", False) and not self._conversions:
            QTimer.singleShot(100, self.close)

    def _recover_interrupted_conversions(self):
        """Finaliza MP4 válidos que quedaron con sufijo .procesando tras un cierre."""
        recovered = 0
        for directory in (VIDEO_DIR, SECURITY_DIR):
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".procesando.mp4"):
                    continue
                temp_mp4 = os.path.join(directory, name)
                stem = temp_mp4.removesuffix(".procesando.mp4")
                final_mp4 = stem + ".mp4"
                mkv = stem + ".mkv"
                wav = stem + ".wav"
                require_audio = os.path.exists(wav) and os.path.getsize(wav) > 4096
                ok, _ = self._media_is_valid(temp_mp4, require_audio)
                if ok:
                    output_duration = self._media_duration(temp_mp4)
                    source_duration = max(self._media_duration(mkv),
                                          self._media_duration(wav))
                    ok = not (source_duration > 0
                              and output_duration < source_duration - 1.0)
                if not ok:
                    continue
                try:
                    os.replace(temp_mp4, final_mp4)
                    for path in (mkv, wav):
                        if os.path.exists(path):
                            os.unlink(path)
                    recovered += 1
                except OSError as exc:
                    self._flash(f"⚠ No se pudo recuperar {name}: {exc}")
        if recovered:
            self._flash(f"✅ {recovered} conversión interrumpida recuperada")

    @staticmethod
    def _log_tail(path, lines=3):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return "\n".join(fh.read().splitlines()[-lines:]) or "FFmpeg terminó sin detalle"
        except OSError:
            return "No se pudo leer el registro de FFmpeg"

    _last_video = ""
    _audio_wav = ""
    _audio_proc = None
    _rec_effect = ""
    _rec_bitrate = 6
    _rec_timestamp = False
    _rec_codec = CODECS[0][1]
    _rec_max_duration = 0
    _audio_offset = 0.0

    def change_resolution(self, idx):
        w, h, fps, label = RESOLUTIONS[idx]
        self.sec_res_combo.setCurrentIndex(idx)
        mpv_ipc(["set_property", "demuxer-lavf-o",
                 f"video_size={w}x{h},input_format={CAM_INPUT_FORMAT},framerate={fps}"])
        mpv_ipc(["loadfile", CAM_URL, "replace"])
        QTimer.singleShot(800, self._apply_view_state)  # re-aplicar zoom tras recargar
        self._flash(f"🎞 Resolución: {label}")

    def preset_lowlight(self):
        """Aclara sombras sin quemar la imagen, respetando cada cámara."""
        details = v4l2_control_details()
        is_emeet = "emeet" in self.dev_combo.currentText().lower()
        settings = {}
        for cid in ("brightness", "contrast", "saturation", "gamma", "gain", "sharpness"):
            if cid not in details:
                continue
            lo, hi, _, default, _ = details[cid]
            if is_emeet:
                emeet_values = {"gain": 100, "brightness": 60, "gamma": 320,
                                "contrast": 50, "saturation": 90, "sharpness": 32}
                value = emeet_values.get(cid, default)
            elif cid == "brightness":
                value = default + round((hi - default) * 0.15)
            elif cid == "gamma":
                value = default + round((hi - default) * 0.10)
            elif cid == "gain":
                value = default + round((hi - default) * 0.20)
            else:
                value = default
            settings[cid] = max(lo, min(hi, value))
        for c, v in settings.items():
            v4l2_set(c, v)
            if c in self.sliders:
                self.sliders[c].blockSignals(True)
                self.sliders[c].setValue(v)
                self.value_labels[c].setText(str(v))
                self.sliders[c].blockSignals(False)
        v4l2_set("auto_exposure", 3)
        self._save_camera_controls()
        self._flash("🌙 Poca luz suave aplicada")

    def preset_reset(self):
        details = v4l2_control_details()
        for cid, _, _, _, fallback in CONTROLS:
            default = details.get(cid, (0, 0, 1, fallback, fallback))[3]
            v4l2_set(cid, default)
            self.sliders[cid].blockSignals(True)
            self.sliders[cid].setValue(default)
            self.value_labels[cid].setText(str(default))
            self.sliders[cid].blockSignals(False)
        self.zoom_slider.setValue(0)  # dispara video-zoom 0 (sin zoom)
        self._on_zoom(0)
        v4l2_set("auto_exposure", 3); v4l2_set("focus_automatic_continuous", 1)
        v4l2_set("white_balance_automatic", 1)
        self.exposure_auto = self.focus_auto = self.wb_auto = True
        self.btn_exp_auto.setChecked(True); self.btn_foc_auto.setChecked(True)
        self.btn_wb_auto.setChecked(True)
        self.mirror = False                       # quitar espejo, efecto y encuadre
        self.fx_combo.setCurrentIndex(0)
        self.grid_combo.setCurrentIndex(0)
        self._apply_vf()
        self._save_camera_controls()
        self._flash("↺ Valores nativos de la cámara restaurados")

    # ----------------------------------------------------------------- seguridad
    def _toggle_security(self):
        try:
            if self.security_active:
                self._stop_security()
            else:
                self._start_security()
        except Exception as e:
            self._flash(f"⚠ Error al cambiar seguridad: {e}")
            traceback.print_exc()

    def _start_security(self):
        if self.recording:
            self.btn_sec.setChecked(False)
            return
        # Confirmar que la grabación normal dejó libre stream-record antes de armar
        # seguridad. No se continúa con un estado heredado o sin respuesta de mpv.
        reply = mpv_ipc_result(["set_property", "stream-record", ""])
        if reply.get("error") != "success":
            self.btn_sec.setChecked(False)
            self._flash(f"⚠ mpv no está listo: {reply.get('error')}")
            return
        os.makedirs(SECURITY_DIR, exist_ok=True)
        # mpv conserva la cámara y la misma resolución durante toda la sesión.
        self.sec_res_combo.setCurrentIndex(self.res_combo.currentIndex())
        # Overlay HUD semi-transparente (se queda visible toda la sesión)
        vw, vh = self.video.width(), self.video.height()
        bw, bh = 520, 340
        self.security_overlay.setGeometry((vw - bw) // 2, (vh - bh) // 2, bw, bh)
        self.security_overlay.show()
        self.security_overlay.raise_()
        self.sec_status_lbl.setText("Iniciando detector…")
        self.sec_side_status.setText("🟢 SEGURIDAD ACTIVA · Iniciando detector…")
        self.btn_sec.setText("🟢 Seguridad ACTIVA · Desactivar")
        self.sec_progress.setRange(0, 0)
        mic_txt = self.sec_mic_combo.currentText() if self.sec_mic_combo.currentData() else "Sin audio"
        self.sec_params_lbl.setText(
            f"📷 {self.sec_res_combo.currentText()}  ·  🎯 Sens: {self.sec_sens_slider.value()}  ·  ⏱ Espera: {self.sec_cooldown_slider.value()} s\n"
            f"🎙 {mic_txt}  ·  💾 {self.sec_bitrate_slider.value()} Mbps  ·  📹 {self.codec_combo.currentText()}")
        # Bloquear controles incompatibles (setEnabled ya los atenúa via CSS :disabled)
        for w in (self.btn_photo, self.btn_rec, self.res_box, self.mic_box,
                   self.fx_combo, self.grid_combo, self.timer_combo,
                   self.photo_quality_combo, self.shutter_check, self.ts_checkbox,
                   self.bitrate_box, self.dur_codec_box, self.zoom_slider):
            w.setEnabled(False)
        self.security_panel.show()
        # Arrancar motor
        self.security_active = True
        self._sec_record_starting = False
        self._sec_recovery_attempted = False
        self._sec_overlay_ts = time.monotonic()
        sens = self.sec_sens_slider.value()
        cool = self.sec_cooldown_slider.value()
        QTimer.singleShot(900, lambda: self._start_security_engine(sens, cool))
        self._flash("🔒 Seguridad activa")

    def _start_security_engine(self, sens, cool):
        if not self.security_active:
            return
        try:
            self.security_engine.start(sensitivity=sens, cooldown=cool)
        except Exception as e:
            self._flash(f"⚠ Error al iniciar detector: {e}")

    def _stop_security(self):
        if not self.security_active:
            return
        self.security_active = False
        self.security_engine.stop()
        self._stop_sec_recording()
        self.security_overlay.hide()
        self.security_label.hide()
        self.security_panel.hide()
        self.btn_sec.setText("🔒  Modo Seguridad")
        self.sec_side_status.setText("⚫ Seguridad inactiva")
        # Re-activar controles
        for w in (self.btn_photo, self.btn_rec, self.res_box, self.mic_box,
                   self.fx_combo, self.grid_combo, self.timer_combo,
                   self.photo_quality_combo, self.shutter_check, self.ts_checkbox,
                   self.bitrate_box, self.dur_codec_box, self.zoom_slider):
            w.setEnabled(True)
        self._flash("Seguridad desactivada")

    def _security_frame_cb(self, frame):
        if not self.security_active:
            return
        self._sec_last_frame = frame

        # Ocultar la etiqueta para que mpv renderice el visor nativo de forma 100% fluida
        if not self.security_label.isHidden():
            self.security_label.hide()
        self.security_overlay.raise_()  # Mantener HUD del modo seguridad por encima

        if self.security_overlay.isVisible() and time.monotonic() - self._sec_overlay_ts > 1.0:
            if not self.security_recording:
                self.sec_status_lbl.setText("Detector listo ✓")
                self.sec_side_status.setText("🟢 SEGURIDAD ACTIVA · Vigilando")
            self.sec_progress.setRange(0, 100)
            self.sec_progress.setValue(100)

    def _on_sec_motion(self):
        self._sec_last_motion = time.monotonic()
        if self.security_recording or self._sec_record_starting:
            return
        # El RKMPP del RK3588 puede rechazar un segundo codificador simultáneo.
        # Esperar al clip anterior evita conversiones solapadas y caídas a CPU.
        if self._conversions:
            self.sec_status_lbl.setText("Procesando clip anterior…")
            self.sec_side_status.setText("🟠 SEGURIDAD ACTIVA · Guardando clip anterior")
            return
        self._sec_record_starting = True
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._sec_rec_path = os.path.join(SECURITY_DIR, f"Seguridad_{ts}.mkv")
        self._sec_pending_mic = self.sec_mic_combo.currentData()
        self._sec_audio_wav = ""
        self._sec_audio_proc = None
        self._sec_check_attempts = 0
        self._sec_recovery_attempted = False
        QTimer.singleShot(200, self._begin_sec_recording)

    def _begin_sec_recording(self):
        if not self.security_active or self.security_recording:
            self._sec_record_starting = False
            return
        video_t0 = time.monotonic()
        reply = mpv_ipc_result(["set_property", "stream-record", self._sec_rec_path])
        if reply.get("error") != "success":
            self._sec_record_starting = False
            detail = str(reply.get("error", "error IPC desconocido"))
            self.sec_status_lbl.setText("⚠ Error de grabación")
            self.sec_side_status.setText(f"🔴 ERROR · {detail}")
            self._flash(f"⚠ mpv rechazó la grabación: {detail}")
            subprocess.Popen(["notify-send", "-u", "critical",
                              "⚠️ Error en modo seguridad", detail], env=clean_env())
            return
        mic = self._sec_pending_mic
        self._sec_audio_offset = 0.0
        audio_error = ""
        if mic:
            ts = os.path.basename(self._sec_rec_path).removesuffix(".mkv").removeprefix("Seguridad_")
            self._sec_audio_wav = os.path.join(SECURITY_DIR, f"Seguridad_{ts}.wav")
            self._sec_audio_proc, audio_error = self._start_audio_capture(
                mic, self._sec_audio_wav)
            self._sec_audio_offset = max(0.0, time.monotonic() - video_t0)
            if not self._sec_audio_proc:
                self._sec_audio_wav = ""
        self.security_recording = True
        self._sec_record_starting = False
        self._sec_last_motion = time.monotonic()
        self._sec_record_t0 = time.monotonic()
        self.sec_status_lbl.setText("🔴 Grabando…")
        self.sec_side_status.setText("🔴 SEGURIDAD ACTIVA · Grabando movimiento")
        self.sec_progress.setRange(0, 0)
        if self._sec_audio_proc:
            self._flash("🔴 Grabando seguridad + audio 🎙")
        elif mic:
            self._flash(f"🔴 Seguridad SIN audio · {audio_error}")
        else:
            self._flash("🔴 Grabando seguridad (sin audio)")
        QTimer.singleShot(500, self._sec_cooldown_tick)
        max_min = REC_DURATIONS[self.dur_combo.currentIndex()][1]
        if max_min > 0:
            QTimer.singleShot(max_min * 60 * 1000, self._sec_max_duration_tick)
        QTimer.singleShot(1500, self._check_sec_recording)

    def _check_sec_recording(self):
        """Comprueba que mpv realmente empezó a escribir el MKV de seguridad."""
        if not self.security_recording:
            return
        if os.path.exists(self._sec_rec_path) and os.path.getsize(self._sec_rec_path) > 10240:
            return
        # A 4K el muxer puede tardar en materializar el primer bloque. Confirmar la
        # propiedad y esperar hasta 4.5 s antes de declarar un fallo real.
        state = mpv_ipc_result(["get_property", "stream-record"])
        self._sec_check_attempts += 1
        if (state.get("error") == "success"
                and state.get("data") == self._sec_rec_path
                and self._sec_check_attempts < 3):
            QTimer.singleShot(1500, self._check_sec_recording)
            return
        # mpv puede dejar stream-record activo pero sin paquetes tras varias rotaciones.
        # Recargar una sola vez reinicia el demuxer; solo se notifica si también falla.
        if (state.get("error") == "success"
                and state.get("data") == self._sec_rec_path
                and not self._sec_recovery_attempted):
            self._sec_recovery_attempted = True
            mpv_ipc_result(["set_property", "stream-record", ""])
            self._stop_sec_audio()
            if os.path.exists(self._sec_rec_path):
                os.unlink(self._sec_rec_path)
            self.security_recording = False
            self._sec_record_starting = True
            self.sec_status_lbl.setText("Recuperando cámara…")
            self.sec_side_status.setText("🟠 SEGURIDAD ACTIVA · Recuperando flujo…")
            w, h, fps, _ = RESOLUTIONS[self.res_combo.currentIndex()]
            mpv_ipc(["set_property", "demuxer-lavf-o",
                     f"video_size={w}x{h},input_format={CAM_INPUT_FORMAT},framerate={fps}"])
            mpv_ipc(["loadfile", CAM_URL, "replace"])
            self._sec_check_attempts = 0
            QTimer.singleShot(1800, self._begin_sec_recording)
            return
        mpv_ipc_result(["set_property", "stream-record", ""])
        self._stop_sec_audio()
        self.security_recording = False
        self._sec_record_starting = False
        for path in (self._sec_rec_path, self._sec_audio_wav):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self.sec_status_lbl.setText("⚠ Error de grabación")
        self.sec_side_status.setText("🔴 ERROR · No se pudo guardar el clip")
        self._flash("⚠ Seguridad no pudo guardar el vídeo")
        detail = str(state.get("error") if state.get("error") != "success"
                     else f"stream-record={state.get('data')!r}")
        subprocess.Popen(["notify-send", "-u", "critical", "⚠️ Error en modo seguridad",
                          detail], env=clean_env())

    def _sec_cooldown_tick(self):
        if not self.security_recording or not self.security_active:
            return
        idle = time.monotonic() - self._sec_last_motion
        if idle < self.sec_cooldown_slider.value():
            QTimer.singleShot(500, self._sec_cooldown_tick)
            return
        self._stop_sec_recording()

    def _sec_max_duration_tick(self):
        if self.security_recording and self.security_active:
            self._stop_sec_recording()

    def _stop_sec_audio(self):
        proc = getattr(self, "_sec_audio_proc", None)
        self._stop_audio_capture(proc)
        self._sec_audio_proc = None

    def _stop_sec_recording(self):
        if not self.security_recording:
            return
        mpv_ipc_result(["set_property", "stream-record", ""])
        self._stop_sec_audio()
        self.security_recording = False
        if self.security_active:
            self.sec_status_lbl.setText("Procesando clip…")
            self.sec_side_status.setText("🟢 SEGURIDAD ACTIVA · Procesando clip…")
            self.sec_progress.setRange(0, 100)
            self.sec_progress.setValue(100)
        # Reutilizar el conversor validado del modo normal. Conserva la resolución
        # original y solo elimina MKV/WAV después de comprobar el MP4.
        self._last_video = self._sec_rec_path
        self._audio_wav = self._sec_audio_wav
        self._rec_effect = ""
        self._rec_bitrate = self.sec_bitrate_slider.value()
        self._rec_timestamp = self.ts_checkbox.isChecked()
        self._rec_codec = CODECS[self.codec_combo.currentIndex()][1]
        self._audio_offset = getattr(self, "_sec_audio_offset", 0.0)
        QTimer.singleShot(900, self._finish_recording)

    def _on_sec_sens(self, v):
        self.security_engine.set_sensitivity(v)

    def _on_sec_cooldown(self, v):
        self.security_engine.set_cooldown(v)

    def _on_sec_npu_toggled(self, checked):
        self.security_engine.intelligent_detection = checked

    def closeEvent(self, event):
        if not getattr(self, "_close_when_finished", False) and (
                self.recording or self.security_recording or self._conversions):
            self._close_when_finished = True
            if self.recording:
                self.toggle_record()
            if self.security_active:
                self._stop_security()
            self._flash("⏳ Finalizando grabación antes de cerrar…")
            event.ignore()
            QTimer.singleShot(1200, self._wait_then_close)
            return
        self._save_settings()
        self._stop_security()
        if not self._stop_kinect():
            event.ignore()
            QTimer.singleShot(500, self.close)
            return
        self.security_engine.shutdown()
        pose_model = getattr(self, "_rknn_pose_model", None)
        if pose_model is not None:
            try:
                pose_model.release()
            except Exception:
                pass
            self._rknn_pose_model = None
        mp_pose_model = getattr(self, "_mp_pose_model", None)
        if mp_pose_model is not None:
            try:
                mp_pose_model.close()
            except Exception:
                pass
            self._mp_pose_model = None
        if self._audio_proc and self._audio_proc.poll() is None:
            self._audio_proc.terminate()
        if self._vu_proc:
            self._vu_proc.kill()
        if self._photo_timer and self._photo_timer.isActive():
            self._photo_timer.stop()
        if self.mpv_proc and self.mpv_proc.poll() is None:
            mpv_ipc(["quit"])
            try:
                self.mpv_proc.wait(timeout=2)
            except Exception:
                self.mpv_proc.terminate()
        super().closeEvent(event)

    def _wait_then_close(self):
        if self.recording or self.security_recording or self._conversions:
            QTimer.singleShot(500, self._wait_then_close)
            return
        self.close()

    def _exception_hook(self, exctype, value, tb):
        msg = "".join(traceback.format_exception(exctype, value, tb))
        log = "/tmp/biro-cam-crash.log"
        try:
            with open(log, "a") as f:
                f.write(f"\n=== {datetime.now()} ===\n{msg}\n")
        except OSError:
            pass
        self._flash(f"⚠ Error: {value}")
        print(msg, file=sys.stderr)

    # ----------------------------------------------------------------- estilo
    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#0b1120; color:#e6edf6;
                font-family:'Arial'; font-size:13px; }
            QWidget#video { background:#000000; }
            QWidget#panel { background:#0b1120; }
            QScrollArea#panel_scroll { background:#0b1120; border-left:1px solid #1b2536; }
            QScrollBar:vertical { background:#0a0f1a; width:10px; border:none; }
            QScrollBar::handle:vertical { background:#1b2536; min-height:30px;
                border-radius:5px; }
            QScrollBar::handle:vertical:hover { background:#243047; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
            QLabel { color:#cdd6e4; }
            QLabel#title { color:#e6edf6; font-size:17px; font-weight:bold; }
            QLabel#subtitle { color:#7e8aa0; font-size:11px; }
            QLabel#dot_on  { color:#34d399; font-size:18px; }
            QLabel#dot_off { color:#475569; font-size:18px; }
            QLabel#rec { color:#f87171; font-weight:bold; font-size:13px; }
            QProgressBar#vu { background:#0c1320; border:1px solid #1b2536; border-radius:4px; }
            QProgressBar#vu::chunk {
                border-radius:3px;
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #34d399, stop:0.6 #fbbf24, stop:1 #f87171); }
            QPushButton { background:#1b2536; border:1px solid #243047;
                border-radius:9px; padding:7px 11px; }
            QPushButton:hover { border-color:#60a5fa; background:#222f45; }
            QPushButton:pressed { background:#2b3b57; }
            QPushButton:checked { background:#60a5fa; color:#0b1120; border-color:#60a5fa;
                font-weight:bold; }
            QPushButton:disabled { background:#0a0f1a; color:#334155; border-color:#1b2536; }
            QCheckBox:disabled { color:#334155; }
            QSlider:disabled::handle:horizontal { background:#334155; border-color:#243047; }
            QPushButton#iconbtn { font-size:16px; padding:0; border-radius:8px; }
            QComboBox { background:#1b2536; border:1px solid #243047;
                border-radius:7px; padding:5px 9px; }
            QComboBox:hover { border-color:#60a5fa; }
            /* ---- bloqueo durante la grabación: fila atenuada y oscurecida ---- */
            QWidget#lockrow[locked="true"] { background:#0a0f1a; border-radius:8px; }
            QComboBox:disabled { background:#0c1320; color:#3a4760; border-color:#172033; }
            QLabel:disabled { color:#46566f; }
            QComboBox QAbstractItemView { background:#111827; color:#e6edf6;
                selection-background-color:#60a5fa; selection-color:#0b1120; border:1px solid #243047; }
            QSlider::groove:horizontal { height:6px; background:#243047; border-radius:3px; }
            QSlider::sub-page:horizontal { background:#3b82f6; border-radius:3px; }
            QSlider::handle:horizontal { background:#dbeafe; width:12px; height:12px;
                margin:-5px 0; border-radius:6px; border:2px solid #3b82f6; }
            QSlider::handle:horizontal:hover { background:#ffffff; }
            QToolTip { background:#111827; color:#e6edf6; border:1px solid #243047; padding:4px; }
        """)


def main():
    # El incrustado de mpv (--wid) necesita X11; bajo Wayland usamos XWayland (xcb).
    if not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
    app = QApplication(sys.argv)
    # WM_CLASS(class) = applicationName -> 'biro-cam' para que coincida con el
    # StartupWMClass del .desktop y GNOME muestre el LOGO de cámara (no la tuerca).
    app.setApplicationName("biro-cam")
    app.setDesktopFileName("biro-cam")
    if os.path.exists(ICON_PATH):
        app.setWindowIcon(QIcon(ICON_PATH))
    panel = Panel()
    panel.resize(960, 880)
    panel.show()
    panel.raise_()             # traer al frente
    panel.activateWindow()     # darle el foco (por si abre detrás)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
