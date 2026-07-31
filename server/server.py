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
import atexit
import concurrent.futures
import fractions
import ipaddress
import json
import logging
import os
import queue
import secrets
import shutil
import signal
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

# Atalhos padrão: seed inicial gravado em shortcuts.json quando o arquivo
# não existe ou está vazio. Garante que todo usuário novo já tenha os 3
# atalhos mais úteis prontos pra uso no display digital, sem precisar criar
# manualmente. Não sobrescreve atalhos criados/removidos pelo usuário.
_DEFAULT_SHORTCUTS = [
    {
        "name": "Configuração",
        "command": "DISPLAY=:1 gnome-control-center &",
    },
    {
        "name": "Alt+F4",
        "command": "DISPLAY=:1 xdotool key Alt+F4 &",
    },
    {
        "name": "Multitarefa",
        "command": "DISPLAY=:1 xdotool key Super &",
    },
]


def _load_shortcuts() -> list:
    """Carrega a lista de atalhos do arquivo JSON.

    Se o arquivo não existe ou está vazio, popula com `_DEFAULT_SHORTCUTS`
    e devolve a lista — garante que usuários novos já tenham os 3 atalhos
    básicos (Configuração, Alt+F4, Multitarefa) sem precisar criar nada.
    """
    try:
        with open(SHORTCUTS_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
            shortcuts = data.get("shortcuts", [])
        # Seed inicial: arquivo existe mas está vazio.
        if not shortcuts:
            log.info("shortcuts.json vazio — populando atalhos padrão.")
            _save_shortcuts(_DEFAULT_SHORTCUTS)
            return list(_DEFAULT_SHORTCUTS)
        return shortcuts
    except FileNotFoundError:
        log.info("shortcuts.json não existe — criando com atalhos padrão.")
        _save_shortcuts(_DEFAULT_SHORTCUTS)
        return list(_DEFAULT_SHORTCUTS)
    except Exception:
        return list(_DEFAULT_SHORTCUTS)


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
                 phone_w: int = 0, phone_h: int = 0, profile: str = "standard"):
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

        # Item 9: perfil de latência. Em low_latency (cabo USB), o server:
        # - usa INTER_NEAREST em vez de INTER_LINEAR no resize (mais rápido)
        # - pula o desenho do cursor (cortando 5-10ms por frame)
        # - aumenta o fps do capture_loop (no virtual_display.py)
        # - reduz qualidade JPEG interna
        self._profile = profile if profile in ("standard", "low_latency") else "standard"
        self._is_low_latency = (self._profile == "low_latency")

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

    def _draw_cursor(self, img_bgr):
        """Desenha um cursor de seta na posição do mouse, direto no array
        BGR (numpy) via cv2 — ver nota de performance em _draw_cursor_arrow."""
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

        self._draw_cursor_arrow(img_bgr, rel_x, rel_y, src_w, src_h)

    def _draw_cursor_arrow(self, img_bgr, cursor_x, cursor_y, src_w, src_h):
        """Desenha uma seta de cursor em (cursor_x, cursor_y) do frame original.

        Trabalha direto no array BGR (numpy) via cv2.fillPoly/polylines, em
        vez de converter pra PIL só pra desenhar a seta. Antes, TODO frame
        (não só quando ia desenhar cursor) passava por
        BGR→RGB→PIL→numpy→BGR — duas conversões de canal (cvtColor) e duas
        cópias completas do buffer, a cada frame, no fps alvo inteiro. Isso
        é custo de CPU/RAM real e evitável: agora o pipeline inteiro fica
        em BGR/numpy do início ao fim, e só usamos cv2 (que já opera sobre
        o buffer existente, sem alocar uma cópia num formato diferente).

        Parâmetros:
            img_bgr:    array numpy BGR (já redimensionado para o target),
                        modificado in-place.
            cursor_x:   posição X do cursor no display original (pixels)
            cursor_y:   posição Y do cursor no display original (pixels)
            src_w:      largura do display original (pixels)
            src_h:      altura do display original (pixels)
        """
        if not (0 <= cursor_x < src_w and 0 <= cursor_y < src_h):
            return  # cursor fora da área

        img_h = img_bgr.shape[0]
        scale = img_h / src_h
        x = cursor_x * scale
        y = cursor_y * scale

        s = max(10, int(img_h * 0.035))
        points = np.array([
            (x, y), (x, y + s),
            (x + s * 0.35, y + s * 0.75), (x + s * 0.55, y + s * 1.0),
            (x + s * 0.72, y + s * 0.88), (x + s * 0.5, y + s * 0.6),
            (x + s * 0.85, y + s * 0.52),
        ], dtype=np.int32).reshape((-1, 1, 2))
        cv2.fillPoly(img_bgr, [points], (255, 255, 255))
        cv2.polylines(img_bgr, [points], isClosed=True, color=(0, 0, 0),
                       thickness=1, lineType=cv2.LINE_AA)

    def _draw_cursor_virtual(self, img_bgr, src_w, src_h):
        """Desenha o cursor no display virtual Xvfb.

        Usa xdotool getmouselocation no DISPLAY do Xvfb para pegar a
        posição do mouse e desenha a seta sobre o frame (direto em BGR).
        """
        if not self._virtual_display or not self._virtual_display.is_running():
            return
        try:
            pos = self._virtual_display.get_mouse_position()
            if pos is None:
                return
            cx, cy = pos
            self._draw_cursor_arrow(img_bgr, cx, cy, src_w, src_h)
        except Exception:
            pass

    def _letterbox(self, img_bgr):
        """Adiciona letterboxing/pillarboxing para enquadrar o conteúdo na
        resolução do celular, sem cortar nenhuma parte da imagem.

        Versão cv2/numpy (opera em BGR) — antes era feita em PIL, exigindo
        mais uma conversão de canal + cópia completa do frame. Se as
        dimensões do celular não forem conhecidas, retorna a imagem
        original sem alteração.
        """
        if not self._phone_w or not self._phone_h:
            return img_bgr

        phone_w, phone_h = self._phone_w, self._phone_h
        img_h, img_w = img_bgr.shape[:2]

        # Se a imagem já tem o aspect ratio do celular, só redimensiona
        if abs(img_w / img_h - phone_w / phone_h) < 0.02:
            return cv2.resize(img_bgr, (phone_w, phone_h), interpolation=cv2.INTER_LINEAR)

        # Calcula escala para caber dentro da tela do celular
        scale = min(phone_w / img_w, phone_h / img_h)
        new_w = max(1, int(img_w * scale))
        new_h = max(1, int(img_h * scale))

        # Redimensiona mantendo proporção
        resized = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        # Cria canvas preto nas dimensões do celular e centraliza
        canvas = np.zeros((phone_h, phone_w, 3), dtype=np.uint8)
        x_offset = (phone_w - new_w) // 2
        y_offset = (phone_h - new_h) // 2
        canvas[y_offset:y_offset + new_h, x_offset:x_offset + new_w] = resized

        return canvas

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

        O pipeline inteiro fica em BGR/numpy do início (mss/Xvfb já
        entregam BGR) ao fim (VideoFrame.from_ndarray(..., format="bgr24")
        espera BGR) — sem nenhuma parada em PIL no meio. Isso evita duas
        conversões de canal (cvtColor) e duas cópias completas do frame
        que rodavam TODO frame só para poder usar ImageDraw/Image.resize
        (ver _draw_cursor_arrow/_letterbox para o detalhe).
        """

        # --- Caminho 1: Display virtual Xvfb (modo Estender) ---
        if self._virtual_display and self._virtual_display.is_running():
            frame_bgr = self._virtual_display.get_frame()
            if frame_bgr is not None:
                src_h, src_w = frame_bgr.shape[:2]
                target_w, target_h = self._aligned_dimensions(src_w, src_h, self._target_h)

                # Item 9: em low_latency, INTER_NEAREST é mais rápido (custo de nitidez).
                interp = cv2.INTER_NEAREST if self._is_low_latency else (
                    cv2.INTER_LINEAR if target_h > 240 else cv2.INTER_NEAREST
                )
                frame_resized = cv2.resize(frame_bgr, (target_w, target_h), interpolation=interp)

                # Desenha cursor do mouse no display virtual (Xvfb não renderiza cursor).
                # Item 9: em low_latency, pula o cursor (corta 5-10ms por frame).
                if not self._is_low_latency:
                    self._draw_cursor_virtual(frame_resized, src_w, src_h)

                # Adaptar à resolução do celular se conhecida
                if self._phone_w and self._phone_h:
                    phone_w, phone_h = self._phone_w, self._phone_h
                    a_phone_w = max(16, round(phone_w / 16) * 16)
                    a_phone_h = max(16, round(phone_h / 16) * 16)
                    frame_resized = self._letterbox(frame_resized)
                    frame_resized = cv2.resize(frame_resized, (a_phone_w, a_phone_h), interpolation=cv2.INTER_LINEAR)

                return VideoFrame.from_ndarray(np.ascontiguousarray(frame_resized), format="bgr24")

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

        # Item 6 (revisado): a versão anterior recortava a região central do
        # monitor para "preencher" a tela do celular sem barras pretas — mas
        # isso descartava uma fatia real da tela do PC (ex.: barra de tarefas,
        # bordas de janelas), o que é pior do que ver a tela inteira com
        # pequenas barras. Voltamos a nunca cortar: `_letterbox` abaixo
        # sempre mostra o monitor inteiro, com barras pretas só quando o
        # aspect ratio realmente não bate com o do celular.
        target_w, target_h = self._aligned_dimensions(src_w, src_h, self._target_h)

        # Item 9: em low_latency, sempre INTER_NEAREST (mais rápido).
        interp = cv2.INTER_NEAREST if self._is_low_latency else (
            cv2.INTER_LINEAR if target_h > 240 else cv2.INTER_NEAREST
        )
        frame_resized = cv2.resize(img, (target_w, target_h), interpolation=interp)

        # Item 9: em low_latency, pula desenho do cursor.
        if not self._is_low_latency:
            self._draw_cursor(frame_resized)

        # Adaptar à resolução do celular se conhecida
        if self._phone_w and self._phone_h:
            phone_w, phone_h = self._phone_w, self._phone_h
            a_phone_w = max(16, round(phone_w / 16) * 16)
            a_phone_h = max(16, round(phone_h / 16) * 16)
            frame_resized = self._letterbox(frame_resized)
            frame_resized = cv2.resize(frame_resized, (a_phone_w, a_phone_h), interpolation=cv2.INTER_LINEAR)

        return VideoFrame.from_ndarray(np.ascontiguousarray(frame_resized), format="bgr24")

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

def _parse_ice_candidate_sdp(sdp: str, sdp_mid: str, sdp_mline_index: int):
    """Item 9: parse de uma linha a=candidate do SDP para RTCIceCandidate.

    Linha SDP típica:
        "candidate:842163049 1 udp 1677729535 192.168.1.10 55554 typ srflx"

    aiortc expõe RTCIceCandidate com atributos separados (foundation,
    component, protocol, priority, ip, port, type, etc.). Este parser é
    defensivo: se algum campo faltar, retorna None e o chamador loga.
    """
    try:
        from aiortc import RTCIceCandidate
        from aiortc.rtcicetransport import candidate_from_sdp
        # aiortc tem `candidate_from_sdp` em rtcicetransport — mas não é API
        # pública estável. Tenta primeiro; se falhar, cai pra parse manual.
        try:
            cand = candidate_from_sdp(sdp.replace("candidate:", "", 1))
            cand.sdpMid = sdp_mid
            cand.sdpMLineIndex = sdp_mline_index
            return cand
        except Exception:
            pass

        # Parse manual (fallback).
        # Formato: candidate:<foundation> <component> <protocol> <priority> <ip> <port> typ <type> [raddr <ip>] [rport <port>]
        s = sdp.replace("candidate:", "", 1).strip()
        parts = s.split()
        if len(parts) < 7:
            return None
        foundation = parts[0]
        component = int(parts[1])
        protocol = parts[2].upper()
        priority = int(parts[3])
        ip = parts[4]
        port = int(parts[5])
        cand_type = "host"
        related_address = None
        related_port = None
        i = 6
        while i < len(parts):
            if parts[i] == "typ" and i + 1 < len(parts):
                cand_type = parts[i + 1]
                i += 2
            elif parts[i] == "raddr" and i + 1 < len(parts):
                related_address = parts[i + 1]
                i += 2
            elif parts[i] == "rport" and i + 1 < len(parts):
                related_port = int(parts[i + 1])
                i += 2
            else:
                i += 1

        cand = RTCIceCandidate(
            foundation=foundation,
            component=component,
            protocol=protocol,
            priority=priority,
            ip=ip,
            port=port,
            type=cand_type,
            relatedAddress=related_address,
            relatedPort=related_port,
            tcpType=None,
        )
        cand.sdpMid = sdp_mid
        cand.sdpMLineIndex = sdp_mline_index
        return cand
    except Exception as exc:
        log.debug("Parser de ICE candidate falhou: %s", exc)
        return None


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

            # Token QR criptografado (ND1.<b64>.<b64>) — alternativa ao PIN.
            # O app escaneia o QR e envia o token opaco; o server decripta,
            # valida expiração e, se válido, autentica a sessão (igual ao
            # "pin_ok"). Token expirado → "qr_token_error" com reason
            # "token_expired"; o app pede novo scan.
            if mtype == "qr_token":
                token = str(data.get("token", ""))
                ok, pin = _validate_qr_token(token)
                if ok:
                    authenticated = True
                    STATE.clear_attempts(peer_ip)
                    await ws.send_json({"type": "pin_ok"})
                    log.info("Token QR válido de %s", peer_ip)
                else:
                    newly_blocked = STATE.register_failed_attempt(peer_ip)
                    await ws.send_json({
                        "type": "qr_token_error",
                        "reason": "token_invalid_or_expired",
                        "blocked": newly_blocked,
                    })
                    log.warning("Token QR inválido/expirado de %s", peer_ip)
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

                # Item 9: perfil de latência ("standard" ou "low_latency").
                # Em low_latency (cabo USB), o server ajusta fps, qualidade
                # JPEG e bitrate para priorizar velocidade.
                profile = data.get("profile", "standard")
                if profile not in ("standard", "low_latency"):
                    profile = "standard"
                max_bitrate = int(data.get("maxBitrate", 0) or 0)
                max_fps = int(data.get("maxFps", 0) or 0)

                pc = RTCPeerConnection()
                pcs.add(pc)

                # Dimensões do celular (se enviadas pelo app)
                phone_w = data.get("screenWidth", 0)
                phone_h = data.get("screenHeight", 0)

                # Cria a track de captura de tela
                screen_track = ScreenCaptureTrack(
                    quality=quality, mode=requested_mode,
                    phone_w=phone_w, phone_h=phone_h,
                    profile=profile,
                )
                pc.addTrack(relay.subscribe(screen_track))

                # Item 9: aplica bitrate máximo no RTPSender do vídeo, se
                # suportado pelo aiortc. Em low_latency, limita a 2.5 Mbps.
                try:
                    senders = pc.getSenders()
                    if senders:
                        sender = senders[0]
                        # aiortc não expõe setParameters como o WebRTC oficial,
                        # mas permite configurar maxBitrate via RTCRtpSendParameters.
                        params = sender.parameters
                        if hasattr(params, "encodings") and params.encodings:
                            for enc in params.encodings:
                                if max_bitrate > 0:
                                    enc.maxBitrate = max_bitrate
                                elif profile == "low_latency":
                                    enc.maxBitrate = 2_500_000
                except Exception as exc:
                    log.debug("Não foi possível aplicar maxBitrate no sender: %s", exc)

                # DataChannel para controle remoto
                @pc.on("datachannel")
                def on_datachannel(channel):
                    log.info("DataChannel aberto: %s", channel.label)

                    @channel.on("message")
                    def on_message(msg):
                        handle_control_message(msg, screen_track)

                # Item 9: trickle ICE — o app (em low_latency) envia candidatos
                # avulsos. aiortc aceita via pc.addIceCandidate().
                @pc.on("icecandidate")
                def on_ice_candidate(candidate):
                    # aiortc dispara este callback interno; não usamos aqui.
                    pass

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

            # Item 9: Trickle ICE — candidato avulso enviado pelo app (low_latency).
            elif mtype == "ice_candidate" and pc is not None:
                try:
                    from aiortc import RTCIceCandidate
                    cand_sdp = str(data.get("candidate", ""))
                    sdp_mid = data.get("sdpMid", "")
                    sdp_mline = int(data.get("sdpMLineIndex", 0) or 0)
                    if cand_sdp:
                        # aiortc não tem `from_sdp` estável entre versões.
                        # Em vez disso, construir manualmente a partir do SDP.
                        # Se o parser falhar, logamos e seguimos — em LAN o
                        # SDP já vem com candidatos embutidos (mesmo em trickle).
                        candidate = _parse_ice_candidate_sdp(cand_sdp, sdp_mid, sdp_mline)
                        if candidate is not None:
                            await pc.addIceCandidate(candidate)
                        else:
                            log.debug("ICE candidate ignorado (parser falhou): %s", cand_sdp[:80])
                except Exception as exc:
                    log.debug("Falha ao adicionar ICE candidate: %s", exc)

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


# Lista de processos de atalhos abertos pelo usuário — precisam ser
# mortos ao encerrar o server para não ficarem órfãos.
_shortcut_processes: list[subprocess.Popen] = []


def _kill_shortcut_processes():
    """Encerra todos os processos de atalhos abertos pelo usuário."""
    if not _shortcut_processes:
        return
    count = len(_shortcut_processes)
    for proc in _shortcut_processes:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    _shortcut_processes.clear()
    log.info("Processos de atalhos encerrados (%d).", count)


def _execute_shortcut_command(command: str):
    """Executa um comando no displaydigital (Xvfb ativo).

    O processo é rastreado e será encerrado automaticamente quando o
    servidor fechar, evitando processos órfãos.
    """
    vd = get_active_display()
    env = None
    if vd and vd.is_running() and vd.display_name:
        env = {**os.environ, "DISPLAY": vd.display_name}
        log.info("Executando atalho no %s: %s", vd.display_name, command)
    else:
        log.info("Executando atalho no display principal: %s", command)

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            env=env or os.environ,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _shortcut_processes.append(proc)
    except Exception as exc:
        log.error("Erro ao executar atalho: %s", exc)


# ==========================================================================
# USB — Ancoragem USB (não Depuração/adb reverse)
# ==========================================================================
# O PC não precisa fazer NADA de especial para a conexão via cabo funcionar
# mais. A versão anterior usava Depuração USB (`adb reverse`): um loop aqui
# no server rodando `adb devices`/`adb reverse` a cada poucos segundos,
# consumindo CPU o tempo todo, mesmo sem nenhum celular plugado.
#
# Trocamos para Ancoragem USB (USB tethering): o celular cria uma interface
# de rede IP de verdade sobre o cabo (o mesmo tipo de link do Wi-Fi), e o
# NuDuck Server já escuta em todas as interfaces (0.0.0.0) — então o cabo
# funciona automaticamente assim que o celular ativa a Ancoragem USB nas
# configurações dele, sem o PC precisar rodar nada, sem adb, sem loop.
# Quem faz o trabalho de achar o IP do PC no cabo é o app (celular), que
# varre o pequeno subnet criado pela ancoragem — ver UsbConnectionMonitor.kt.


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
# QR Code seguro (token temporário criptografado, validade 30s)
# ==========================================================================
#
# Problema que resolve:
#   O QR Code original embutia {host, port, name, pin} em JSON puro. Escaneando
#   o QR com qualquer app externo (Google Lens, leitor genérico) o IP, a porta
#   e o PIN ficam legíveis — falha de segurança: alguém com uma foto do QR
#   consegue se conectar até a sessão mudar de PIN.
#
# Solução:
#   - Gerar um token criptografado (AES-256-GCM) embutindo {pin, exp, nonce}.
#   - host e porta ficam FORA da camada cifrada (são públicas: a LAN inteira
#     já os conhece por mDNS e a porta é fixa 8765), mas o PIN só sai dentro
#     do ciphertext. Assim um leitor externo só vê "ND1.<base64>" — ilegível.
#   - Validade: 30 segundos. A UI Tk regenera o QR a cada 25s automaticamente.
#   - O app não decifra o token (não tem a chave); envia o token opaco para o
#     server via WebSocket ("qr_token") e o server valida (decripta + checa
#     expiração). Se válido, autentica a sessão sem precisar do PIN à parte.
#
# Formato do payload do QR (uma única string):
#   ND1.<base64url(host:port)>.<base64url(nonce_12B || ciphertext || tag_16B)>
# Onde:
#   - ND1 = prefixo do formato (NuDuck versão 1)
#   - host:port = IP e porta do servidor (texto, base64url)
#   - O ciphertext criptografa (AES-256-GCM) um JWT assinado (HS256), não
#     mais um JSON cru. Claims do JWT: {"pin", "exp", "iat", "jti"}.
#     Duas camadas de proteção, cada uma cobrindo o que a outra não cobre:
#       - AES-GCM (fora): confidencialidade — sem a chave do server,
#         ninguém lê o conteúdo, nem que o QR seja fotografado por outra
#         pessoa/app.
#       - JWT/HS256 (dentro): integridade + expiração no formato padrão —
#         `exp` é validado pela própria biblioteca PyJWT na decodificação
#         (rejeita token expirado antes mesmo de olhar os claims), e a
#         assinatura HMAC garante que o payload não foi adulterado mesmo
#         que alguém descobrisse uma forma de recriar um blob AES-GCM
#         válido (defesa em profundidade).

QR_TOKEN_VERSION = "ND1"
QR_TOKEN_TTL_SECONDS = 30  # Validade do token
QR_TOKEN_REFRESH_MS = 25000  # Refresh da UI Tk (margem de 5s antes de expirar)

# Caminho do arquivo da chave secreta persistente (gerada uma vez, mantida
# entre reinicializações). Se o usuário reinstalar/excluir este arquivo, os
# tokens antigos deixam de validar — comportamento esperado.
_QR_SECRET_FILE = os.path.join(_get_persistent_data_dir(), "qr_secret.key")
_qr_aes_key: Optional[bytes] = None


def _get_qr_secret_key() -> bytes:
    """Retorna a chave AES-256 do server (32 bytes). Cria se não existe."""
    global _qr_aes_key
    if _qr_aes_key is not None:
        return _qr_aes_key

    # Tenta carregar do arquivo.
    try:
        if os.path.exists(_QR_SECRET_FILE):
            with open(_QR_SECRET_FILE, "rb") as f:
                key = f.read()
            if len(key) == 32:
                _qr_aes_key = key
                return key
            log.warning("Arquivo qr_secret.key com tamanho incorreto; regenerando.")
    except Exception as exc:
        log.warning("Erro ao ler qr_secret.key (%s); regenerando.", exc)

    # Gera nova chave e persiste.
    key = secrets.token_bytes(32)
    try:
        # Permissões restritas (0600) em Unix — contém segredo.
        fd = os.open(_QR_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except Exception as exc:
        log.error("Erro ao salvar qr_secret.key: %s", exc)
    _qr_aes_key = key
    return key


def _b64url_encode(data: bytes) -> str:
    """Base64URL sem padding (URL-safe)."""
    import base64
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _get_jwt_secret_key() -> str:
    """Chave de assinatura HS256 do JWT — reaproveita a mesma chave AES
    persistente (32 bytes), só que em hex, pra não precisar gerenciar dois
    arquivos de segredo separados. Rotacionar qr_secret.key invalida os
    dois (AES e JWT) ao mesmo tempo, o que é o comportamento certo.
    """
    return _get_qr_secret_key().hex()


def _build_qr_token_payload() -> str:
    """Gera o payload criptografado do QR: ND1.<b64(host:port)>.<b64(ciphertext)>.

    O ciphertext criptografa (AES-256-GCM) um JWT assinado (HS256) — não
    mais um JSON cru. O JWT carrega os claims padrão `exp`/`iat` (a
    biblioteca PyJWT já valida `exp` sozinha na decodificação) mais `pin`
    e `jti` (nonce, para diferenciar tokens gerados no mesmo segundo).

    O PIN expira em QR_TOKEN_TTL_SECONDS.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        log.error("Biblioteca 'cryptography' não instalada: %s", exc)
        # Fallback para JSON legado (não recomendado — só pra não quebrar).
        return _json.dumps({
            "host": get_local_ip(),
            "port": PORT,
            "name": socket.gethostname().split(".")[0],
            "pin": STATE.pin,
        })

    host_port = f"{get_local_ip()}:{PORT}"
    host_port_b64 = _b64url_encode(host_port.encode("utf-8"))

    now = time.time()
    claims = {
        "v": 1,
        "pin": STATE.pin,
        "jti": secrets.token_hex(8),  # nonce interno, para diferenciar tokens
        "iat": int(now),
        "exp": int(now + QR_TOKEN_TTL_SECONDS),
    }

    try:
        import jwt as pyjwt
        payload = pyjwt.encode(claims, _get_jwt_secret_key(), algorithm="HS256")
    except ImportError:
        log.warning("PyJWT não instalado — token QR sem a camada JWT (só AES-GCM). "
                     "Instale: pip install PyJWT")
        payload = _json.dumps(claims, separators=(",", ":"))

    aesgcm = AESGCM(_get_qr_secret_key())
    nonce = secrets.token_bytes(12)  # nonce AES-GCM (12 bytes recomendado)
    ciphertext = aesgcm.encrypt(nonce, payload.encode("utf-8"), None)
    blob = nonce + ciphertext  # nonce (12B) || ciphertext || tag (16B embutidos)
    blob_b64 = _b64url_encode(blob)

    return f"{QR_TOKEN_VERSION}.{host_port_b64}.{blob_b64}"


