#!/usr/bin/env python3
"""
NuDuck - server.py
Transforma o celular Android num monitor do PC Linux via WebRTC.

Funciona na rede local (Wi-Fi ou USB), sem nuvem, sem cadastro.
Autenticação por PIN de 6 dígitos exibido na tela do PC.

Modos:
  - Espelhar: o celular repete a tela do PC.
  - Estender: o celular vira uma segunda tela (usando Xvfb, igual SpaceDesk).
  - Janela: o celular espelha apenas uma janela específica selecionada.

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
import sys
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
from PIL import Image, ImageDraw
from zeroconf import ServiceInfo, Zeroconf
import cv2

# Display virtual Xvfb (abordagem SpaceDesk para o modo Estender)
from virtual_display import XvfbVirtualDisplay, is_xvfb_available, stop_active_display, set_active_display, get_active_display
import json as _json

# ==========================================================================
# Atalhos do servidor (salvos em shortcuts.json)
# ==========================================================================

def _get_persistent_data_dir() -> str:
    """Retorna uma pasta pra guardar dados do usuário (atalhos, etc.) que
    sobrevive a reinicializações do programa.

    Quando rodando direto do código (`python3 server.py`), usa a própria
    pasta do projeto — como sempre foi.

    Quando empacotado como executável único pelo PyInstaller (--onefile),
    `__file__`/`sys.executable` apontam pra dentro de uma pasta TEMPORÁRIA
    que o PyInstaller cria do zero e APAGA toda vez que o programa fecha
    (ex: algo como C:\\Users\\...\\AppData\\Local\\Temp\\_MEIxxxxxx). Qualquer
    coisa salva lá dentro (como os atalhos) some no próximo "abrir o app" —
    era exatamente esse o motivo dos atalhos não persistirem. Por isso, no
    executável, salvamos numa pasta de configuração de verdade do sistema
    (AppData no Windows, ~/.config no Linux/Mac), que não é apagada nunca.
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
        elif sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Application Support")
        else:
            base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        data_dir = os.path.join(base, "NuDuck")
    else:
        data_dir = os.path.dirname(os.path.abspath(__file__))

    try:
        os.makedirs(data_dir, exist_ok=True)
    except Exception as exc:
        log.error("Não consegui criar pasta de dados %s: %s", data_dir, exc)
    return data_dir


SHORTCUTS_FILE = os.path.join(_get_persistent_data_dir(), "shortcuts.json")


