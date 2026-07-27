"""
virtual_display.py - Display virtual Xvfb para o modo Estender do NuDuck.

Funciona igual ao SpaceDesk: cria um servidor X separado (Xvfb) que o
celular renderiza e controla, funcionando como uma segunda tela virtual.

Requisitos:
  sudo apt install xvfb xdotool openbox xsetroot x11-utils
"""

import io
import logging
import multiprocessing as mp
import numpy as np
import os
import shutil
import signal
import struct
import subprocess
import time

log = logging.getLogger("NuDuck")

# Referência global ao Xvfb atual — garante que só exista um por vez
_active_display = None


def get_active_display():
    """Retorna o Xvfb ativo, ou None."""
    return _active_display


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
        self._capture_worker = None
        self._shared_array = None
        self._new_frame_event = None
        self._stop_event = None
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
            return False, "Xvfb não encontrado. Instale: sudo apt install xvfb xdotool openbox", None

        # Mata qualquer Xvfb órfão antes de tentar
        self._cleanup_orphan_xvfb()

        # Procura um display livre
        for d in range(1, 100):
            lock_file = f"/tmp/.X{d}-lock"
            socket_file = f"/tmp/.X11-unix/X{d}"

            # Tenta remover resquício de Xvfb morto
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
                    "+extension", "Composite",
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

        # Abre um terminal automaticamente
        self._open_initial_terminal(env)

        # Inicia captura
        self._start_capture_worker()

        # Espera um pouco e verifica se a captura está funcionando
        time.sleep(1.0)
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
                # Lê o PID do lock file — se o processo não existe, remove
                try:
                    with open(lock_file) as f:
                        pid_str = f.read().strip().split()[0]
                        pid = int(pid_str)
                        # Verifica se o processo realmente existe
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
        """Abre um terminal no display virtual."""
        terminals = ("xterm", "uxterm", "lxterminal", "gnome-terminal", "konsole")
        for term in terminals:
            if shutil.which(term):
                try:
                    args = [term]
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
        log.warning("Nenhum terminal encontrado.")

    def stop(self):
        """Para o Xvfb, WM, captura e limpa os arquivos de socket."""
        log.info("Encerrando display virtual %s...", self.display_name)

        # Sinaliza parada do worker
        if self._stop_event:
            self._stop_event.set()

        # Mata o processo de captura
        if self._capture_worker:
            self._capture_worker.join(timeout=2)
            if self._capture_worker.is_alive():
                self._capture_worker.kill()
                self._capture_worker.join(timeout=1)
            self._capture_worker = None

        # Mata WM e Xvfb
        if self.wm_process:
            try:
                self.wm_process.terminate()
                self.wm_process.wait(timeout=2)
            except Exception:
                pass
            self.wm_process = None

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

        # Limpa recursos de memória compartilhada
        self._shared_array = None
        self._new_frame_event = None
        self._stop_event = None

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
        """Retorna o último frame BGR (H, W, 3), ou gera um frame colorido."""
        if not self._started or not self._shared_array:
            # Sem display — gera frame azul escuro
            return np.full(self._shape, [46, 26, 26], dtype=np.uint8)

        # Tenta pegar frame do worker
        if self._new_frame_event and self._new_frame_event.wait(timeout=0.1):
            self._new_frame_event.clear()
            try:
                return (
                    np.frombuffer(self._shared_array.get_obj(), dtype=np.uint8)
                    .reshape(self._shape)
                    .copy()
                )
            except Exception:
                pass

        # Worker não produziu frame — gera frame colorido de fallback
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
# Processo de captura
# ----------------------------------------------------------------------

def _capture_worker(display_name, shared_array, shape, new_frame_event, stop_event, width, height):
    """Processo filho que captura o framebuffer do Xvfb.

    Tenta mss primeiro. Se falhar, usa xwd. Se os dois falharem,
    gera um frame colorido (nunca preto).
    """
    os.environ["DISPLAY"] = display_name
    os.environ["HOME"] = os.path.expanduser("~")

    capture_fn = None

    # --- Método 1: mss ---
    try:
        import mss

        sct = mss.mss()
        monitors = sct.monitors
        if len(monitors) >= 2:
            mon = monitors[1]
            log.info("Capture (mss) ativo em %s: %dx%d", display_name, mon["width"], mon["height"])

            def capture_mss():
                try:
                    raw = sct.grab(mon)
                    img = np.array(raw)[:, :, :3]
                    buf = np.frombuffer(shared_array.get_obj(), dtype=np.uint8).reshape(shape)
                    np.copyto(buf, img)
                    new_frame_event.set()
                except Exception:
                    pass  # não mata o worker, tenta de novo no próximo ciclo

            capture_fn = capture_mss
        else:
            log.warning("mss não encontrou monitores em %s, tentando xwd", display_name)
    except Exception as exc:
        log.warning("mss falhou em %s (%s), tentando xwd", display_name, exc)

    # --- Método 2: xwd (X Window Dump) ---
    if capture_fn is None and shutil.which("xwd"):
        log.info("Capture (xwd) ativo em %s", display_name)

        def capture_xwd():
            try:
                proc = subprocess.Popen(
                    ["xwd", "-root", "-display", display_name],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env={**os.environ, "DISPLAY": display_name},
                )
                xwd_data = proc.stdout.read()

                if len(xwd_data) > 100:
                    img_rgb = _xwd_to_rgb(xwd_data, width, height)
                    if img_rgb is not None:
                        img_bgr = img_rgb[:, :, ::-1].copy()
                        buf = np.frombuffer(shared_array.get_obj(), dtype=np.uint8).reshape(shape)
                        np.copyto(buf, img_bgr)
                        new_frame_event.set()
            except Exception:
                pass

        capture_fn = capture_xwd

    # --- Método 3: frame colorido ---
    if capture_fn is None:
        log.warning("mss e xwd falharam, usando frame colorido")
        bg = np.full((height, width, 3), [46, 26, 26], dtype=np.uint8)  # azul escuro BGR

        def capture_fallback():
            buf = np.frombuffer(shared_array.get_obj(), dtype=np.uint8).reshape(shape)
            np.copyto(buf, bg)
            new_frame_event.set()

        capture_fn = capture_fallback

    # Loop principal — nunca morre, tenta de novo a cada frame
    while not stop_event.is_set():
        try:
            capture_fn()
            stop_event.wait(0.033)  # ~30fps
        except Exception:
            stop_event.wait(0.1)


def _xwd_to_rgb(data, expected_w, expected_h):
    """Converte dados XWD para numpy RGB."""
    try:
        if len(data) < 100:
            return None

        header_size = struct.unpack(">I", data[52:56])[0]
        bpp = struct.unpack(">I", data[48:52])[0]

        if bpp not in (24, 32):
            return None

        pixel_data = data[header_size:]
        bytes_per_pixel = bpp // 8
        row_size = expected_w * bytes_per_pixel

        if len(pixel_data) < row_size * expected_h:
            return None

        img = np.frombuffer(pixel_data[:row_size * expected_h], dtype=np.uint8)
        img = img.reshape((expected_h, expected_w, bytes_per_pixel))
        img = np.flipud(img)
        bgr = img[:, :, :3]
        return bgr[:, :, ::-1].copy()
    except Exception:
        return None


def is_xvfb_available():
    """Verifica: Xvfb instalado e tem display livre?"""
    if not shutil.which("Xvfb"):
        return False
    for d in range(1, 100):
        socket_file = f"/tmp/.X11-unix/X{d}"
        lock_file = f"/tmp/.X{d}-lock"

        # Limpa resquício
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
