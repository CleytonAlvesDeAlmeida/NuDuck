"""
virtual_display.py - Display virtual Xvfb para o modo Estender do NuDuck.

Funciona igual ao SpaceDesk: cria um servidor X separado (Xvfb) que o
celular renderiza e controla, funcionando como uma segunda tela virtual.

Requisitos:
  sudo apt install xvfb xdotool openbox xsetroot x11-utils xterm
"""

import logging
import numpy as np
import os
import shutil
import signal
import struct
import subprocess
import threading
import time

log = logging.getLogger("NuDuck")

# Referência global ao Xvfb atual — garante que só exista um por vez
_active_display = None


def get_active_display():
    """Retorna o Xvfb ativo, ou None."""
    return _active_display


def set_active_display(vd):
    """Define o Xvfb ativo (usado pelo server.py)."""
    global _active_display
    _active_display = vd


def stop_active_display():
    """Para o Xvfb ativo (se existir) e limpa tudo."""
    global _active_display
    if _active_display is not None:
        _active_display.stop()
        _active_display = None


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
        # Captura via thread (evita leak de memória compartilhada do multiprocessing)
        self._capture_thread = None
        self._stop_event = None
        self._new_frame_event = None
        self._frame_lock = None
        self._latest_frame = None
        self._shape = (height, width, 3)
        self._started = False
        self._wm_name = None
        self._capture_ok = False

    def start(self):
        """Inicia o Xvfb + window manager + captura.

        Retorna:
          Sucesso: (True, nome_do_display, info_dict)
          Falha:   (False, mensagem_de_erro, None)
        """
        if not shutil.which("Xvfb"):
            return False, "Xvfb não encontrado. Instale: sudo apt install xvfb xdotool openbox xterm", None

        # Mata qualquer Xvfb órfão antes de tentar
        self._cleanup_orphan_xvfb()

        # Procura um display livre
        for d in range(1, 100):
            lock_file = f"/tmp/.X{d}-lock"
            socket_file = f"/tmp/.X11-unix/X{d}"
            self._remove_stale_files(lock_file, socket_file)
            if not os.path.exists(socket_file):
                self.display_num = d
                break

        if self.display_num is None:
            return False, "Nenhum display :1..:99 livre", None

        self.display_name = f":{self.display_num}"

        # Inicia o Xvfb
        try:
            self.xvfb_process = subprocess.Popen(
                [
                    "Xvfb", self.display_name,
                    "-screen", "0", f"{self.width}x{self.height}x{self.depth}",
                    "-ac", "-nolisten", "tcp",
                    "+extension", "RANDR",
                    "+extension", "RENDER",
                    "+extension", "XFIXES",
                    "+extension", "MIT-SHM",
                    "+extension", "BIG-REQUESTS",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.6)

            if self.xvfb_process.poll() is not None:
                stderr = self.xvfb_process.stderr.read().decode(errors="replace").strip()
                return False, f"Xvfb falhou (codigo {self.xvfb_process.returncode}): {stderr}", None

            log.info("Xvfb iniciado em %s (%dx%d)", self.display_name, self.width, self.height)
        except Exception as exc:
            return False, f"Erro ao iniciar Xvfb: {exc}", None

        # Cor de fundo do desktop (azul escuro — dá pra ver que não é preto)
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
                    log.info("WM '%s' iniciado em %s", wm, self.display_name)
                    break
                except Exception:
                    continue

        if not self._wm_name:
            log.warning("Nenhum WM encontrado. Instale openbox.")

        # Abre um terminal automaticamente no display virtual
        self._open_initial_terminal(env)

        # Inicia captura (thread, não processo — sem leak de memória compartilhada)
        self._start_capture_thread()

        # Espera um pouco e verifica se a captura está funcionando
        time.sleep(1.5)
        if self._new_frame_event and self._new_frame_event.is_set():
            self._capture_ok = True
            log.info("Captura do Xvfb funcionando.")
        else:
            self._capture_ok = False
            log.warning("Captura do Xvfb pode não estar funcionando. Usando frame de fallback.")

        self._started = True

        info = {
            "display": self.display_name,
            "resolution": f"{self.width}x{self.height}",
            "wm": self._wm_name or "(nenhum)",
            "capture": "ok" if self._capture_ok else "fallback",
            "note": (
                f"Display virtual ativo em {self.display_name}. "
                f"Para abrir apps: DISPLAY={self.display_name} nome_do_app"
            ),
        }
        return True, self.display_name, info

    def _remove_stale_files(self, lock_file, socket_file):
        """Remove arquivos de lock/socket de Xvfb que morreu."""
        try:
            if os.path.exists(lock_file):
                try:
                    with open(lock_file) as f:
                        pid_str = f.read().strip().split()[0]
                        pid = int(pid_str)
                        os.kill(pid, 0)
                except (ProcessLookupError, ValueError, PermissionError):
                    os.remove(lock_file)
                    if os.path.exists(socket_file):
                        os.remove(socket_file)
                    log.debug("Removeu resquício de Xvfb morto (display %s)", self.display_name)
        except Exception:
            pass

    def _cleanup_orphan_xvfb(self):
        """Mata processos Xvfb órfãos que podem estar bloqueando displays."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "Xvfb"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = result.stdout.strip().split("\n")
                for pid in pids:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                        log.info("Matou Xvfb órfão (PID %s)", pid)
                    except (ProcessLookupError, PermissionError):
                        pass
                time.sleep(0.3)
        except Exception:
            pass

    def _open_initial_terminal(self, env):
        """Abre um terminal no display virtual.

        Prefer xterm — funciona com Xvfb e não usa D-Bus.
        gnome-terminal ignora DISPLAY e abre no monitor físico.
        """
        terminals = ("xterm", "uxterm", "lxterminal", "konsole", "gnome-terminal")
        for term in terminals:
            if shutil.which(term):
                try:
                    args = [term]
                    if term == "gnome-terminal":
                        # gnome-terminal usa D-Bus — forçar display via --display
                        args = ["gnome-terminal", "--display=" + self.display_name, "--"]
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
        log.warning("Nenhum terminal encontrado.")

    def _start_capture_thread(self):
        """Inicia thread que captura o framebuffer do Xvfb."""
        self._stop_event = threading.Event()
        self._new_frame_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame = np.zeros(self._shape, dtype=np.uint8)

        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
        )
        self._capture_thread.start()

    def _capture_loop(self):
        """Loop de captura — roda em thread, usa subprocess (sem conflito X11).

        Tenta xwd primeiro. Se falhar, gera frame colorido (nunca preto).
        """
        display_name = self.display_name
        capture_fn = None

        # --- Método 1: xwd (X Window Dump) — subprocesso, sem conflito X11 ---
        if shutil.which("xwd"):
            log.info("Captura via xwd ativa em %s", display_name)

            def capture_xwd():
                try:
                    proc = subprocess.Popen(
                        ["xwd", "-root", "-display", display_name],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        env={**os.environ, "DISPLAY": display_name},
                    )
                    xwd_data = proc.stdout.read()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()

                    if len(xwd_data) > 100:
                        img_bgr = _xwd_to_bgr(xwd_data, self.width, self.height)
                        if img_bgr is not None:
                            with self._frame_lock:
                                np.copyto(self._latest_frame, img_bgr)
                            self._new_frame_event.set()
                            return
                except Exception:
                    pass
                # Se xwd falhou neste frame, não faz nada (mantém frame anterior)

            capture_fn = capture_xwd
        else:
            log.warning("xwd não encontrado. Instale: sudo apt install x11-utils")

        # --- Método 2: frame colorido de fallback (nunca preto) ---
        if capture_fn is None:
            log.warning("Nenhum método de captura disponível, usando frame colorido")
            bg = np.full((self.height, self.width, 3), [46, 26, 26], dtype=np.uint8)  # azul escuro BGR

            def capture_fallback():
                with self._frame_lock:
                    np.copyto(self._latest_frame, bg)
                self._new_frame_event.set()

            capture_fn = capture_fallback

        # Loop principal — captura a ~30fps
        while not self._stop_event.is_set():
            try:
                capture_fn()
            except Exception:
                pass
            self._stop_event.wait(0.033)

    def stop(self):
        """Para o Xvfb, WM, captura e limpa os arquivos de socket."""
        log.info("Encerrando display virtual %s...", self.display_name)

        # Sinaliza parada da thread de captura
        if self._stop_event:
            self._stop_event.set()

        # Espera thread terminar
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=3)
        self._capture_thread = None

        # Mata WM
        if self.wm_process:
            try:
                self.wm_process.terminate()
                self.wm_process.wait(timeout=2)
            except Exception:
                pass
            self.wm_process = None

        # Mata Xvfb
        if self.xvfb_process:
            try:
                self.xvfb_process.terminate()
                self.xvfb_process.wait(timeout=2)
            except Exception:
                try:
                    self.xvfb_process.kill()
                except Exception:
                    pass
            self.xvfb_process = None

        # Limpa arquivos de socket e lock do Xorg/Xvfb
        if self.display_num is not None:
            for f in (
                f"/tmp/.X{self.display_num}-lock",
                f"/tmp/.X11-unix/X{self.display_num}",
            ):
                try:
                    if os.path.exists(f):
                        os.remove(f)
                        log.debug("Removeu %s", f)
                except Exception:
                    pass

        # Limpa referências (threading não tem leak como multiprocessing)
        self._latest_frame = None
        self._stop_event = None
        self._new_frame_event = None
        self._frame_lock = None

        self._started = False
        self._capture_ok = False
        log.info("Display virtual %s encerrado e limpo.", self.display_name)

    def is_running(self):
        """True se o Xvfb ainda está ativo."""
        if not self._started:
            return False
        if self.xvfb_process and self.xvfb_process.poll() is not None:
            return False
        return True

    def get_frame(self):
        """Retorna o último frame BGR (H, W, 3), ou gera um frame colorido."""
        if not self._started or self._latest_frame is None:
            # Sem display — gera frame azul escuro
            return np.full(self._shape, [46, 26, 26], dtype=np.uint8)

        # Tenta pegar frame da thread de captura
        if self._new_frame_event.wait(timeout=0.1):
            self._new_frame_event.clear()
            try:
                with self._frame_lock:
                    return self._latest_frame.copy()
            except Exception:
                pass

        # Thread não produziu frame — gera frame colorido de fallback
        return np.full(self._shape, [46, 26, 26], dtype=np.uint8)

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
# Conversão XWD -> numpy BGR
# ----------------------------------------------------------------------

def _xwd_to_bgr(data, expected_w, expected_h):
    """Converte dados XWD para numpy array BGR.

    Layout do header XWD (todos os campos são CARD32 big-endian):
      Offset  0: header_size
      Offset 12: pixmap_width
      Offset 16: pixmap_height
      Offset 40: depth (bits por pixel significativos: 24 ou 32)
      Offset 44: bytes_per_line (bytes por linha, pode ter padding)
      Offset 48: visual_class
      Offset 52: red_mask
      Offset 56: green_mask
      Offset 60: blue_mask
    Os dados do pixel começam em data[header_size:].
    """
    try:
        if len(data) < 100:
            return None

        # Lê campos do header XWD (big-endian)
        header_size = struct.unpack(">I", data[0:4])[0]
        width = struct.unpack(">I", data[12:16])[0]
        height = struct.unpack(">I", data[16:20])[0]
        depth = struct.unpack(">I", data[40:44])[0]
        bytes_per_line = struct.unpack(">I", data[44:48])[0]
        red_mask = struct.unpack(">I", data[52:56])[0]
        blue_mask = struct.unpack(">I", data[60:64])[0]

        if depth not in (24, 32):
            log.debug("XWD: profundidade %d não suportada (precisa 24 ou 32)", depth)
            return None

        pixel_data = data[header_size:]

        # Verifica se tem dados suficientes
        needed = bytes_per_line * height
        if len(pixel_data) < needed:
            log.debug("XWD: dados insuficientes (tem %d, precisa %d)", len(pixel_data), needed)
            return None

        # Calcula bytes por pixel a partir de bytes_per_line e width
        actual_bpp = bytes_per_line // width if width > 0 else depth // 8

        # Extrai pixels: reshape linha por linha, depois pega só os canais BGR
        raw = np.frombuffer(pixel_data[:needed], dtype=np.uint8)
        raw = raw.reshape((height, bytes_per_line))

        # Pega a largura correta e só 3 canais (BGR)
        valid_w = min(width, expected_w)
        img = raw[:, :valid_w * actual_bpp].reshape((height, valid_w, actual_bpp))
        img = img[:, :, :3].copy()

        # Corta para a altura esperada
        img = img[:min(height, expected_h)]

        # XWD é bottom-up — inverte verticalmente
        img = np.flipud(img)

        # Detecta ordem das cores pelos masks
        # Se red_mask < blue_mask → dados estão em ordem RGB, inverter para BGR
        if red_mask > 0 and blue_mask > 0 and red_mask < blue_mask:
            img = img[:, :, ::-1].copy()

        return img
    except Exception as exc:
        log.debug("XWD parse falhou: %s", exc)
        return None


def is_xvfb_available():
    """Verifica: Xvfb instalado e tem display livre?"""
    if not shutil.which("Xvfb"):
        return False
    for d in range(1, 100):
        socket_file = f"/tmp/.X11-unix/X{d}"
        lock_file = f"/tmp/.X{d}-lock"
        try:
            if os.path.exists(lock_file):
                with open(lock_file) as f:
                    pid = int(f.read().strip().split()[0])
                    os.kill(pid, 0)  # processo vivo? mantém
            if not os.path.exists(socket_file):
                return True
        except (ProcessLookupError, ValueError, PermissionError, FileNotFoundError):
            # Processo morto mas lock ficou — limpa
            try:
                os.remove(lock_file)
            except Exception:
                pass
            try:
                os.remove(socket_file)
            except Exception:
                pass
            return True
    return False