def _load_shortcuts() -> list:
    """Carrega a lista de atalhos do arquivo JSON."""
    try:
        with open(SHORTCUTS_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
            return data.get("shortcuts", [])
    except Exception:
        return []


def _save_shortcuts(shortcuts: list):
    """Salva a lista de atalhos no arquivo JSON."""
    try:
        with open(SHORTCUTS_FILE, "w", encoding="utf-8") as f:
            _json.dump({"shortcuts": shortcuts}, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.error("Erro ao salvar atalhos: %s", exc)


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
    # Modo espelhar janela: ID da janela selecionada (window_id do xdotool)
    window_mode: bool = False
    selected_window_id: Optional[int] = None
    selected_window_name: str = ""
    # Modo atual da conexão ativa
    current_mode: str = "mirror"
    # Referência ao screen_track ativo (para mode_change e resize)
    active_screen_track: Optional[object] = None
    # Referência à PC ativa (para renegociação)
    active_pc: Optional[object] = None
    # Função de envio WebSocket ativa (para respostas de mode_change/resize)
    active_ws_send: Optional[object] = None

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
# Listagem de janelas (para o modo Espelhar Janela)
# ==========================================================================

def get_window_list() -> list:
    """Retorna lista de janelas abertas no display atual usando xdotool.

    Cada item é um dict com:
      - id: window ID (hex string)
      - name: nome da janela (título)
      - pid: PID do processo dono (se disponível)
    """
    if not shutil.which("xdotool"):
        log.warning("xdotool não encontrado; não é possível listar janelas.")
        return []

    try:
        result = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", ""],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return []

        window_ids = result.stdout.strip().splitlines()
        windows = []
        seen = set()

        for wid_hex in window_ids:
            wid_hex = wid_hex.strip()
            if not wid_hex or wid_hex in seen:
                continue
            seen.add(wid_hex)

            # Pega o nome da janela
            try:
                name_result = subprocess.run(
                    ["xdotool", "getwindowname", wid_hex],
                    capture_output=True, text=True, timeout=1,
                )
                name = name_result.stdout.strip() if name_result.returncode == 0 else "(sem nome)"
            except Exception:
                name = "(sem nome)"

            # Ignora janelas sem nome útil
            if not name or name in ("", "(sem nome)"):
                continue

            # Pega PID
            pid = None
            try:
                pid_result = subprocess.run(
                    ["xdotool", "getwindowpid", wid_hex],
                    capture_output=True, text=True, timeout=1,
                )
                if pid_result.returncode == 0:
                    pid = int(pid_result.stdout.strip())
            except Exception:
                pass

            windows.append({
                "id": wid_hex,
                "name": name[:100],  # limita tamanho do nome
                "pid": pid,
            })

        return windows

    except Exception as exc:
        log.warning("Erro ao listar janelas: %s", exc)
        return []


def _get_window_geometry(window_id_hex: str):
    """Pega a geometria (x, y, w, h) de uma janela via xdotool.

    Retorna dict {x, y, width, height} ou None se falhar.
    """
    try:
        geo_result = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", window_id_hex],
            capture_output=True, text=True, timeout=1,
        )
        if geo_result.returncode != 0:
            return None

        x = y = w = h = None
        for line in geo_result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("X="):
                try: x = int(line.split("=")[1])
                except: pass
            elif line.startswith("Y="):
                try: y = int(line.split("=")[1])
                except: pass
            elif line.startswith("WIDTH="):
                try: w = int(line.split("=")[1])
                except: pass
            elif line.startswith("HEIGHT="):
                try: h = int(line.split("=")[1])
                except: pass

        if x is None or y is None or w is None or h is None or w <= 0 or h <= 0:
            return None
        return {"x": x, "y": y, "width": w, "height": h}
    except Exception:
        return None


def _crop_window_from_frame(frame_bgr, monitor, geo):
    """Recorta a região de uma janela de um frame BGR da tela cheia.

    Parâmetros:
        frame_bgr: numpy array BGR da tela cheia
        monitor:   dict do monitor do mss (contém left, top, width, height)
        geo:       dict da geometria da janela {x, y, width, height}

    Retorna numpy array BGR recortado, ou None se fora dos limites.
    """
    rel_x = geo["x"] - monitor["left"]
    rel_y = geo["y"] - monitor["top"]
    w, h = geo["width"], geo["height"]

    src_h, src_w = frame_bgr.shape[:2]

    # Garante que não sai dos limites
    rel_x = max(0, rel_x)
    rel_y = max(0, rel_y)
    w = min(w, src_w - rel_x)
    h = min(h, src_h - rel_y)

    if w <= 0 or h <= 0:
        return None

    return frame_bgr[rel_y:rel_y + h, rel_x:rel_x + w]


# ==========================================================================
# Captura de tela -> VideoStreamTrack
# ==========================================================================

class ScreenCaptureTrack(VideoStreamTrack):
    """Captura a tela com mss e entrega frames pro WebRTC.

    Três modos:
      - "mirror": captura o monitor principal do PC e envia pro celular.
      - "extend": cria um display virtual via Xvfb e transmite ele.
        Funciona em qualquer hardware (igual ao SpaceDesk).
      - "window": espelha apenas uma janela específica selecionada pelo usuário.
    """

    # Limites do ajuste automático de qualidade
    _AUTO_LOAD_HIGH = 0.85
    _AUTO_LOAD_LOW = 0.4
    _AUTO_COOLDOWN_SECONDS = 2.5

    def __init__(self, quality: str = DEFAULT_QUALITY, mode: str = "mirror",
                 phone_w: int = 0, phone_h: int = 0):
        super().__init__()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._sct = None
        self._monitor = None
        self._time_base = fractions.Fraction(1, 90000)

        # Display virtual Xvfb (modo Estender)
        self._virtual_display = None
        self._virtual_display_info = None

        # Cache de geometria da janela para modo espelhar janela
        self._window_geo_cache = None   # {x, y, width, height}
        self._window_geo_frame = 0       # último frame em que atualizou

        # Dimensões da tela do celular (para adaptar o vídeo)
        self._phone_w = phone_w if phone_w > 0 else None
        self._phone_h = phone_h if phone_h > 0 else None

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
            # Usa resolução do celular se disponível, senão padrão 1280x800
            vd_w = min(self._phone_w or 1280, 3840)
            vd_h = min(self._phone_h or 800, 2160)
            # Garante mínimo razoável
            vd_w = max(vd_w, 320)
            vd_h = max(vd_h, 240)
            log.info("Criando display virtual via Xvfb (%dx%d)...", vd_w, vd_h)
            vd = XvfbVirtualDisplay(width=vd_w, height=vd_h)
            success, result, info = vd.start()
            if success:
                self._virtual_display = vd
                self._virtual_display_info = info
                set_active_display(vd)
                log.info("Display virtual criado em %s (%s).", result, info.get("resolution", "?"))
                return "extend", -1, None, None
            else:
                reason = f"Xvfb falhou: {result}"
                log.warning("Falha ao criar display virtual: %s", reason)
                return "mirror", 1, None, reason

        # Xvfb não instalado
        reason = (
            "Xvfb não encontrado. Instale com:\n"
            "  sudo apt install xvfb xdotool openbox x11-xserver-utils x11-apps xterm\n"
            "(Debian/Ubuntu) ou:\n"
            "  sudo dnf install xorg-x11-server-Xvfb xdotool openbox xorg-x11-server-utils xorg-x11-apps xterm\n"
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

        self._draw_cursor_arrow(pil_img, rel_x, rel_y, src_w, src_h)

    def _draw_cursor_arrow(self, pil_img, cursor_x, cursor_y, src_w, src_h):
        """Desenha uma seta de cursor em (cursor_x, cursor_y) do frame original.

        Parâmetros:
            pil_img:    imagem PIL (já redimensionada para o target)
            cursor_x:   posição X do cursor no display original (pixels)
            cursor_y:   posição Y do cursor no display original (pixels)
            src_w:      largura do display original (pixels)
            src_h:      altura do display original (pixels)
        """
        if not (0 <= cursor_x < src_w and 0 <= cursor_y < src_h):
            return  # cursor fora da área

        scale = pil_img.height / src_h
        x = cursor_x * scale
        y = cursor_y * scale

        s = max(10, int(pil_img.height * 0.035))
        points = [
            (x, y), (x, y + s),
            (x + s * 0.35, y + s * 0.75), (x + s * 0.55, y + s * 1.0),
            (x + s * 0.72, y + s * 0.88), (x + s * 0.5, y + s * 0.6),
            (x + s * 0.85, y + s * 0.52),
        ]
        draw = ImageDraw.Draw(pil_img)
        draw.polygon(points, fill=(255, 255, 255), outline=(0, 0, 0))

    def _draw_cursor_virtual(self, pil_img, src_w, src_h):
        """Desenha o cursor no display virtual Xvfb.

        Usa xdotool getmouselocation no DISPLAY do Xvfb para pegar a
        posição do mouse e desenha a seta sobre o frame.
        """
        if not self._virtual_display or not self._virtual_display.is_running():
            return
        try:
            pos = self._virtual_display.get_mouse_position()
            if pos is None:
                return
            cx, cy = pos
            self._draw_cursor_arrow(pil_img, cx, cy, src_w, src_h)
        except Exception:
            pass

    def _letterbox(self, pil_img):
        """Adiciona letterboxing/pillarboxing para enquadrar o conteúdo na
        resolução do celular, sem cortar nenhuma parte da imagem.

        Se as dimensões do celular não forem conhecidas, retorna a imagem
        original sem alteração.
        """
        if not self._phone_w or not self._phone_h:
            return pil_img

        phone_w, phone_h = self._phone_w, self._phone_h
        img_w, img_h = pil_img.size

        # Se a imagem já tem o aspect ratio do celular, só redimensiona
        if abs(img_w / img_h - phone_w / phone_h) < 0.02:
            return pil_img.resize((phone_w, phone_h), Image.BILINEAR)

        # Calcula escala para caber dentro da tela do celular
        scale = min(phone_w / img_w, phone_h / img_h)
        new_w = int(img_w * scale)
        new_h = int(img_h * scale)

        # Redimensiona mantendo proporção
        resized = pil_img.resize((new_w, new_h), Image.BILINEAR)

        # Cria canvas preto nas dimensões do celular
        canvas = Image.new("RGB", (phone_w, phone_h), (0, 0, 0))

        # Centraliza a imagem no canvas
        x_offset = (phone_w - new_w) // 2
        y_offset = (phone_h - new_h) // 2
        canvas.paste(resized, (x_offset, y_offset))

        return canvas

    def _resample_filter(self, target_h: int) -> int:
        """Filtro de reamostragem: NEAREST (mais leve) para as qualidades mais
        baixas, onde a perda de nitidez é imperceptível e a economia de CPU
        ajuda hardware fraco; BILINEAR (mais suave) do 480p pra cima."""
        return Image.NEAREST if target_h <= 240 else Image.BILINEAR

    def _aligned_dimensions(self, src_w: int, src_h: int, target_h: int) -> tuple:
        """Calcula (largura, altura) de saída arredondadas para múltiplos de 16px.

        Codecs de vídeo (VP8/H.264, usados pelo aiortc) trabalham internamente
        em blocos de 16x16 pixels. Quando a largura ou altura pedida não é
        múltipla de 16, o codec completa o "resto" com dados de preenchimento
        (às vezes lixo de memória) — isso aparecia como uma faixa
        branca/cinza colada numa borda do vídeo, mais visível no modo
        Estender quando o celular ficava na vertical (o vídeo era esticado
        pra ocupar a tela toda, ampliando essa faixa). Arredondar as
        dimensões pra múltiplos de 16 antes de gerar o frame elimina esse
        preenchimento.
        """
        aligned_h = max(16, round(target_h / 16) * 16)
        raw_w = src_w * (aligned_h / src_h)
        aligned_w = max(16, round(raw_w / 16) * 16)
        return aligned_w, aligned_h

    def _capture_and_convert(self):
        """Captura a tela e converte para frame do WebRTC.

        Três caminhos:
          1. Xvfb (extend): pega frame do display virtual
          2. Janela (window): captura apenas uma janela específica
          3. Normal (mirror): captura monitor principal com mss

        Nota BGR: mss/Xvfb/capture_window entregam BGR. PIL.Image.fromarray()
        num array BGR cria uma imagem PIL com canais "BGR" — mas PIL sempre
        interpreta como RGB. No entanto, ao passar de volta para numpy e
        usar VideoFrame.from_ndarray(..., format="bgr24"), os canais são
        interpretados como BGR. Então o ciclo BGR→PIL(BGR interpretado como
        RGB)→numpy→bgr24 resulta em cores trocadas (R↔B).

        Solução: Usar cv2.resize (opera em BGR nativamente) para o resize
        inicial, e só converter para PIL para desenhar o cursor (operação
        que não depende de cor). Depois volta para BGR numpy.
        """

        # --- Caminho 1: Display virtual Xvfb (modo Estender) ---
        if self._virtual_display and self._virtual_display.is_running():
            frame_bgr = self._virtual_display.get_frame()
            if frame_bgr is not None:
                src_h, src_w = frame_bgr.shape[:2]
                target_w, target_h = self._aligned_dimensions(src_w, src_h, self._target_h)

                # Usa cv2.resize diretamente em BGR (sem conversão de canais)
                frame_resized = cv2.resize(frame_bgr, (target_w, target_h),
                                           interpolation=cv2.INTER_LINEAR if target_h > 240 else cv2.INTER_NEAREST)

                # Converte para PIL apenas para desenhar o cursor
                # cv2 BGR -> PIL RGB (cvtColor): B,G,R -> R,G,B
                frame_rgb = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
                pil_img = Image.fromarray(frame_rgb)

                # Desenha cursor do mouse no display virtual (Xvfb não renderiza cursor)
                self._draw_cursor_virtual(pil_img, src_w, src_h)

                # Adaptar à resolução do celular se conhecida
                if self._phone_w and self._phone_h:
                    phone_w, phone_h = self._phone_w, self._phone_h
                    a_phone_w = max(16, round(phone_w / 16) * 16)
                    a_phone_h = max(16, round(phone_h / 16) * 16)
                    pil_img = self._letterbox(pil_img)
                    pil_img = pil_img.resize((a_phone_w, a_phone_h), Image.BILINEAR)

                # PIL RGB -> numpy RGB -> cv2 BGR para o VideoFrame
                out_rgb = np.ascontiguousarray(np.array(pil_img))
                out_bgr = cv2.cvtColor(out_rgb, cv2.COLOR_RGB2BGR)
                return VideoFrame.from_ndarray(out_bgr, format="bgr24")

            # get_frame() retornou None (não deveria acontecer mais)
            log.warning("get_frame() retornou None, gerando frame de fallback")
            fallback = np.full(
                (self._target_h, max(2, int(self._target_h * 16 / 9)), 3),
                [46, 26, 26], dtype=np.uint8,
            )
            return VideoFrame.from_ndarray(fallback, format="bgr24")

        # --- Caminho 2: Captura normal com mss (modo Espelhar) ---
        # Se STATE.window_mode=True e janela selecionada, recorta só a janela
        # do frame completo. A geometria é cacheada (atualizada ~1x/s)
        # para evitar subprocess por frame.
        if self._sct is None:
            self._sct = mss.mss()
            if self._explicit_region is not None:
                self._monitor = self._explicit_region
            else:
                self._monitor = self._sct.monitors[self._monitor_index]

        raw = self._sct.grab(self._monitor)
        img = np.array(raw)[:, :, :3]  # BGRA -> BGR (fica em BGR o tempo todo)
        src_h, src_w = img.shape[:2]

        # --- Modo Espelhar Janela: recorta só a janela selecionada ---
        if STATE.window_mode and STATE.selected_window_id:
            # Atualiza cache de geometria a cada ~30 frames (~1x/s)
            if (self._window_geo_cache is None or
                    self._frame_count - self._window_geo_frame >= 30):
                geo = _get_window_geometry(STATE.selected_window_id)
                if geo is not None:
                    self._window_geo_cache = geo
                    self._window_geo_frame = self._frame_count
                else:
                    log.debug("Não conseguiu obter geometria da janela %s", STATE.selected_window_id)

            if self._window_geo_cache is not None:
                cropped = _crop_window_from_frame(img, self._monitor, self._window_geo_cache)
                if cropped is not None and cropped.size > 0:
                    img = cropped
                    src_h, src_w = img.shape[:2]
                    log.debug("Janela recortada: %dx%d", src_w, src_h)

        target_w, target_h = self._aligned_dimensions(src_w, src_h, self._target_h)

        # Usa cv2.resize em BGR
        frame_resized = cv2.resize(img, (target_w, target_h),
                                    interpolation=cv2.INTER_LINEAR if target_h > 240 else cv2.INTER_NEAREST)

        # BGR -> RGB para PIL (cursor)
        frame_rgb = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(frame_rgb)

        self._draw_cursor(pil_img)

        # Adaptar à resolução do celular se conhecida
        if self._phone_w and self._phone_h:
            phone_w, phone_h = self._phone_w, self._phone_h
            a_phone_w = max(16, round(phone_w / 16) * 16)
            a_phone_h = max(16, round(phone_h / 16) * 16)
            pil_img = self._letterbox(pil_img)
            pil_img = pil_img.resize((a_phone_w, a_phone_h), Image.BILINEAR)

        # PIL RGB -> BGR para VideoFrame
        out_rgb = np.ascontiguousarray(np.array(pil_img))
        out_bgr = cv2.cvtColor(out_rgb, cv2.COLOR_RGB2BGR)
        return VideoFrame.from_ndarray(out_bgr, format="bgr24")

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
            set_active_display(None)
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

                # Dimensões do celular (se enviadas pelo app)
                phone_w = data.get("screenWidth", 0)
                phone_h = data.get("screenHeight", 0)

                # Cria a track de captura de tela
                screen_track = ScreenCaptureTrack(
                    quality=quality, mode=requested_mode,
                    phone_w=phone_w, phone_h=phone_h,
                )
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

            # Mudança de modo (Espelhar <-> Estender) durante conexão ativa
            elif mtype == "mode_change" and screen_track is not None:
                new_mode = data.get("mode", "mirror")
                if new_mode not in ("mirror", "extend"):
                    new_mode = "mirror"
                current = screen_track.resolved_mode
                if new_mode != current:
                    log.info("Pedido de mudança de modo: %s -> %s", current, new_mode)
                    # Recria o ScreenCaptureTrack com o novo modo
                    phone_w = screen_track._phone_w or 0
                    phone_h = screen_track._phone_h or 0
                    old_track = screen_track
                    new_track = ScreenCaptureTrack(
                        quality=STATE.quality,
                        mode=new_mode,
                        phone_w=phone_w,
                        phone_h=phone_h,
                    )
                    STATE.active_screen_track = new_track
                    STATE.current_mode = new_track.resolved_mode
                    # Para o track antigo (fecha Xvfb se estava em extend)
                    old_track.close()
                    # Substitui no relay e na PeerConnection
                    if pc is not None:
                        sender = pc.getSenders()[0] if pc.getSenders() else None
                        if sender:
                            try:
                                sender.replaceTrack(relay.subscribe(new_track))
                                log.info("Track substituída: modo %s -> %s", current, new_track.resolved_mode)
                            except Exception as exc:
                                log.warning("Falha ao substituir track: %s", exc)
                    # Responde com o modo real resolvido
                    await ws.send_json({
                        "type": "mode_changed",
                        "mode": new_track.resolved_mode,
                        "modeFallbackReason": new_track.fallback_reason,
                    })

            # Redimensionamento do Xvfb (rotação do celular)
            elif mtype == "resize" and screen_track is not None:
                new_w = int(data.get("width", 0))
                new_h = int(data.get("height", 0))
                if new_w > 0 and new_h > 0:
                    log.info("Pedido de redimensionamento: %dx%d", new_w, new_h)
                    vd = screen_track._virtual_display
                    if vd and vd.is_running():
                        # Redimensiona o Xvfb
                        old_w, old_h = vd.width, vd.height
                        if old_w != new_w or old_h != new_h:
                            success = vd.resize(new_w, new_h)
                            if success:
                                screen_track._phone_w = new_w
                                screen_track._phone_h = new_h
                                log.info("Xvfb redimensionado: %dx%d -> %dx%d", old_w, old_h, new_w, new_h)
                                await ws.send_json({
                                    "type": "resize_ok",
                                    "width": new_w,
                                    "height": new_h,
                                })
                            else:
                                log.warning("Falha ao redimensionar Xvfb")
                                await ws.send_json({
                                    "type": "resize_error",
                                    "message": "Falha ao redimensionar Xvfb",
                                })
                        else:
                            # Mesma resolução, apenas atualiza
                            screen_track._phone_w = new_w
                            screen_track._phone_h = new_h
                    else:
                        # Não está em modo extend, apenas atualiza dimensões do celular
                        screen_track._phone_w = new_w
                        screen_track._phone_h = new_h
                        log.info("Dimensões do celular atualizadas: %dx%d (modo %s)", new_w, new_h, screen_track.resolved_mode)

            # Execução de atalho via WebSocket
            elif mtype == "execute_shortcut":
                shortcut_name = str(data.get("name", "")).strip()
                if shortcut_name:
                    shortcuts = _load_shortcuts()
                    shortcut = next((s for s in shortcuts if s["name"] == shortcut_name), None)
                    if shortcut:
                        _execute_shortcut_command(shortcut["command"])
                        log.info("Atalho executado (via WS): %s", shortcut_name)
                    else:
                        log.warning("Atalho não encontrado (via WS): %s", shortcut_name)

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
        "current_mode": STATE.current_mode,
    })


