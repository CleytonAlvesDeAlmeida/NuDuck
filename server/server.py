#!/usr/bin/env python3
"""
NuDuck - server.py
Transforma o celular Android num monitor do PC Linux via WebRTC.

Funciona na rede local (Wi-Fi ou USB), sem nuvem, sem cadastro.
Autenticação por PIN de 6 dígitos exibido na tela do PC.

Modos:
  - Espelhar: o celular repete a tela do PC.
  - Estender: o celular vira uma segunda tela (usando Xvfb, igual SpaceDesk).

Uso:
    python3 server.py

Requisitos: ver requirements.txt + pacotes do sistema no README.
"""

import asyncio
import concurrent.futures
import fractions
import ipaddress
import json
import logging
import os
import queue
import secrets
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import mss
import numpy as np
import pyautogui
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaRelay
from av import VideoFrame
from zeroconf import ServiceInfo, Zeroconf

# Display virtual Xvfb (abordagem SpaceDesk para o modo Estender)
from virtual_display import XvfbVirtualDisplay, is_xvfb_available, stop_active_display


# ==========================================================================
# Configuração
# ==========================================================================

APP_NAME = "NuDuck"
PORT = 8765
SERVICE_TYPE = "_droidmonitor._tcp.local."

# Qualidade de vídeo: "auto" ajusta sozinho, os demais são fixos.
# A largura é calculada pela proporção real da tela (nunca estica nem corta).
QUALITY_PRESETS = {
    "144p":  {"height": 144,  "fps": 15},
    "240p":  {"height": 240,  "fps": 15},
    "360p":  {"height": 360,  "fps": 20},
    "480p":  {"height": 480,  "fps": 24},
    "720p":  {"height": 720,  "fps": 30},
    "1080p": {"height": 1080, "fps": 30},
}
QUALITY_ORDER = ["144p", "240p", "360p", "480p", "720p", "1080p"]
DEFAULT_QUALITY = "480p"


def is_valid_quality(value: str) -> bool:
    return value == "auto" or value in QUALITY_PRESETS

MAX_PIN_ATTEMPTS = 5
PIN_BLOCK_SECONDS = 60
PIN_LENGTH = 6

pyautogui.FAILSAFE = False
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(APP_NAME)

# Buffer de logs em memória pra janela "Ver terminal"
LOG_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=5000)


class _QueueLogHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_QUEUE.put_nowait(self.format(record))
        except queue.Full:
            pass


_queue_handler = _QueueLogHandler()
_queue_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(_queue_handler)


# ==========================================================================
# Estado global (PIN, controle, qualidade)
# ==========================================================================

@dataclass
class AppState:
    pin: str = field(default_factory=lambda: "".join(secrets.choice("0123456789") for _ in range(PIN_LENGTH)))
    allow_control: bool = False
    quality: str = DEFAULT_QUALITY
    usb_status: str = "checking"
    failed_attempts: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def register_failed_attempt(self, ip: str) -> bool:
        """Retorna True se o IP acabou de ser bloqueado."""
        with self.lock:
            count, blocked_until = self.failed_attempts.get(ip, (0, 0))
            count += 1
            newly_blocked = False
            if count >= MAX_PIN_ATTEMPTS:
                blocked_until = time.time() + PIN_BLOCK_SECONDS
                newly_blocked = True
            self.failed_attempts[ip] = (count, blocked_until)
            return newly_blocked

    def is_blocked(self, ip: str) -> bool:
        with self.lock:
            count, blocked_until = self.failed_attempts.get(ip, (0, 0))
            if blocked_until and time.time() < blocked_until:
                return True
            if blocked_until and time.time() >= blocked_until:
                self.failed_attempts[ip] = (0, 0)
            return False

    def clear_attempts(self, ip: str):
        with self.lock:
            self.failed_attempts.pop(ip, None)


STATE = AppState()
relay = MediaRelay()
pcs: set[RTCPeerConnection] = set()


# ==========================================================================
# Restrição de rede local (bloqueia IPs de fora)
# ==========================================================================

