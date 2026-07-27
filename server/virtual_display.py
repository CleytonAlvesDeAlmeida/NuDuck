"""
virtual_display.py - Display virtual Xvfb para o modo Estender do NuDuck.

Funciona igual ao SpaceDesk: cria um servidor X separado (Xvfb) que o
celular renderiza e controla, funcionando como uma segunda tela virtual.

Como é software puro (não usa GPU nem xrandr), funciona em QUALQUER hardware.

Requisitos:
  sudo apt install xvfb xdotool openbox xsetroot x11-utils    (Debian/Ubuntu)
"""

import io
import logging
import multiprocessing as mp
import numpy as np
import os
import shutil
import subprocess
import time

log = logging.getLogger("NuDuck")


class XvfbVirtualDisplay:
    """Gerencia um display virtual Xvfb como segunda tela."""

    def __init__(self, width=1280, height=800, depth=24):
        self.width = width
        self.height = height
        self.depth = depth
        self.display_num = None
        self.display_name = None
        self.xvfb_process = None
        self.wm_process = None
        self._capture_worker = None
        self._shared_array = None
        self._new_frame_event = None
        self._stop_event = None
        self._shape = (height, width, 3)
        self._started = False
        self._wm_name = None

    # ------------------------------------------------------------------
    # Iniciar / Parar
    # ------------------------------------------------------------------

    def start(self):
        """Inicia o Xvfb + window manager + captura.

        Retorna:
          Sucesso: (True, nome_do_display, info_dict)
          Falha:   (False, mensagem_de_erro, None)
        """
        if not shutil.which("Xvfb"):
            return False, (
                "Xvfb não encontrado. Instale com:\n"
                "  sudo apt install xvfb xdotool openbox xsetroot"
            ), None

        # Procura um display livre
        for d in range(1, 100):
            if not os.path.exists(f"/tmp/.X11-unix/X{d}"):
                self.display_num = d
                break
        if self.display_num is None:
            return False, "Nenhum display :1..:99 livre", None

        self.display_name = f":{self.display_num}"

        # Inicia o Xvfb com todas as extensões necessárias
        try:
            self.xvfb_process = subprocess.Popen(
                [
                    "Xvfb", self.display_name,
                    "-screen", "0", f"{self.width}x{self.height}x{self.depth}",
                    "-ac", "-nolisten", "tcp",
                    # Extensões necessárias pra captura funcionar
                    "+extension", "RANDR",
                    "+extension", "RENDER",
                    "+extension", "Composite",
                    "+extension", "XFIXES",
                    "+extension", "MIT-SHM",
                    "+extension", "BIG-REQUESTS",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.5)

            if self.xvfb_process.poll() is not None:
                stderr = self.xvfb_process.stderr.read().decode(errors="replace").strip()
                return False, f"Xvfb falhou (codigo {self.xvfb_process.returncode}): {stderr}", None

            log.info("Xvfb iniciado em %s (%dx%d)", self.display_name, self.width, self.height)
        except Exception as exc:
            return False, f"Erro ao iniciar Xvfb: {exc}", None

        # Cor de fundo do desktop (azul escuro, dá pra ver que não é preto)
        env = {**os.environ, "DISPLAY": self.display_name}
        try:
            subprocess.run(
                ["xsetroot", "-solid", "#1a1a2e"],
                env=env, capture_output=True, timeout=3,
            )
        except Exception:
            pass

        # Inicia um window manager leve
        for wm in ("openbox", "fluxbox", "twm", "fvwm"):
            if shutil.which(wm):
                try:
                    self.wm_process = subprocess.Popen(
                        [wm], env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self._wm_name = wm
                    log.info("Window manager '%s' iniciado em %s", wm, self.display_name)
                    break
                except Exception:
                    continue

        if not self._wm_name:
            log.warning("Nenhum WM encontrado. Instale openbox para desktop completo.")

        # Abre um terminal automaticamente pra ter onde interagir
        self._open_initial_terminal(env)

        # Inicia captura do framebuffer
        self._start_capture_worker()

        self._started = True
        info = {
            "display": self.display_name,
            "resolution": f"{self.width}x{self.height}",
            "wm": self._wm_name or "(nenhum)",
            "note": (
                f"Display virtual ativo em {self.display_name}. "
                f"Para abrir apps nele: DISPLAY={self.display_name} nome_do_app"
            ),
        }
        return True, self.display_name, info

    def _open_initial_terminal(self, env):
        """Abre um terminal no display virtual pra ter onde interagir."""
        terminals = ("xterm", "uxterm", "lxterminal", "gnome-terminal", "konsole")
        for term in terminals:
            if shutil.which(term):
                try:
                    args = [term]
                    # gnome-terminal precisa de -- para args
                    if term == "gnome-terminal":
                        args = [term, "--"]
                    subprocess.Popen(
                        args + ["/bin/bash"],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    log.info("Terminal '%s' aberto em %s", term, self.display_name)
                    return
                except Exception:
                    continue
        log.warning("Nenhum terminal encontrado para abrir no display virtual.")

    def stop(self):
        """Para o Xvfb, WM e captura."""
        if self._stop_event:
            self._stop_event.set()
        if self._capture_worker:
            self._capture_worker.join(timeout=2)
            if self._capture_worker.is_alive():
                self._capture_worker.kill()
        if self.wm_process:
            try:
                self.wm_process.terminate()
                self.wm_process.wait(timeout=2)
            except Exception:
                pass
        if self.xvfb_process:
            try:
                self.xvfb_process.terminate()
                self.xvfb_process.wait(timeout=2)
            except Exception:
                pass
        self._started = False
        log.info("Display virtual %s encerrado.", self.display_name)

    def is_running(self):
        """True se o Xvfb ainda está ativo."""
        if not self._started:
            return False
        if self.xvfb_process and self.xvfb_process.poll() is not None:
            return False
        return True

    # ------------------------------------------------------------------
    # Captura de frames
    # ------------------------------------------------------------------

    def _start_capture_worker(self):
        """Inicia processo filho que captura o framebuffer do Xvfb."""
        self._stop_event = mp.Event()
        self._new_frame_event = mp.Event()
        self._shared_array = mp.Array("B", self.height * self.width * 3)

        self._capture_worker = mp.Process(
            target=_capture_worker,
            args=(
                self.display_name,
                self._shared_array,
                self._shape,
                self._new_frame_event,
                self._stop_event,
                self.width,
                self.height,
            ),
            daemon=True,
        )
        self._capture_worker.start()

    def get_frame(self):
        """Retorna o último frame BGR como numpy (H, W, 3), ou None."""
        if not self._started or not self._new_frame_event:
            return None
        if self._new_frame_event.wait(timeout=0.5):
            self._new_frame_event.clear()
            return (
                np.frombuffer(self._shared_array.get_obj(), dtype=np.uint8)
                .reshape(self._shape)
                .copy()
            )
        return None

    # ------------------------------------------------------------------
    # Input (toque do celular -> Xvfb via xdotool)
    # ------------------------------------------------------------------

    def send_input(self, action, x=0, y=0, key=None):
        """Envia clique/movimento/tecla pro display virtual."""
        if not self._started or not self.display_name:
            return
        if not shutil.which("xdotool"):
            return

        env = {**os.environ, "DISPLAY": self.display_name}
        try:
            if action == "mousemove":
                subprocess.run(
                    ["xdotool", "mousemove", "--", str(int(x)), str(int(y))],
                    env=env, capture_output=True, timeout=1,
                )
            elif action == "click":
                subprocess.run(
                    ["xdotool", "mousemove", "--", str(int(x)), str(int(y)), "click", "1"],
                    env=env, capture_output=True, timeout=1,
                )
            elif action == "mousedown":
                subprocess.run(
                    ["xdotool", "mousedown", "1"],
                    env=env, capture_output=True, timeout=1,
                )
            elif action == "mouseup":
                subprocess.run(
                    ["xdotool", "mouseup", "1"],
                    env=env, capture_output=True, timeout=1,
                )
            elif action == "key" and key:
                subprocess.run(
                    ["xdotool", "key", "--", str(key)],
                    env=env, capture_output=True, timeout=1,
                )
        except Exception:
            pass


# ----------------------------------------------------------------------
# Processo de captura
# ----------------------------------------------------------------------

def _capture_worker(display_name, shared_array, shape, new_frame_event, stop_event, width, height):
    """Processo filho que captura o framebuffer do Xvfb continuamente.

    Tenta mss primeiro. Se falhar, usa xwd (X Window Dump) como fallback.
    Se os dois falharem, gera um frame colorido pra não ficar preto.
    """
    os.environ["DISPLAY"] = display_name
    os.environ["HOME"] = os.path.expanduser("~")

    capture_fn = None

    # --- Método 1: mss (rápido, via X11) ---
    try:
        import mss

        sct = mss.mss()
        monitors = sct.monitors
        if len(monitors) >= 2:
            mon = monitors[1]
            log.info("Capture worker (mss) ativo em %s: %dx%d",
                     display_name, mon["width"], mon["height"])

            def capture_mss():
                raw = sct.grab(mon)
                img = np.array(raw)[:, :, :3]  # BGRA -> BGR
                buf = np.frombuffer(shared_array.get_obj(), dtype=np.uint8).reshape(shape)
                np.copyto(buf, img)
                new_frame_event.set()

            capture_fn = capture_mss
        else:
            log.warning("Capture worker: mss não encontrou monitores em %s, tentando xwd", display_name)
    except Exception as exc:
        log.warning("Capture worker: mss falhou em %s (%s), tentando xwd", display_name, exc)

    # --- Método 2: xwd (X Window Dump) — 100% compatível com Xvfb ---
    if capture_fn is None and shutil.which("xwd"):
        log.info("Capture worker (xwd) ativo em %s", display_name)

        def capture_xwd():
            try:
                proc = subprocess.Popen(
                    ["xwd", "-root", "-display", display_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ, "DISPLAY": display_name},
                )
                xwd_data = proc.stdout.read()

                if len(xwd_data) < 100:
                    return  # dados muito pequenos, provavelmente erro

                # Converte XWD para numpy BGR
                img_rgb = _xwd_to_rgb(xwd_data, width, height)
                if img_rgb is not None:
                    # RGB -> BGR
                    img_bgr = img_rgb[:, :, ::-1].copy()
                    buf = np.frombuffer(shared_array.get_obj(), dtype=np.uint8).reshape(shape)
                    np.copyto(buf, img_bgr)
                    new_frame_event.set()
            except Exception:
                pass

        capture_fn = capture_xwd

    # --- Método 3: frame colorido de fallback (nunca fica preto) ---
    if capture_fn is None:
        log.warning("Capture worker: mss e xwd falharam, usando frame colorido de fallback")

        # Gera um frame azul escuro com texto indicando o display
        desktop_bg = np.zeros((height, width, 3), dtype=np.uint8)
        desktop_bg[:, :] = [26, 26, 46]  # #1a1a2e em BGR

        def capture_fallback():
            buf = np.frombuffer(shared_array.get_obj(), dtype=np.uint8).reshape(shape)
            np.copyto(buf, desktop_bg)
            new_frame_event.set()

        capture_fn = capture_fallback

    # Loop principal de captura
    while not stop_event.is_set():
        try:
            capture_fn()
            stop_event.wait(0.033)  # ~30fps max
        except Exception:
            stop_event.set()
            break


def _xwd_to_rgb(data, expected_w, expected_h):
    """Converte dados XWD (X Window Dump) para numpy array RGB.

    Formato XWD: header binário seguido de pixels no formato do servidor X
    (geralmente BGRX ou BGRA para depth 24/32).
    """
    try:
        if len(data) < 100:
            return None

        # O header XWD começa com campos de 32 bits (big-endian)
        import struct

        # Offset do pixel data (campo 14 do header, em bytes 52-55)
        header_size = struct.unpack(">I", data[52:56])[0]

        # Bits por pixel (campo 13, offset 48-51)
        bpp = struct.unpack(">I", data[48:52])[0]

        if bpp not in (24, 32):
            return None

        # Extrai dados de pixel (pula o header)
        pixel_data = data[header_size:]

        # Pixels XWD vêm de baixo pra cima (origem em baixo)
        # Cada pixel tem bpp/8 bytes
        bytes_per_pixel = bpp // 8
        row_size = expected_w * bytes_per_pixel

        if len(pixel_data) < row_size * expected_h:
            return None

        img = np.frombuffer(pixel_data[:row_size * expected_h], dtype=np.uint8)
        img = img.reshape((expected_h, expected_w, bytes_per_pixel))

        # Inverte verticalmente (XWD: bottom-up -> top-down)
        img = np.flipud(img)

        # Pega só os 3 primeiros canais (BGR ou BGRX -> BGR) e converte pra RGB
        bgr = img[:, :, :3]
        rgb = bgr[:, :, ::-1].copy()
        return rgb
    except Exception:
        return None


def is_xvfb_available():
    """Verifica: Xvfb instalado e tem display livre?"""
    if not shutil.which("Xvfb"):
        return False
    for d in range(1, 100):
        if not os.path.exists(f"/tmp/.X11-unix/X{d}"):
            return True
    return False