async def windows_handler(request: web.Request):
    """Endpoint REST que lista todas as janelas abertas no display.

    GET /windows -> JSON com lista de janelas [{id, name, pid}, ...]
    Usado pelo app Android e pela UI Tkinter para mostrar as opções.
    """
    windows = get_window_list()
    return web.json_response({"windows": windows})


# ==========================================================================
# Atalhos (REST + execução)
# ==========================================================================

async def shortcuts_handler(request: web.Request):
    """Endpoint REST para gerenciar atalhos.

    GET /shortcuts  -> lista de {name, command} (usado pelo app Android).
    POST /shortcuts -> adiciona um atalho (usado pelo servidor/UI).
    DELETE /shortcuts -> remove um atalho por nome (query: ?name=...).
    POST /shortcuts/execute -> executa um atalho no displaydigital.
    """
    if request.method == "GET":
        shortcuts = _load_shortcuts()
        # Retorna só os nomes (o app mostra os nomes, não os comandos)
        return web.json_response({"shortcuts": [{"name": s["name"]} for s in shortcuts]})

    elif request.method == "POST":
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "JSON inválido"}, status=400)

        name = str(data.get("name", "")).strip()
        command = str(data.get("command", "")).strip()
        if not name or not command:
            return web.json_response({"error": "'name' e 'command' são obrigatórios"}, status=400)

        shortcuts = _load_shortcuts()
        # Atualiza se já existe, ou adiciona novo
        existing = next((i for i, s in enumerate(shortcuts) if s["name"] == name), None)
        if existing is not None:
            shortcuts[existing]["command"] = command
        else:
            shortcuts.append({"name": name, "command": command})
        _save_shortcuts(shortcuts)
        log.info("Atalho salvo: %s", name)
        return web.json_response({"ok": True, "shortcuts": [{"name": s["name"]} for s in shortcuts]})

    elif request.method == "DELETE":
        name = request.query.get("name", "").strip()
        if not name:
            return web.json_response({"error": "?name= é obrigatório"}, status=400)

        shortcuts = _load_shortcuts()
        new_shortcuts = [s for s in shortcuts if s["name"] != name]
        if len(new_shortcuts) == len(shortcuts):
            return web.json_response({"error": "Atalho não encontrado"}, status=404)
        _save_shortcuts(new_shortcuts)
        log.info("Atalho removido: %s", name)
        return web.json_response({"ok": True, "shortcuts": [{"name": s["name"]} for s in new_shortcuts]})

    return web.json_response({"error": "Método não permitido"}, status=405)