def _validate_qr_token(token: str) -> tuple[bool, str]:
    """Valida um token ND1 recebido do app.

    Retorna (sucesso, pin). Em caso de falha, retorna (False, "").
    Sucesso significa: token bem formado, decriptado com a chave AES certa,
    JWT com assinatura válida, e dentro do prazo de validade (checado duas
    vezes: pelo PyJWT via `exp` do JWT, e pelo AES-GCM em si não expirar
    nada — a expiração real é sempre a do JWT).
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        log.error("Biblioteca 'cryptography' não instalada; token rejeitado.")
        return False, ""

    if not token.startswith(f"{QR_TOKEN_VERSION}."):
        return False, ""

    parts = token.split(".", 2)
    if len(parts) != 3:
        return False, ""

    _, _host_b64, blob_b64 = parts

    # Decifra a camada AES-GCM.
    try:
        blob = _b64url_decode(blob_b64)
        if len(blob) < 12 + 16:  # nonce(12) + tag(16)
            return False, ""
        nonce = blob[:12]
        ciphertext = blob[12:]
        aesgcm = AESGCM(_get_qr_secret_key())
        plaintext = aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception as exc:
        log.warning("Token QR inválido (decriptação AES-GCM falhou): %s", exc)
        return False, ""

    # Decodifica e valida a camada JWT (assinatura + exp).
    try:
        import jwt as pyjwt
        data = pyjwt.decode(plaintext, _get_jwt_secret_key(), algorithms=["HS256"])
    except ImportError:
        # PyJWT não instalado no server: aceita o payload como JSON puro
        # (mesmo fallback usado em _build_qr_token_payload quando faltava
        # PyJWT na geração) — mantém compatibilidade, mas sem a camada JWT.
        try:
            data = _json.loads(plaintext)
        except Exception:
            return False, ""
        now_s = time.time()
        if now_s > data.get("exp", 0):
            log.warning("Token QR expirado (sem PyJWT).")
            return False, ""
    except Exception as exc:
        # Cobre jwt.ExpiredSignatureError, jwt.InvalidSignatureError, etc.
        log.warning("Token QR com JWT inválido/expirado: %s", exc)
        return False, ""

    # Verifica versão.
    if data.get("v") != 1:
        log.warning("Token QR com versão não suportada: %s", data.get("v"))
        return False, ""

    # Verifica PIN (token só é válido para o PIN da sessão atual do server).
    # Se o server reiniciou e gerou novo PIN, tokens antigos são invalidados.
    pin = str(data.get("pin", ""))
    if pin != STATE.pin:
        log.warning("Token QR com PIN que não bate com a sessão atual.")
        return False, ""

    return True, pin


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
        """Gera o QR Code com token criptografado (ND1.<b64>.<b64>).

        Fora do app, o QR mostra apenas "ND1.<runa_base64>.<runa_base64>" —
        ilegível. Apenas o app, que reenvia o token opaco ao server via WS,
        consegue validar. O host:port também vai fora da camada cifrada
        (mas sem o PIN, que é o que realmente protege a conexão).
        """
        try:
            import qrcode
            from PIL import ImageTk

            payload = _build_qr_token_payload()
            box_size = max(3, round(4 * ui_scale))
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
        base_w, base_h = 380, 820
        win_w = min(sc(base_w), int(root.winfo_screenwidth() * 0.9))
        win_h = min(sc(base_h), int(root.winfo_screenheight() * 0.9))
        pos_x = max(0, (root.winfo_screenwidth() - win_w) // 2)
        pos_y = max(0, (root.winfo_screenheight() - win_h) // 3)
        root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        root.minsize(sc(300), sc(560))
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
            # Mata processos de atalhos abertos pelo usuário para
            # não ficarem órfãos.
            try:
                _kill_shortcut_processes()
            except Exception as exc:
                log.warning("Erro ao matar processos de atalhos no shutdown: %s", exc)
            # Para o display virtual ativo (Xvfb, x2x, WM, terminal, etc.)
            try:
                stop_active_display()
            except Exception as exc:
                log.warning("Erro ao parar display virtual no shutdown: %s", exc)
            try:
                root.destroy()
            except Exception:
                pass
            # Usa sys.exit em vez de os._exit para garantir que os
            # handlers atexit rodem (incluindo stop_active_display como
            # rede de segurança).
            sys.exit(0)

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

            term_frame = tk.Frame(win, bg="#0b0b0b")
            term_frame.pack(fill="both", expand=True)
            term_scroll = tk.Scrollbar(
                term_frame, orient="vertical",
                bg="#ffffff", troughcolor="#333333", activebackground="#cccccc",
                borderwidth=0, highlightthickness=0,
            )
            text = tk.Text(
                term_frame, bg="#0b0b0b", fg="#e6e6e6", insertbackground="#e6e6e6",
                font=("Consolas", 9), borderwidth=0, yscrollcommand=term_scroll.set,
            )
            term_scroll.configure(command=text.yview)
            term_scroll.pack(side="right", fill="y")
            text.pack(side="left", fill="both", expand=True)
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
            header, text=APP_NAME, font=("Sans", 16, "bold"),
            bg=BG_SURFACE, fg=ACCENT_BLUE,
        ).pack(pady=(10, 1))
        tk.Label(
            header, text="PIN de conexão", font=("Sans", 9),
            bg=BG_SURFACE, fg=FG_MUTED,
        ).pack()
        tk.Label(
            header, text=STATE.pin, font=("Consolas", 26, "bold"),
            bg=BG_SURFACE, fg=ACCENT_ORANGE,
        ).pack(pady=(0, 4))

        qr_photo = _build_qr_photo(root, ui_scale)
        if qr_photo is not None:
            qr_wrap = tk.Frame(header, bg="white", padx=4, pady=4)
            qr_wrap.pack(pady=(0, 4))
            qr_label = tk.Label(qr_wrap, image=qr_photo, bg="white")
            qr_label.image = qr_photo
            qr_label.pack()
            tk.Label(
                header, text="Escaneie no app para conectar (validade 30s)",
                font=("Sans", 8), bg=BG_SURFACE, fg=FG_MUTED,
            ).pack(pady=(0, 8))

            # Refresh automático do QR a cada QR_TOKEN_REFRESH_MS (25s).
            # O token criptografado tem validade de 30s; o refresh em 25s
            # garante que sempre haja um QR válido na tela, com margem.
            def refresh_qr():
                new_photo = _build_qr_photo(root, ui_scale)
                if new_photo is not None:
                    qr_label.configure(image=new_photo)
                    qr_label.image = new_photo  # mantém referência viva
                root.after(QR_TOKEN_REFRESH_MS, refresh_qr)

            root.after(QR_TOKEN_REFRESH_MS, refresh_qr)
        else:
            tk.Frame(header, bg=BG_SURFACE, height=6).pack()

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

        def _make_scrollable_tab(bg):
            """Cria uma aba com rolagem vertical, pra garantir que os campos
            e botões nunca fiquem escondidos fora da área visível da janela
            — não importa o tamanho da tela nem quantos itens tenham dentro.
            """
            outer = tk.Frame(notebook, bg=bg)
            canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, borderwidth=0)
            vsb = tk.Scrollbar(
                outer, orient="vertical", command=canvas.yview,
                bg="#ffffff", troughcolor="#333333",
                activebackground="#cccccc", borderwidth=0,
                highlightthickness=0, width=12,
            )
            inner = tk.Frame(canvas, bg=bg, padx=14, pady=14)
            inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
            canvas.configure(yscrollcommand=vsb.set)
            canvas.pack(side="left", fill="both", expand=True)
            # Sempre visível (não só quando "precisa") — versão anterior
            # escondia a barra baseada na altura do canvas medida cedo
            # demais, o que podia deixá-la escondida por engano mesmo com
            # conteúdo cortado, escondendo caixas de texto e botões.
            vsb.pack(side="right", fill="y")

            def _on_inner_configure(_event=None):
                canvas.configure(scrollregion=canvas.bbox("all"))

            inner.bind("<Configure>", _on_inner_configure)
            canvas.bind("<Configure>", lambda e: (canvas.itemconfig(inner_id, width=e.width), _on_inner_configure()))

            def _wheel(event):
                delta = event.delta
                if delta:
                    canvas.yview_scroll(int(-1 * (delta / 120)), "units")

            def _bind_wheel(_e=None):
                canvas.bind_all("<MouseWheel>", _wheel)
                canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-2, "units"))
                canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(2, "units"))

            def _unbind_wheel(_e=None):
                canvas.unbind_all("<MouseWheel>")
                canvas.unbind_all("<Button-4>")
                canvas.unbind_all("<Button-5>")

            outer.bind("<Enter>", _bind_wheel)
            outer.bind("<Leave>", _unbind_wheel)
            return outer, inner

        tab_window_outer, tab_window = _make_scrollable_tab(BG_CARD)
        tab_shortcuts_outer, tab_shortcuts = _make_scrollable_tab(BG_CARD)
        tab_system_outer, tab_system = _make_scrollable_tab(BG_CARD)
        notebook.add(tab_window_outer, text="  Janela  ")
        notebook.add(tab_shortcuts_outer, text="  Atalhos  ")
        notebook.add(tab_system_outer, text="  Sistema  ")

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
        shortcut_scrollbar = tk.Scrollbar(
            shortcut_listbox_frame, orient="vertical", command=shortcut_listbox.yview,
            bg="#ffffff", troughcolor="#333333", activebackground="#cccccc",
            borderwidth=0, highlightthickness=0, width=10,
        )
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
            tab_system,
            text="Cabo USB: ative a Ancoragem USB nas configurações do celular (não precisa fazer nada aqui no PC).",
            font=("Sans", 9), bg=BG_CARD, fg=FG_MUTED, anchor="w", justify="left", wraplength=280,
        )
        usb_label.pack(fill="x", pady=(0, 12))

        _section_label(tab_system, "Diagnóstico")
        _flat_button(tab_system, "Ver terminal", open_log_viewer).pack(fill="x", pady=(0, 16))

        _section_label(tab_system, "Aparência do Display 2")
        appearance_status_label = _hint_label(
            tab_system, "Clona tema/ícones/papel de parede do display principal para o Display 2 (Estender)."
        )

        def _clone_appearance_now():
            """Roda a clonagem de aparência sob demanda (item pedido: botão
            manual, já que a versão 100% automática no início do Estender
            nem sempre pega dependendo do ambiente gráfico do PC).

            Faz 3 coisas, nessa ordem:
              1. Reaplica a lógica já existente (dconf/gsettings por chave +
                 papel de parede via feh) — é a mesma que roda sozinha ao
                 iniciar o Estender, aqui repetida sob demanda.
              2. Roda literalmente os comandos do arquivo enviado: dump
                 completo do schema org.gnome.desktop.interface no display
                 principal (:0) e load desse dump no Display 2.
                 Aviso técnico: dconf é uma configuração por usuário, não
                 por DISPLAY — então esse dump/load tende a não mudar nada
                 sozinho (a config já é a mesma pros dois displays). Incluído
                 mesmo assim porque foi pedido explicitamente e não faz mal.
              3. Se houver xfce4-panel ou gnome-shell instalado, inicia um
                 painel leve no Display 2 (só se ainda não tiver um rodando).
                 Atenção: gnome-shell é pesado e pode falhar/travar num
                 Xvfb sem aceleração de GPU — se isso acontecer, o Display 2
                 continua funcionando normalmente, só o painel não aparece.
            """
            appearance_status_label.config(text="Executando…", fg=FG_MUTED)

            def _work():
                vd = get_active_display()
                if not vd or not vd.is_running() or not vd.display_name:
                    root.after(0, lambda: appearance_status_label.config(
                        text="Ative o modo Estender primeiro (é preciso ter um Display 2 rodando).",
                        fg=COLOR_WARN,
                    ))
                    return

                display_n = vd.display_name
                results = []
                env_n = vd._build_session_env(display_n)

                # 1) Reaplica a lógica já existente (mais robusta: dconf +
                #    gsettings + papel de parede via feh + fallback de cor).
                try:
                    vd._clone_display0_appearance_with_retry()
                    results.append("tema/papel de parede reaplicados")
                except Exception as exc:
                    results.append(f"falha ao reaplicar tema ({exc})")

                # 2) Comandos do arquivo enviado: dump/load do dconf.
                try:
                    env0 = vd._build_session_env(":0")
                    dump = subprocess.run(
                        ["dconf", "dump", "/org/gnome/desktop/interface/"],
                        env=env0, capture_output=True, timeout=5,
                    )
                    if dump.returncode == 0 and dump.stdout:
                        load = subprocess.run(
                            ["dconf", "load", "/org/gnome/desktop/interface/"],
                            input=dump.stdout, env=env_n, capture_output=True, timeout=5,
                        )
                        if load.returncode == 0:
                            results.append("dconf dump/load ok")
                        else:
                            err = load.stderr.decode(errors="ignore").strip()
                            results.append(f"dconf load falhou ({err or 'sem detalhe'})")
                    else:
                        err = dump.stderr.decode(errors="ignore").strip()
                        results.append(f"dconf dump vazio/falhou ({err or 'sem detalhe'})")
                except FileNotFoundError:
                    results.append("dconf não instalado (sudo apt install dconf-cli)")
                except Exception as exc:
                    results.append(f"erro no dconf dump/load ({exc})")

                # 3) Painel leve no Display 2, se disponível e ainda não
                #    estiver rodando (evita empilhar processo a cada clique).
                try:
                    already_running = any(
                        "xfce4-panel" in " ".join(p.get("cmd", [])) or "gnome-shell" in " ".join(p.get("cmd", []))
                        for p in getattr(vd, "_extra_panel_procs", [])
                    )
                except Exception:
                    already_running = False

                if not already_running:
                    try:
                        if shutil.which("xfce4-panel"):
                            proc = subprocess.Popen(
                                ["xfce4-panel"], env=env_n,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            )
                            vd._extra_panel_procs = getattr(vd, "_extra_panel_procs", []) + [
                                {"proc": proc, "cmd": ["xfce4-panel"]}
                            ]
                            results.append("xfce4-panel iniciado no Display 2")
                        elif shutil.which("gnome-shell"):
                            proc = subprocess.Popen(
                                ["gnome-shell", "--replace"], env=env_n,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            )
                            vd._extra_panel_procs = getattr(vd, "_extra_panel_procs", []) + [
                                {"proc": proc, "cmd": ["gnome-shell", "--replace"]}
                            ]
                            results.append("gnome-shell --replace iniciado no Display 2 (pode falhar sem GPU)")
                    except Exception as exc:
                        results.append(f"painel/shell não iniciado ({exc})")

                msg = "; ".join(results)
                root.after(0, lambda: appearance_status_label.config(text=msg, fg=COLOR_OK))

            threading.Thread(target=_work, daemon=True).start()

        _flat_button(tab_system, "Clonar aparência para o Display 2", _clone_appearance_now, accent=True).pack(
            fill="x", pady=(6, 4)
        )
        _hint_label(
            tab_system,
            "Também roda os comandos do arquivo: dump/load do dconf e painel leve "
            "(xfce4-panel ou gnome-shell) no Display 2, quando disponíveis.",
        )

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
    _kill_shortcut_processes()
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

    # Rede de segurança extra pro bug do x2x/Xvfb ficando órfão: cobre os
    # casos que não passam pelo botão "fechar janela" do Tkinter (matar o
    # processo com Ctrl+C no terminal, `kill`, systemd parando o serviço,
    # etc.). atexit cobre saída normal (`sys.exit`/fim do script); os
    # handlers de sinal cobrem SIGTERM/SIGINT — sem eles, `os._exit(0)` ou
    # um sinal não tratado matam o processo sem rodar nenhuma limpeza.
    atexit.register(_kill_shortcut_processes)
    atexit.register(stop_active_display)

    def _handle_signal(signum, _frame):
        log.info("Sinal %s recebido — encerrando %s...", signum, APP_NAME)
        try:
            _kill_shortcut_processes()
        except Exception:
            pass
        try:
            stop_active_display()
        except Exception:
            pass
        sys.exit(0)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _handle_signal)
        except Exception:
            pass  # alguns sinais não são interceptáveis em certas plataformas

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
    print("  USB:   ative a Ancoragem USB no celular.")
    print("=" * 50)

    try:
        start_ui(hostname)
    except Exception as exc:
        log.warning("Interface gráfica indisponível (%s); use o console.", exc)

    zeroconf = start_mdns(hostname)
    app = build_app()

    try:
        web.run_app(app, host="0.0.0.0", port=PORT, print=None)
    finally:
        zeroconf.close()


if __name__ == "__main__":
    main()