def is_local_ip(ip_str: str) -> bool:
    """True se o IP é da rede local ou localhost."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return (
            ip in ipaddress.ip_network("192.168.0.0/16")
            or ip in ipaddress.ip_network("10.0.0.0/8")
            or ip in ipaddress.ip_network("172.16.0.0/12")
        )
    return False


@web.middleware
async def local_network_only_middleware(request: web.Request, handler):
    peer_ip = request.remote
    if not peer_ip or not is_local_ip(peer_ip):
        log.warning("Conexão recusada de IP não-local: %s", peer_ip)
        raise web.HTTPForbidden(text="Somente rede local é permitida.")
    return await handler(request)


# ==========================================================================
# Captura de tela -> VideoStreamTrack
# ==========================================================================

class ScreenCaptureTrack(VideoStreamTrack):
    """Captura a tela com mss e entrega frames pro WebRTC.

    Dois modos:
      - "mirror": captura o monitor principal do PC e envia pro celular.
      - "extend": cria um display virtual via Xvfb e transmite ele.
        Funciona em qualquer hardware (igual ao SpaceDesk).
    """

    # Limites do ajuste automático de qualidade
    _AUTO_LOAD_HIGH = 0.85
    _AUTO_LOAD_LOW = 0.4
    _AUTO_COOLDOWN_SECONDS = 2.5

    def __init__(self, quality: str = DEFAULT_QUALITY, mode: str = "mirror"):
        super().__init__()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._sct = None
        self._monitor = None
        self._time_base = fractions.Fraction(1, 90000)

        # Display virtual Xvfb (modo Estender)
        self._virtual_display = None
        self._virtual_display_info = None

        self._frame_count = 0
        self._start_time = None

        # Qualidade automática
        self._auto = False
        self._auto_idx = QUALITY_ORDER.index(DEFAULT_QUALITY)
        self._load_ema: Optional[float] = None
        self._last_adapt = 0.0

        # Decide o modo
        self.requested_mode = mode if mode in ("mirror", "extend") else "mirror"
        (
            self.resolved_mode,
            self._monitor_index,
            self._explicit_region,
            self.fallback_reason,
        ) = self._resolve_monitor(self.requested_mode)

        self.set_quality(quality)

    def _resolve_monitor(self, mode: str) -> tuple:
        """Decide qual tela capturar.

        Retorna (modo_real, índice_do_monitor, região, motivo_do_fallback).
        """
        if mode != "extend":
            return "mirror", 1, None, None

        # --- Modo Estender: cria display virtual via Xvfb (SpaceDesk) ---
        # Limpa Xvfb de conexão anterior (se existir)
        stop_active_display()

        if is_xvfb_available():
            log.info("Criando display virtual via Xvfb...")
            vd = XvfbVirtualDisplay(width=1280, height=800)
            success, result, info = vd.start()
            if success:
                self._virtual_display = vd
                self._virtual_display_info = info
                log.info("Display virtual criado em %s (%s).", result, info.get("resolution", "?"))
                return "extend", -1, None, None
            else:
                reason = f"Xvfb falhou: {result}"
                log.warning("Falha ao criar display virtual: %s", reason)
                return "mirror", 1, None, reason

        # Xvfb não instalado
        reason = (
            "Xvfb não encontrado. Instale com:\n"
            "  sudo apt install xvfb xdotool openbox\n"
            "(Debian/Ubuntu) ou:\n"
            "  sudo dnf install xorg-x11-server-Xvfb xdotool openbox\n"
            "(Fedora)"
        )
        log.warning("Modo 'Estender' pedido, mas %s", reason)
        return "mirror", 1, None, reason

    def set_quality(self, quality: str):
        """Define a qualidade do vídeo."""
        self._auto = (quality == "auto")
        if self._auto:
            self._apply_quality_index(QUALITY_ORDER.index(DEFAULT_QUALITY))
            self._load_ema = None
            self._last_adapt = time.time()
        else:
            preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS[DEFAULT_QUALITY])
            self._target_h = preset["height"]
            self._fps = preset["fps"]
            self._frame_interval = 1.0 / self._fps

    def _apply_quality_index(self, idx: int):
        idx = max(0, min(idx, len(QUALITY_ORDER) - 1))
        self._auto_idx = idx
        preset = QUALITY_PRESETS[QUALITY_ORDER[idx]]
        self._target_h = preset["height"]
        self._fps = preset["fps"]
        self._frame_interval = 1.0 / self._fps

    def _adapt_quality(self, proc_time: float):
        """Sobe ou desce a qualidade conforme a carga da CPU."""
        load = proc_time / self._frame_interval
        self._load_ema = load if self._load_ema is None else (self._load_ema * 0.8 + load * 0.2)

        now = time.time()
        if now - self._last_adapt < self._AUTO_COOLDOWN_SECONDS:
            return

        if self._load_ema > self._AUTO_LOAD_HIGH and self._auto_idx > 0:
            self._apply_quality_index(self._auto_idx - 1)
            self._last_adapt = now
            self._load_ema = None
            log.info("Automático: reduziu para %s", QUALITY_ORDER[self._auto_idx])
        elif self._load_ema < self._AUTO_LOAD_LOW and self._auto_idx < len(QUALITY_ORDER) - 1:
            self._apply_quality_index(self._auto_idx + 1)
            self._last_adapt = now
            self._load_ema = None
            log.info("Automático: subiu para %s", QUALITY_ORDER[self._auto_idx])

    def _draw_cursor(self, pil_img):
        """Desenha um cursor de seta na posição do mouse."""
        try:
            cx, cy = pyautogui.position()
        except Exception:
            return

        src_w = self._monitor["width"]
        src_h = self._monitor["height"]
        rel_x = cx - self._monitor["left"]
        rel_y = cy - self._monitor["top"]
        if not (0 <= rel_x < src_w and 0 <= rel_y < src_h):
            return  # cursor em outro monitor

        scale = pil_img.height / src_h
        x, y = rel_x * scale, rel_y * scale

        from PIL import ImageDraw
        s = max(10, int(pil_img.height * 0.035))
        points = [
            (x, y), (x, y + s),
            (x + s * 0.35, y + s * 0.75), (x + s * 0.55, y + s * 1.0),
            (x + s * 0.72, y + s * 0.88), (x + s * 0.5, y + s * 0.6),
            (x + s * 0.85, y + s * 0.52),
        ]
        draw = ImageDraw.Draw(pil_img)
        draw.polygon(points, fill=(255, 255, 255), outline=(0, 0, 0))

    def _capture_and_convert(self):
        """Captura a tela e converte para frame do WebRTC."""

        # --- Caminho do display virtual Xvfb ---
        if self._virtual_display and self._virtual_display.is_running():
            frame_bgr = self._virtual_display.get_frame()
            if frame_bgr is not None:
                src_h, src_w = frame_bgr.shape[:2]
                target_h = self._target_h
                target_w = max(2, int(round(src_w * (target_h / src_h) / 2)) * 2)

                from PIL import Image
                pil_img = Image.fromarray(frame_bgr[:, :, ::-1])
                pil_img = pil_img.resize((target_w, target_h), Image.BILINEAR)
                out = np.ascontiguousarray(np.array(pil_img)[:, :, ::-1])
                return VideoFrame.from_ndarray(out, format="bgr24")

            # get_frame() retornou None (não deveria acontecer mais)
            log.warning("get_frame() retornou None, gerando frame de fallback")
            fallback = np.full(
                (self._target_h, max(2, int(self._target_h * 16 / 9)), 3),
                [46, 26, 26], dtype=np.uint8,
            )
            return VideoFrame.from_ndarray(fallback, format="bgr24")

        # --- Caminho normal: captura com mss (modo Espelhar) ---
        if self._sct is None:
            self._sct = mss.mss()
            if self._explicit_region is not None:
                self._monitor = self._explicit_region
            else:
                self._monitor = self._sct.monitors[self._monitor_index]

        raw = self._sct.grab(self._monitor)
        img = np.array(raw)[:, :, :3]  # BGRA -> BGR
        src_h, src_w = img.shape[:2]

        target_h = self._target_h
        target_w = max(2, int(round(src_w * (target_h / src_h) / 2)) * 2)

        from PIL import Image
        pil_img = Image.fromarray(img[:, :, ::-1])  # BGR -> RGB
        pil_img = pil_img.resize((target_w, target_h), Image.BILINEAR)

        self._draw_cursor(pil_img)

        out = np.ascontiguousarray(np.array(pil_img)[:, :, ::-1])  # RGB -> BGR
        return VideoFrame.from_ndarray(out, format="bgr24")

    async def recv(self):
        if self._start_time is None:
            self._start_time = time.time()

        next_frame_time = self._start_time + self._frame_count * self._frame_interval
        now = time.time()
        if next_frame_time > now:
            await asyncio.sleep(next_frame_time - now)

        loop = asyncio.get_event_loop()
        t0 = time.time()
        frame = await loop.run_in_executor(self._executor, self._capture_and_convert)
        proc_time = time.time() - t0

        if self._auto:
            self._adapt_quality(proc_time)

        pts = int(self._frame_count * (90000 / self._fps))
        frame.pts = pts
        frame.time_base = self._time_base
        self._frame_count += 1
        return frame

    def close(self):
        """Encerra a captura e fecha o display virtual."""
        if self._virtual_display:
            self._virtual_display.stop()
            self._virtual_display = None
            self._virtual_display_info = None
        self._executor.shutdown(wait=False)


# ==========================================================================
# Controle remoto (toque do celular -> mouse/teclado do PC)
# ==========================================================================

def handle_control_message(raw_msg: str, screen_track: ScreenCaptureTrack):
    """Processa toques e teclas vindos do celular.

    Formato (JSON, coordenadas de 0.0 a 1.0):
      {"type": "tap",   "x": 0.5, "y": 0.5}
      {"type": "move",  "x": 0.5, "y": 0.5}
      {"type": "down",  "x": 0.5, "y": 0.5}
      {"type": "up",    "x": 0.5, "y": 0.5}
      {"type": "key",   "key": "enter"}
    """
    if not STATE.allow_control:
        return

    try:
        msg = json.loads(raw_msg)
    except (json.JSONDecodeError, TypeError):
        return

    mtype = msg.get("type")

    # Se o display virtual Xvfb está ativo, envia input pra ele
    if screen_track._virtual_display and screen_track._virtual_display.is_running():
        vd = screen_track._virtual_display
        if mtype in ("tap", "move", "down", "up"):
            x = min(max(float(msg.get("x", 0)), 0.0), 1.0)
            y = min(max(float(msg.get("y", 0)), 0.0), 1.0)
            vx, vy = int(x * vd.width), int(y * vd.height)
            action_map = {"tap": "click", "move": "mousemove", "down": "mousedown", "up": "mouseup"}
            vd.send_input(action=action_map.get(mtype, "mousemove"), x=vx, y=vy)
            return
        elif mtype == "key":
            key = msg.get("key")
            if key:
                vd.send_input(action="key", key=key)
            return

    # Input normal (pyautogui na tela principal do PC)
    screen_w, screen_h = pyautogui.size()

    if mtype in ("tap", "move", "down", "up"):
        x = min(max(float(msg.get("x", 0)), 0.0), 1.0)
        y = min(max(float(msg.get("y", 0)), 0.0), 1.0)
        px, py = int(x * screen_w), int(y * screen_h)

        if mtype == "tap":
            pyautogui.click(px, py)
        elif mtype == "move":
            pyautogui.moveTo(px, py, _pause=False)
        elif mtype == "down":
            pyautogui.mouseDown(px, py)
        elif mtype == "up":
            pyautogui.mouseUp(px, py)

    elif mtype == "key":
        key = msg.get("key")
        if key:
            try:
                pyautogui.press(key)
            except Exception:
                log.debug("Tecla não reconhecida: %s", key)


# ==========================================================================
# Sinalização WebSocket (PIN + troca de SDP WebRTC)
# ==========================================================================

async def websocket_handler(request: web.Request):
    """Lida com a conexão WebSocket do celular: PIN, offer/answer, qualidade."""
    ws = web.WebSocketResponse(heartbeat=None)
    await ws.prepare(request)
    peer_ip = request.remote

    if STATE.is_blocked(peer_ip):
        await ws.send_json({"type": "error", "message": "IP temporariamente bloqueado."})
        await ws.close()
        return ws

    pc: Optional[RTCPeerConnection] = None
    screen_track: Optional[ScreenCaptureTrack] = None
    authenticated = False

    log.info("Nova conexão de %s", peer_ip)

    try:
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                continue

            data = json.loads(message.data)
            mtype = data.get("type")

            # Log do celular (aceito antes do PIN, útil pra depuração)
            if mtype == "log":
                level = str(data.get("level", "INFO")).upper()
                tag = str(data.get("tag", ""))[:60]
                text = str(data.get("message", ""))[:500]
                prefix = f"[Celular{f'/{tag}' if tag else ''}] "
                if level == "ERROR":
                    log.error("%s%s", prefix, text)
                elif level in ("WARN", "WARNING"):
                    log.warning("%s%s", prefix, text)
                else:
                    log.info("%s%s", prefix, text)
                continue

            # Verificação do PIN
            if mtype == "pin":
                submitted = str(data.get("pin", ""))
                if submitted == STATE.pin:
                    authenticated = True
                    STATE.clear_attempts(peer_ip)
                    await ws.send_json({"type": "pin_ok"})
                    log.info("PIN correto de %s", peer_ip)
                else:
                    newly_blocked = STATE.register_failed_attempt(peer_ip)
                    await ws.send_json({"type": "pin_error", "blocked": newly_blocked})
                    log.warning("PIN incorreto de %s", peer_ip)
                    if newly_blocked:
                        await ws.close()
                        break
                continue

            if not authenticated:
                await ws.send_json({"type": "error", "message": "Envie o PIN primeiro."})
                continue

            # Oferta WebRTC (SDP offer)
            if mtype == "offer":
                quality = data.get("quality", STATE.quality)
                if not is_valid_quality(quality):
                    quality = DEFAULT_QUALITY
                STATE.quality = quality

                requested_mode = data.get("mode", "mirror")
                if requested_mode not in ("mirror", "extend"):
                    requested_mode = "mirror"

                pc = RTCPeerConnection()
                pcs.add(pc)

                # Cria a track de captura de tela
                screen_track = ScreenCaptureTrack(quality=quality, mode=requested_mode)
                pc.addTrack(relay.subscribe(screen_track))

                # DataChannel para controle remoto
                @pc.on("datachannel")
                def on_datachannel(channel):
                    log.info("DataChannel aberto: %s", channel.label)

                    @channel.on("message")
                    def on_message(msg):
                        handle_control_message(msg, screen_track)

                # Cleanup quando a conexão fecha
                @pc.on("connectionstatechange")
                async def on_state_change():
                    log.info("Conexão WebRTC: %s", pc.connectionState)
                    if pc.connectionState in ("failed", "closed"):
                        pcs.discard(pc)
                        screen_track.close()
                        await pc.close()

                # Processa o SDP offer e cria a answer
                offer = RTCSessionDescription(sdp=data["sdp"], type=data["sdpType"])
                await pc.setRemoteDescription(offer)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                # Espera os candidatos ICE terminarem
                if pc.iceGatheringState != "complete":
                    gathering_done = asyncio.Event()

                    @pc.on("icegatheringstatechange")
                    def on_ice_gathering_change():
                        if pc.iceGatheringState == "complete":
                            gathering_done.set()

                    try:
                        await asyncio.wait_for(gathering_done.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        log.warning("Timeout coletando ICE; enviando SDP mesmo assim.")

                # Envia a answer pro celular
                answer_payload = {
                    "type": "answer",
                    "sdp": pc.localDescription.sdp,
                    "sdpType": pc.localDescription.type,
                    "mode": screen_track.resolved_mode,
                    "modeFallbackReason": screen_track.fallback_reason,
                }
                if screen_track._virtual_display_info:
                    answer_payload["virtualDisplay"] = screen_track._virtual_display_info
                await ws.send_json(answer_payload)

            elif mtype == "quality" and screen_track is not None:
                q = data.get("value")
                if is_valid_quality(q):
                    screen_track.set_quality(q)
                    STATE.quality = q
                    log.info("Qualidade alterada: %s", q)

    except Exception as exc:
        log.exception("Erro na sessão de %s: %s", peer_ip, exc)
    finally:
        if screen_track is not None:
            screen_track.close()
        if pc is not None:
            pcs.discard(pc)
            await pc.close()
        log.info("Conexão encerrada de %s", peer_ip)

    return ws


async def status_handler(request: web.Request):
    """Endpoint de status pra depuração local."""
    return web.json_response({
        "name": APP_NAME,
        "allow_control": STATE.allow_control,
        "quality": STATE.quality,
    })


# ==========================================================================
# USB (ADB reverse automático — sem comando no terminal)
# ==========================================================================

USB_STATUS_LABELS = {
    "checking":    ("Cabo USB: verificando...", "gray"),
    "connected":   ("Cabo USB: pronto ✅", "#2e7d32"),
    "no_device":   ("Cabo USB: plugue e autorize depuração", "gray"),
    "adb_missing": ("Cabo USB: 'adb' não encontrado", "gray"),
    "error":       ("Cabo USB: erro ao verificar", "gray"),
}


def _adb_reverse_loop():
    """Verifica se tem celular plugado e aplica adb reverse sozinho."""
    adb = shutil.which("adb")
    if adb is None:
        log.info("adb não encontrado; modo USB desativado.")
        STATE.usb_status = "adb_missing"
        return

    while True:
        try:
            result = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=5)
            lines = [ln for ln in result.stdout.splitlines()[1:] if ln.strip()]
            devices = [ln.split("\t")[0] for ln in lines if ln.endswith("\tdevice")]

            if devices:
                rev = subprocess.run(
                    [adb, "reverse", f"tcp:{PORT}", f"tcp:{PORT}"],
                    capture_output=True, text=True, timeout=5,
                )
                STATE.usb_status = "connected" if rev.returncode == 0 else "error"
            else:
                STATE.usb_status = "no_device"
        except Exception:
            STATE.usb_status = "error"

        time.sleep(3)


def start_usb_autoforward():
    threading.Thread(target=_adb_reverse_loop, daemon=True).start()


# ==========================================================================
# mDNS (descoberta automática na rede local)
# ==========================================================================

def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def start_mdns(hostname: str) -> Zeroconf:
    zeroconf = Zeroconf()
    local_ip = get_local_ip()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{hostname}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(local_ip)],
        port=PORT,
        properties={"name": hostname},
        server=f"{hostname}.local.",
    )
    zeroconf.register_service(info)
    log.info("Anunciado via mDNS como '%s' em %s:%d", hostname, local_ip, PORT)
    return zeroconf


# ==========================================================================
# Janela do servidor (PIN + QR Code + checkbox)
# ==========================================================================

def start_ui(hostname: str):
    """Janela Tkinter com o PIN, QR Code e controles."""
    import sys
    import tkinter as tk

    def _icon_path() -> str:
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "assets", "icon.png")

    def _build_qr_photo(root, ui_scale: float = 1.0):
        """Gera o QR Code com host/porta/PIN."""
        try:
            import qrcode
            from PIL import ImageTk

            payload = json.dumps({
                "host": get_local_ip(),
                "port": PORT,
                "name": hostname,
                "pin": STATE.pin,
            })
            box_size = max(3, round(5 * ui_scale))
            qr_img = qrcode.make(payload, box_size=box_size, border=2)
            return ImageTk.PhotoImage(qr_img, master=root)
        except Exception as exc:
            log.warning("QR Code falhou (%s).", exc)
            return None

    def run():
        nonlocal_state = {"log_window": None}

        root = tk.Tk()
        root.title(APP_NAME)

        # Escala pra telas HiDPI/4K
        try:
            dpi = root.winfo_fpixels("1i")
            ui_scale = max(1.0, min(dpi / 96.0, 2.5))
        except Exception:
            ui_scale = 1.0
        root.tk.call("tk", "scaling", ui_scale * (96.0 / 72.0))

        def sc(px: int) -> int:
            return int(round(px * ui_scale))

        base_w, base_h = 340, 620
        win_w = min(sc(base_w), int(root.winfo_screenwidth() * 0.9))
        win_h = min(sc(base_h), int(root.winfo_screenheight() * 0.9))
        pos_x = max(0, (root.winfo_screenwidth() - win_w) // 2)
        pos_y = max(0, (root.winfo_screenheight() - win_h) // 3)
        root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        root.minsize(sc(280), sc(420))
        root.resizable(True, True)

        def shutdown():
            log.info("Encerrando %s...", APP_NAME)
            try:
                root.destroy()
            except Exception:
                pass
            os._exit(0)

        root.protocol("WM_DELETE_WINDOW", shutdown)

        def open_log_viewer():
            win = nonlocal_state["log_window"]
            if win is not None and win.winfo_exists():
                win.lift()
                return

            import tkinter.scrolledtext as scrolledtext

            win = tk.Toplevel(root)
            win.title(f"{APP_NAME} — Terminal")
            log_w = min(sc(640), int(root.winfo_screenwidth() * 0.9))
            log_h = min(sc(420), int(root.winfo_screenheight() * 0.9))
            win.geometry(f"{log_w}x{log_h}")
            win.minsize(sc(360), sc(240))

            text = scrolledtext.ScrolledText(
                win, bg="#0b0b0b", fg="#e6e6e6", insertbackground="#e6e6e6",
                font=("Consolas", 9),
            )
            text.pack(fill="both", expand=True)
            text.configure(state="disabled")

            def poll_logs():
                updated = False
                while True:
                    try:
                        line = LOG_QUEUE.get_nowait()
                    except queue.Empty:
                        break
                    text.configure(state="normal")
                    text.insert("end", line + "\n")
                    updated = True
                if updated:
                    n_lines = int(text.index("end-1c").split(".")[0])
                    if n_lines > 1000:
                        text.delete("1.0", f"{n_lines - 1000}.0")
                    text.see("end")
                    text.configure(state="disabled")
                if win.winfo_exists():
                    win.after(400, poll_logs)

            poll_logs()
            nonlocal_state["log_window"] = win

        try:
            icon_img = tk.PhotoImage(file=_icon_path())
            root.iconphoto(True, icon_img)
        except Exception as exc:
            log.warning("Ícone não encontrado (%s).", exc)

        tk.Label(root, text=APP_NAME, font=("Sans", 16, "bold")).pack(pady=(15, 5))
        tk.Label(root, text="PIN de conexão:", font=("Sans", 11)).pack()
        tk.Label(root, text=STATE.pin, font=("Sans", 28, "bold")).pack(pady=(0, 10))

        qr_photo = _build_qr_photo(root, ui_scale)
        if qr_photo is not None:
            qr_label = tk.Label(root, image=qr_photo)
            qr_label.image = qr_photo
            qr_label.pack(pady=(0, 5))
            tk.Label(
                root, text="Escaneie no app para conectar",
                font=("Sans", 9), fg="gray",
            ).pack(pady=(0, 10))

        control_var = tk.BooleanVar(value=STATE.allow_control)

        def on_toggle():
            STATE.allow_control = control_var.get()
            log.info("Permitir controle: %s", STATE.allow_control)

        tk.Checkbutton(
            root, text="Permitir controle (mouse/teclado)",
            variable=control_var, command=on_toggle,
        ).pack(pady=5)

        usb_label = tk.Label(root, text="Cabo USB: verificando...", fg="gray", font=("Sans", 9))
        usb_label.pack(pady=(8, 0))

        def poll_usb():
            text, color = USB_STATUS_LABELS.get(STATE.usb_status, USB_STATUS_LABELS["checking"])
            usb_label.config(text=text, fg=color)
            root.after(1500, poll_usb)

        poll_usb()

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=(12, 0))
        tk.Button(btn_frame, text="Ver terminal", command=open_log_viewer).pack(side="left", padx=5)
        tk.Button(
            btn_frame, text="Encerrar servidor", command=shutdown,
            fg="white", bg="#b91c1c",
        ).pack(side="left", padx=5)

        tk.Label(root, text=f"Rede local, porta {PORT}", fg="gray").pack(pady=(10, 0))
        tk.Label(root, text="Sem PIN, ninguém conecta.", fg="gray").pack()

        root.mainloop()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()


# ==========================================================================
# Inicialização
# ==========================================================================

async def on_shutdown(app: web.Application):
    for pc in list(pcs):
        await pc.close()
    pcs.clear()


def build_app() -> web.Application:
    app = web.Application(middlewares=[local_network_only_middleware])
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/status", status_handler)
    app.on_shutdown.append(on_shutdown)
    return app


def _port_available(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    hostname = socket.gethostname().split(".")[0]

    if not _port_available(PORT):
        log.error("Porta %d em uso — outra instância do %s já está rodando.", PORT, APP_NAME)
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                APP_NAME,
                f"Já existe um {APP_NAME} rodando (porta {PORT}).\n\n"
                "Feche a outra instância antes de abrir uma nova.",
            )
            root.destroy()
        except Exception:
            pass
        return

    print("=" * 50)
    print(f"  {APP_NAME}")
    print("=" * 50)
    print(f"  PIN: {STATE.pin}")
    print(f"  Porta: {PORT}")
    print("  Wi-Fi: conecte pelo app na mesma rede.")
    print("  USB:   plugue com depuração ativa.")
    print("=" * 50)

    try:
        start_ui(hostname)
    except Exception as exc:
        log.warning("Interface gráfica indisponível (%s); use o console.", exc)

    start_usb_autoforward()

    zeroconf = start_mdns(hostname)
    app = build_app()

    try:
        web.run_app(app, host="0.0.0.0", port=PORT, print=None)
    finally:
        zeroconf.close()


if __name__ == "__main__":
    main()