async def shortcuts_execute_handler(request: web.Request):
    """Executa um atalho no displaydigital (segunda tela Xvfb).

    POST /shortcuts/execute  body: {"name": "..."}
    O comando é executado no DISPLAY do Xvfb ativo.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "JSON inválido"}, status=400)

    name = str(data.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "'name' é obrigatório"}, status=400)

    shortcuts = _load_shortcuts()
    shortcut = next((s for s in shortcuts if s["name"] == name), None)
    if shortcut is None:
        return web.json_response({"error": f"Atalho '{name}' não encontrado"}, status=404)

    command = shortcut["command"]
    _execute_shortcut_command(command)
    return web.json_response({"ok": True, "executed": name})


def _execute_shortcut_command(command: str):
    """Executa um comando no displaydigital (Xvfb ativo)."""
    vd = get_active_display()
    env = None
    if vd and vd.is_running() and vd.display_name:
        env = {**os.environ, "DISPLAY": vd.display_name}
        log.info("Executando atalho no %s: %s", vd.display_name, command)
    else:
        log.info("Executando atalho no display principal: %s", command)

    try:
        subprocess.Popen(
            command,
            shell=True,
            env=env or os.environ,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        log.error("Erro ao executar atalho: %s", exc)


# ==========================================================================
# USB (ADB reverse automático — sem comando no terminal)
# ==========================================================================

USB_STATUS_LABELS = {
    "checking":     ("Cabo USB: verificando...", "gray"),
    "connected":    ("Cabo USB: pronto ✅", "#2e7d32"),
    "no_device":    ("Cabo USB: plugue e autorize depuração", "gray"),
    "unauthorized": ("Cabo USB: autorize a depuração no celular", "#b26a00"),
    "adb_missing":  ("Cabo USB: 'adb' não encontrado", "gray"),
    "error":        ("Cabo USB: erro ao aplicar adb reverse", "#c62828"),
}


def _parse_adb_devices(output: str) -> tuple[list[str], list[str]]:
    """Extrai (seriais_autorizados, seriais_nao_autorizados) da saída de `adb devices`."""
    ready: list[str] = []
    unauthorized: list[str] = []
    for line in output.splitlines()[1:]:  # primeira linha é o cabeçalho "List of devices attached"
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        serial, state = parts[0].strip(), parts[1].strip()
        if not serial:
            continue
        if state == "device":
            ready.append(serial)
        elif state == "unauthorized":
            unauthorized.append(serial)
    return ready, unauthorized


def _adb_reverse_loop():
    """Verifica se tem celular plugado e aplica `adb reverse` sozinho.

    Importante: o comando precisa mirar um serial específico (`adb -s <serial>
    reverse ...`). Sem isso, o adb recusa a operação com "error: more than one
    device/emulator" sempre que há mais de um dispositivo visível — o que é
    comum mesmo com só o cabo plugado, pois muitos celulares recentes têm
    "depuração sem fio" (adb por Wi-Fi) habilitada ao mesmo tempo, ou o PC
    tem um emulador/outro aparelho já pareado. Esse era o motivo do botão
    "Via cabo" às vezes não funcionar: o `adb reverse` global falhava
    silenciosamente e o app tentava conversar com um servidor que não
    existia em 127.0.0.1.
    """
    adb = shutil.which("adb")
    if adb is None:
        log.info("adb não encontrado; modo USB desativado.")
        STATE.usb_status = "adb_missing"
        return

    while True:
        try:
            result = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=5)
            ready, unauthorized = _parse_adb_devices(result.stdout)

            if ready:
                any_ok = False
                for serial in ready:
                    rev = subprocess.run(
                        [adb, "-s", serial, "reverse", f"tcp:{PORT}", f"tcp:{PORT}"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if rev.returncode == 0:
                        any_ok = True
                    else:
                        log.warning(
                            "adb reverse falhou para %s: %s",
                            serial, rev.stderr.strip() or rev.stdout.strip(),
                        )
                STATE.usb_status = "connected" if any_ok else "error"
            elif unauthorized:
                STATE.usb_status = "unauthorized"
            else:
                STATE.usb_status = "no_device"
        except Exception as exc:
            log.warning("Erro ao verificar dispositivos USB: %s", exc)
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
    from tkinter import ttk

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

        # ---- Paleta de cores — igual à do app Android (ver Theme.kt) ----
        BG_MAIN = "#0F172A"       # background do app
        BG_SURFACE = "#0B0B0B"    # surface (topo/menus) do app
        BG_CARD = "#182338"       # tom intermediário, pros "cartões"/abas
        BG_FIELD = "#1E293B"      # fundo de campos de entrada/lista
        FG_TEXT = "#F1F5F9"
        FG_MUTED = "#94A3B8"
        ACCENT_BLUE = "#38BDF8"   # primary do app
        ACCENT_BLUE_DK = "#0EA5E9"  # secondary do app
        ACCENT_ORANGE = "#EC5E00"   # tertiary do app (laranja do logo)
        COLOR_OK = "#22C55E"
        COLOR_WARN = "#F59E0B"
        COLOR_DANGER = "#DC2626"
        COLOR_DANGER_DK = "#991B1B"

        root = tk.Tk()
        root.title(APP_NAME)
        root.configure(bg=BG_MAIN)

        # Escala pra telas HiDPI/4K
        try:
            dpi = root.winfo_fpixels("1i")
            ui_scale = max(1.0, min(dpi / 96.0, 2.5))
        except Exception:
            ui_scale = 1.0
        root.tk.call("tk", "scaling", ui_scale * (96.0 / 72.0))

        def sc(px: int) -> int:
            return int(round(px * ui_scale))

        # Janela mais baixa que antes — o conteúdo agora fica em abas em
        # vez de tudo empilhado numa coluna só (por isso 980px de altura
        # não é mais necessário).
        base_w, base_h = 360, 700
        win_w = min(sc(base_w), int(root.winfo_screenwidth() * 0.9))
        win_h = min(sc(base_h), int(root.winfo_screenheight() * 0.9))
        pos_x = max(0, (root.winfo_screenwidth() - win_w) // 2)
        pos_y = max(0, (root.winfo_screenheight() - win_h) // 3)
        root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        root.minsize(sc(300), sc(480))
        root.resizable(True, True)

        # ---- Estilo ttk (Notebook/Combobox não têm equivalente puro em tk) ----
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TNotebook", background=BG_MAIN, borderwidth=0)
        style.configure(
            "TNotebook.Tab", background=BG_SURFACE, foreground=FG_MUTED,
            padding=(sc(14), sc(7)), font=("Sans", 9, "bold"), borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", BG_CARD)],
            foreground=[("selected", ACCENT_BLUE)],
        )
        style.configure("Card.TFrame", background=BG_CARD)
        style.configure(
            "TCombobox", fieldbackground=BG_FIELD, background=BG_FIELD,
            foreground=FG_TEXT, arrowcolor=FG_TEXT, borderwidth=0,
            selectbackground=BG_FIELD, selectforeground=FG_TEXT,
            insertcolor=FG_TEXT, bordercolor=BG_FIELD,
            lightcolor=BG_FIELD, darkcolor=BG_FIELD, relief="flat",
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", BG_FIELD), ("disabled", BG_FIELD), ("!disabled", BG_FIELD)],
            selectbackground=[("readonly", BG_FIELD)],
            selectforeground=[("readonly", FG_TEXT)],
            foreground=[("readonly", FG_TEXT), ("disabled", FG_MUTED)],
            background=[("readonly", BG_FIELD), ("active", BG_FIELD)],
        )
        # A lista suspensa do Combobox é uma Listbox "crua" do Tk por baixo
        # dos panos — não segue o ttk.Style, precisa ser configurada à parte.
        root.option_add("*TCombobox*Listbox.background", BG_FIELD)
        root.option_add("*TCombobox*Listbox.foreground", FG_TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_BLUE)
        root.option_add("*TCombobox*Listbox.selectForeground", "#00202B")

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

            win = tk.Toplevel(root, bg=BG_MAIN)
            win.title(f"{APP_NAME} — Terminal")
            log_w = min(sc(640), int(root.winfo_screenwidth() * 0.9))
            log_h = min(sc(420), int(root.winfo_screenheight() * 0.9))
            win.geometry(f"{log_w}x{log_h}")
            win.minsize(sc(360), sc(240))

            text = scrolledtext.ScrolledText(
                win, bg="#0b0b0b", fg="#e6e6e6", insertbackground="#e6e6e6",
                font=("Consolas", 9), borderwidth=0,
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

        # ================= Cabeçalho (fixo, sempre visível) =================
        header = tk.Frame(root, bg=BG_SURFACE)
        header.pack(fill="x")

        tk.Label(
            header, text=APP_NAME, font=("Sans", 17, "bold"),
            bg=BG_SURFACE, fg=ACCENT_BLUE,
        ).pack(pady=(14, 2))
        tk.Label(
            header, text="PIN de conexão", font=("Sans", 10),
            bg=BG_SURFACE, fg=FG_MUTED,
        ).pack()
        tk.Label(
            header, text=STATE.pin, font=("Consolas", 30, "bold"),
            bg=BG_SURFACE, fg=ACCENT_ORANGE,
        ).pack(pady=(0, 8))

        qr_photo = _build_qr_photo(root, ui_scale)
        if qr_photo is not None:
            qr_wrap = tk.Frame(header, bg="white", padx=6, pady=6)
            qr_wrap.pack(pady=(0, 6))
            qr_label = tk.Label(qr_wrap, image=qr_photo, bg="white")
            qr_label.image = qr_photo
            qr_label.pack()
            tk.Label(
                header, text="Escaneie no app para conectar",
                font=("Sans", 9), bg=BG_SURFACE, fg=FG_MUTED,
            ).pack(pady=(0, 12))
        else:
            tk.Frame(header, bg=BG_SURFACE, height=8).pack()

        control_var = tk.BooleanVar(value=STATE.allow_control)

        def on_toggle():
            STATE.allow_control = control_var.get()
            log.info("Permitir controle: %s", STATE.allow_control)

        tk.Checkbutton(
            root, text="Permitir controle (mouse/teclado)",
            variable=control_var, command=on_toggle,
            bg=BG_MAIN, fg=FG_TEXT, font=("Sans", 10),
            activebackground=BG_MAIN, activeforeground=FG_TEXT,
            selectcolor=BG_FIELD, borderwidth=0, highlightthickness=0,
        ).pack(pady=(10, 6))

        # ================= Abas =================
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(2, 6))

        tab_window = tk.Frame(notebook, bg=BG_CARD, padx=14, pady=14)
        tab_shortcuts = tk.Frame(notebook, bg=BG_CARD, padx=14, pady=14)
        tab_system = tk.Frame(notebook, bg=BG_CARD, padx=14, pady=14)
        notebook.add(tab_window, text="  Janela  ")
        notebook.add(tab_shortcuts, text="  Atalhos  ")
        notebook.add(tab_system, text="  Sistema  ")

        def _section_label(parent, text):
            tk.Label(
                parent, text=text, font=("Sans", 10, "bold"),
                bg=BG_CARD, fg=ACCENT_BLUE, anchor="w",
            ).pack(fill="x", pady=(0, 6))

        def _hint_label(parent, text, fg=FG_MUTED):
            lbl = tk.Label(
                parent, text=text, font=("Sans", 8),
                bg=BG_CARD, fg=fg, anchor="w", justify="left", wraplength=sc(280),
            )
            lbl.pack(fill="x", pady=(4, 0))
            return lbl

        def _flat_button(parent, text, command, accent=False, danger=False):
            bg = COLOR_DANGER if danger else (ACCENT_BLUE if accent else BG_FIELD)
            fg = "white" if danger else ("#00202B" if accent else FG_TEXT)
            active_bg = COLOR_DANGER_DK if danger else (ACCENT_BLUE_DK if accent else "#243248")
            return tk.Button(
                parent, text=text, command=command, font=("Sans", 9, "bold" if (accent or danger) else "normal"),
                bg=bg, fg=fg, activebackground=active_bg, activeforeground=fg,
                borderwidth=0, relief="flat", padx=10, pady=5,
                highlightthickness=0, cursor="hand2",
            )

        # --- Aba: Espelhar Janela ---
        _section_label(tab_window, "Espelhar uma janela específica")

        window_var = tk.StringVar(value="")
        window_combo = ttk.Combobox(
            tab_window, textvariable=window_var,
            state="readonly", width=40,
        )
        window_combo.pack(fill="x", pady=(0, 6))

        window_status_label = _hint_label(tab_window, "Clique em 'Atualizar' para ver as janelas")

        def refresh_windows():
            """Busca janelas abertas e atualiza o combobox."""
            windows = get_window_list()
            if not windows:
                window_status_label.config(
                    text="Nenhuma janela encontrada (xdotool necessário)",
                    fg=COLOR_WARN,
                )
                window_combo["values"] = []
                window_var.set("")
                STATE.window_mode = False
                STATE.selected_window_id = None
                STATE.selected_window_name = ""
                return

            names = [f"{w['name']} (PID:{w['pid'] or '?'})" for w in windows]
            window_combo["values"] = names
            window_status_label.config(
                text=f"{len(windows)} janela(s) encontrada(s)",
                fg=COLOR_OK,
            )

            # Guarda mapeamento nome -> window id para uso ao selecionar
            tab_window._window_map = {
                f"{w['name']} (PID:{w['pid'] or '?'})": w
                for w in windows
            }

            # Se já tinha uma janela selecionada, re-seleciona
            if STATE.selected_window_name:
                for n in names:
                    if STATE.selected_window_name in n:
                        window_var.set(n)
                        break

        def on_window_selected(event=None):
            """Quando o usuário seleciona uma janela no combobox."""
            sel = window_var.get()
            wmap = getattr(tab_window, "_window_map", {})
            win = wmap.get(sel)
            if win:
                STATE.window_mode = True
                STATE.selected_window_id = win["id"]
                STATE.selected_window_name = win["name"]
                window_status_label.config(
                    text=f"Janela selecionada: {win['name']}",
                    fg=ACCENT_BLUE,
                )
                log.info("Janela selecionada: %s (ID: %s)", win["name"], win["id"])
            else:
                STATE.window_mode = False
                STATE.selected_window_id = None
                STATE.selected_window_name = ""

        window_combo.bind("<<ComboboxSelected>>", on_window_selected)

        _flat_button(tab_window, "Atualizar janelas", refresh_windows).pack(pady=(6, 0))

        tab_window._window_map = {}

        # --- Aba: Atalhos (comandos que abrem programas na 2ª tela) ---
        _section_label(tab_shortcuts, "Atalhos do Display Virtual")
        _hint_label(
            tab_shortcuts,
            "Comandos executados na tela estendida (modo Estender). Ficam "
            "salvos e continuam aqui mesmo depois de fechar e abrir o "
            f"{APP_NAME} de novo.",
        ).pack(fill="x", pady=(0, 8))

        shortcut_listbox_frame = tk.Frame(tab_shortcuts, bg=BG_CARD)
        shortcut_listbox_frame.pack(fill="x", pady=(0, 8))

        shortcut_listbox = tk.Listbox(
            shortcut_listbox_frame, height=5, font=("Consolas", 9),
            bg=BG_FIELD, fg=FG_TEXT, selectbackground=ACCENT_BLUE,
            selectforeground="#00202B", borderwidth=0, highlightthickness=0,
        )
        shortcut_scrollbar = tk.Scrollbar(shortcut_listbox_frame, orient="vertical", command=shortcut_listbox.yview)
        shortcut_listbox.configure(yscrollcommand=shortcut_scrollbar.set)
        shortcut_listbox.pack(side="left", fill="both", expand=True)
        shortcut_scrollbar.pack(side="right", fill="y")

        entry_frame = tk.Frame(tab_shortcuts, bg=BG_CARD)
        entry_frame.pack(fill="x", pady=(0, 4))

        tk.Label(
            entry_frame, text="Nome:", font=("Sans", 9),
            bg=BG_CARD, fg=FG_MUTED,
        ).grid(row=0, column=0, sticky="w", padx=(0, 4))
        shortcut_name_entry = tk.Entry(
            entry_frame, font=("Consolas", 10), width=25,
            bg=BG_FIELD, fg=FG_TEXT, insertbackground=FG_TEXT,
            borderwidth=0, highlightthickness=1,
            highlightbackground=BG_FIELD, highlightcolor=ACCENT_BLUE,
        )
        shortcut_name_entry.grid(row=0, column=1, sticky="ew", padx=(0, 4), pady=2)

        tk.Label(
            entry_frame, text="Comando:", font=("Sans", 9),
            bg=BG_CARD, fg=FG_MUTED,
        ).grid(row=1, column=0, sticky="w", padx=(0, 4), pady=(4, 0))
        shortcut_cmd_entry = tk.Entry(
            entry_frame, font=("Consolas", 10), width=25,
            bg=BG_FIELD, fg=FG_TEXT, insertbackground=FG_TEXT,
            borderwidth=0, highlightthickness=1,
            highlightbackground=BG_FIELD, highlightcolor=ACCENT_BLUE,
        )
        shortcut_cmd_entry.insert(0, "DISPLAY=:1 firefox &")
        shortcut_cmd_entry.grid(row=1, column=1, sticky="ew", padx=(0, 4), pady=(4, 2))

        entry_frame.columnconfigure(1, weight=1)

        shortcut_status_label = _hint_label(tab_shortcuts, "Nenhum atalho definido")

        def refresh_shortcuts():
            """Atualiza a lista de atalhos."""
            shortcut_listbox.delete(0, tk.END)
            shortcuts = _load_shortcuts()
            for s in shortcuts:
                shortcut_listbox.insert(tk.END, f"{s['name']}  ->  {s['command']}")
            n = len(shortcuts)
            shortcut_status_label.config(
                text=f"{n} atalho(s) salvo(s)" if n else "Nenhum atalho definido",
                fg=COLOR_OK if n else FG_MUTED,
            )

        def add_shortcut():
            """Adiciona atalho usando as caixas de texto da janela principal."""
            name = shortcut_name_entry.get().strip()
            command = shortcut_cmd_entry.get().strip()
            if not name or not command:
                shortcut_status_label.config(text="Nome e comando são obrigatórios!", fg=COLOR_WARN)
                return
            shortcuts = _load_shortcuts()
            existing = next((i for i, s in enumerate(shortcuts) if s["name"] == name), None)
            if existing is not None:
                shortcuts[existing]["command"] = command
                shortcut_status_label.config(text=f"Atalho '{name}' atualizado!", fg=ACCENT_BLUE)
            else:
                shortcuts.append({"name": name, "command": command})
                shortcut_status_label.config(text=f"Atalho '{name}' adicionado!", fg=COLOR_OK)
            _save_shortcuts(shortcuts)
            refresh_shortcuts()
            shortcut_name_entry.delete(0, tk.END)
            shortcut_cmd_entry.delete(0, tk.END)
            shortcut_cmd_entry.insert(0, "DISPLAY=:1 firefox &")
            shortcut_name_entry.focus_set()
            log.info("Atalho adicionado/editado: %s", name)

        def remove_shortcut():
            """Remove o atalho selecionado."""
            sel = shortcut_listbox.curselection()
            if not sel:
                shortcut_status_label.config(text="Selecione um atalho para remover", fg=COLOR_WARN)
                return
            idx = sel[0]
            shortcuts = _load_shortcuts()
            if 0 <= idx < len(shortcuts):
                name = shortcuts[idx]["name"]
                shortcuts.pop(idx)
                _save_shortcuts(shortcuts)
                refresh_shortcuts()
                log.info("Atalho removido: %s", name)

        shortcut_btn_frame = tk.Frame(tab_shortcuts, bg=BG_CARD)
        shortcut_btn_frame.pack(pady=(6, 0), fill="x")
        _flat_button(shortcut_btn_frame, "Adicionar", add_shortcut, accent=True).pack(side="left", padx=(0, 6))
        _flat_button(shortcut_btn_frame, "Remover", remove_shortcut).pack(side="left", padx=(0, 6))
        _flat_button(shortcut_btn_frame, "Atualizar", refresh_shortcuts).pack(side="left")

        # Carrega atalhos iniciais
        refresh_shortcuts()

        # --- Aba: Sistema ---
        _section_label(tab_system, "Status")

        usb_label = tk.Label(
            tab_system, text="Cabo USB: verificando...", font=("Sans", 9),
            bg=BG_CARD, fg=FG_MUTED, anchor="w",
        )
        usb_label.pack(fill="x", pady=(0, 12))

        def poll_usb():
            text, color = USB_STATUS_LABELS.get(STATE.usb_status, USB_STATUS_LABELS["checking"])
            usb_label.config(text=text, fg=color)
            root.after(1500, poll_usb)

        poll_usb()

        _section_label(tab_system, "Diagnóstico")
        _flat_button(tab_system, "Ver terminal", open_log_viewer).pack(fill="x", pady=(0, 16))

        _section_label(tab_system, "Rede")
        _hint_label(tab_system, f"Rede local, porta {PORT}").pack(fill="x")
        _hint_label(tab_system, "Sem PIN, ninguém conecta.").pack(fill="x")

        # ================= Rodapé (fixo, sempre visível) =================
        footer = tk.Frame(root, bg=BG_MAIN)
        footer.pack(fill="x", pady=(0, 12))
        _flat_button(footer, "Encerrar servidor", shutdown, danger=True).pack()

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
    app.router.add_get("/windows", windows_handler)
    app.router.add_route("*", "/shortcuts", shortcuts_handler)
    app.router.add_post("/shortcuts/execute", shortcuts_execute_handler)
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
