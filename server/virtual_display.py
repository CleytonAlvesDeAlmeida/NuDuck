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

        # Espera captura estabilizar e verifica
        time.sleep(3.0)
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

        Prioriza xterm (respeita DISPLAY, não usa D-Bus).
        Se nenhum terminal gráfico funcionar, cria um launcher Tkinter.
        """
        opened = False

        # Lista de terminais: xterm primeiro (mais confiável com Xvfb)
        terminals = [
            ("xterm", []),
            ("uxterm", []),
            ("lxterminal", []),
            ("x-terminal-emulator", []),  # Debian/Ubuntu alternative
            ("konsole", []),
            # gnome-terminal por último — usa D-Bus e pode abrir no monitor errado
            ("gnome-terminal", ["--disable-factory"]),
        ]

        for term_name, extra_args in terminals:
            if shutil.which(term_name):
                try:
                    args = [term_name] + extra_args
                    if term_name == "gnome-terminal":
                        args += ["--"]
                    subprocess.Popen(
                        args + ["/bin/bash"],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    log.info("Terminal '%s' aberto em %s", term_name, self.display_name)
                    opened = True
                    break
                except Exception:
                    continue

        if not opened:
            log.warning("Nenhum terminal gráfico encontrado. Criando launcher Tkinter...")
            self._open_tkinter_launcher(env)

    def _open_tkinter_launcher(self, env):
        """Cria uma janela Tkinter simples no Xvfb como fallback de terminal."""
        try:
            script = f'''
import tkinter as tk
import subprocess, os

os.environ["DISPLAY"] = "{self.display_name}"
root = tk.Tk()
root.title("NuDuck - Segunda Tela")
root.geometry("500x350")
root.configure(bg="#1a1a2e")

lbl = tk.Label(root, text="NuDuck - Display Virtual", fg="white", bg="#1a1a2e",
               font=("Sans", 14, "bold"))
lbl.pack(pady=(10, 5))

lbl2 = tk.Label(root, text="Digite um comando e pressione Enter:", fg="#aaaaaa", bg="#1a1a2e",
                font=("Sans", 10))
lbl2.pack(pady=(0, 5))

entry = tk.Entry(root, font=("Consolas", 11), bg="#16213e", fg="white", insertbackground="white")
entry.pack(fill="x", padx=15, pady=2)

def run_cmd(event=None):
    cmd = entry.get().strip()
    if cmd:
        entry.delete(0, "end")
        subprocess.Popen(cmd.split(), env=os.environ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

entry.bind("<Return>", run_cmd)

btn = tk.Button(root, text="Abrir", command=run_cmd, bg="#0f3460", fg="white",
                font=("Sans", 10))
btn.pack(pady=5)

hint = tk.Label(root, text='Exemplos: firefox, nautilus, xterm\\n'
               'Ou no terminal do PC: DISPLAY={self.display_name} nome_do_app &',
               fg="#666666", bg="#1a1a2e", font=("Sans", 9), justify="center")
hint.pack(pady=10)

root.mainloop()
'''
            subprocess.Popen(
                ["python3", "-c", script],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("Launcher Tkinter criado em %s", self.display_name)
        except Exception as exc:
            log.warning("Falha ao criar launcher Tkinter: %s", exc)

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
        """Loop de captura — testa métodos na inicialização, usa o melhor.

        Fase 1 (setup): testa xwd com timeout. Se falhar, tenta alternativas.
        Fase 2 (loop):  usa o método que funcionou, a ~30fps.
        """
        display_name = self.display_name
        capture_fn = None
        _fail_count = [0]  # mutable counter para closure
        MAX_FAIL = 50  # após N falhas seguidas, tenta outro método

        # ===== FASE 1: Encontrar método de captura que funciona =====

        # Método 1: xwd (X Window Dump)
        if shutil.which("xwd"):
            log.info("Testando captura via xwd em %s...", display_name)
            fn = self._test_xwd(display_name)
            if fn is not None:
                capture_fn = fn
                log.info("Captura via xwd OK em %s", display_name)
            else:
                log.warning("xwd falhou em %s, tentando outro método", display_name)

        # Método 2: ImageMagick import
        if capture_fn is None and shutil.which("import"):
            log.info("Testando captura via ImageMagick import em %s...", display_name)
            fn = self._test_imagemagick(display_name)
            if fn is not None:
                capture_fn = fn
                log.info("Captura via ImageMagick import OK em %s", display_name)
            else:
                log.warning("ImageMagick import falhou em %s", display_name)

        # Método 3: fallback colorido
        if capture_fn is None:
            log.warning("Nenhum método de captura funcionou. Usando frame colorido.")
            bg = np.full((self.height, self.width, 3), [46, 26, 26], dtype=np.uint8)

            def capture_fallback():
                with self._frame_lock:
                    np.copyto(self._latest_frame, bg)
                self._new_frame_event.set()

            capture_fn = capture_fallback

        # ===== FASE 2: Loop de captura =====
        while not self._stop_event.is_set():
            try:
                capture_fn()
                _fail_count[0] = 0
            except Exception:
                _fail_count[0] += 1
                if _fail_count[0] == MAX_FAIL:
                    log.error("Captura falhou %dx consecutivas!", MAX_FAIL)
            self._stop_event.wait(0.033)  # ~30fps

    def _test_xwd(self, display_name):
        """Testa se xwd funciona. Retorna função de captura ou None."""
        try:
            proc = subprocess.Popen(
                ["xwd", "-root", "-display", display_name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "DISPLAY": display_name},
            )
            try:
                stdout, stderr = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=1)
                log.warning("xwd: timeout (não respondeu em 3s)")
                return None

            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()[:200]
                log.warning("xwd: erro (rc=%d): %s", proc.returncode, err)
                return None

            if len(stdout) < 100:
                log.warning("xwd: dados insuficientes (%d bytes)", len(stdout))
                return None

            # Tenta converter
            img = _xwd_to_bgr(stdout, self.width, self.height)
            if img is None:
                log.warning("xwd: dados recebidos mas parser falhou")
                return None

            log.info("xwd OK — frame %dx%d, tamanho=%d bytes", img.shape[1], img.shape[0], len(stdout))

            # Salva primeiro frame
            with self._frame_lock:
                np.copyto(self._latest_frame, img)
            self._new_frame_event.set()

            # Retorna função de captura rápida para o loop
            shape = self._shape

            def capture_xwd():
                try:
                    p = subprocess.Popen(
                        ["xwd", "-root", "-display", display_name],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        env={**os.environ, "DISPLAY": display_name},
                    )
                    try:
                        out, _ = p.communicate(timeout=1)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.communicate(timeout=0.5)
                        return
                    if p.returncode == 0 and len(out) > 100:
                        result = _xwd_to_bgr(out, shape[1], shape[0])
                        if result is not None:
                            with self._frame_lock:
                                np.copyto(self._latest_frame, result)
                            self._new_frame_event.set()
                except Exception:
                    pass

            return capture_xwd

        except Exception as exc:
            log.warning("xwd exception: %s", exc)
            return None

    def _test_imagemagick(self, display_name):
        """Testa captura via ImageMagick 'import'. Retorna função ou None."""
        try:
            proc = subprocess.Popen(
                ["import", "-display", display_name, "-window", "root",
                 "-depth", "24", "png:-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "DISPLAY": display_name},
            )
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=1)
                log.warning("ImageMagick: timeout")
                return None

            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()[:200]
                log.warning("ImageMagick: erro (rc=%d): %s", proc.returncode, err)
                return None

            if len(stdout) < 100:
                log.warning("ImageMagick: dados insuficientes (%d bytes)", len(stdout))
                return None

            # Converte PNG para numpy
            import io
            from PIL import Image
            img_pil = Image.open(io.BytesIO(stdout)).convert("RGB")
            img_rgb = np.array(img_pil)
            img_bgr = img_rgb[:, :, ::-1].copy()

            # Redimensiona se necessário
            if img_bgr.shape[:2] != (self.height, self.width):
                from PIL import Image as PILImage
                pil = PILImage.fromarray(img_rgb)
                pil = pil.resize((self.width, self.height), PILImage.BILINEAR)
                img_bgr = np.array(pil)[:, :, ::-1].copy()

            with self._frame_lock:
                np.copyto(self._latest_frame, img_bgr)
            self._new_frame_event.set()
            log.info("ImageMagick import OK — frame %dx%d", img_bgr.shape[1], img_bgr.shape[0])

            shape = self._shape

            def capture_magick():
                try:
                    p = subprocess.Popen(
                        ["import", "-display", display_name, "-window", "root",
                         "-depth", "24", "png:-"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        env={**os.environ, "DISPLAY": display_name},
                    )
                    try:
                        out, _ = p.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.communicate(timeout=0.5)
                        return
                    if p.returncode == 0 and len(out) > 100:
                        pil = Image.open(io.BytesIO(out)).convert("RGB")
                        arr = np.array(pil)[:, :, ::-1]
                        if arr.shape[:2] != (shape[0], shape[1]):
                            pil2 = PILImage.fromarray(arr[:, :, ::-1])
                            pil2 = pil2.resize((shape[1], shape[0]), PILImage.BILINEAR)
                            arr = np.array(pil2)[:, :, ::-1]
                        with self._frame_lock:
                            np.copyto(self._latest_frame, arr)
                        self._new_frame_event.set()
                except Exception:
                    pass

            return capture_magick

        except ImportError:
            log.warning("PIL/Pillow não encontrado para ImageMagick fallback")
            return None
        except Exception as exc:
            log.warning("ImageMagick exception: %s", exc)
            return None

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
    """Converte dados XWD (Format B) para numpy array BGR.

    Offset table do header XWD (cada campo = CARD32 big-endian = 4 bytes):
      0: header_size       28: byte_order
      4: file_version       32: bitmap_unit
      8: format             36: bitmap_bit_order
     12: pixmap_depth       40: bitmap_pad
     16: pixmap_width       44: bits_per_pixel
     20: pixmap_height      48: bytes_per_line
     24: xoffset            52: visual_class
                            56: red_mask
                            60: green_mask
                            64: blue_mask
    Pixel data starts at data[header_size:].
    """
    try:
        if len(data) < 100:
            return None

        # Lê header XWD (big-endian)
        header_size = struct.unpack(">I", data[0:4])[0]
        pixmap_width = struct.unpack(">I", data[16:20])[0]
        pixmap_height = struct.unpack(">I", data[20:24])[0]
        bits_per_pixel = struct.unpack(">I", data[44:48])[0]
        bytes_per_line = struct.unpack(">I", data[48:52])[0]
        red_mask = struct.unpack(">I", data[56:60])[0]
        green_mask = struct.unpack(">I", data[60:64])[0]
        blue_mask = struct.unpack(">I", data[64:68])[0]

        if bits_per_pixel not in (24, 32):
            log.debug("XWD: bits_per_pixel=%d (precisa 24 ou 32)", bits_per_pixel)
            return None

        if header_size < 100 or header_size > 100000:
            log.debug("XWD: header_size=%d suspeito", header_size)
            return None

        width = pixmap_width
        height = pixmap_height

        if width < 10 or height < 10 or width > 7680 or height > 4320:
            log.debug("XWD: dimensões suspeitas %dx%d", width, height)
            return None

        pixel_data = data[header_size:]

        # Verifica dados suficientes
        needed = bytes_per_line * height
        if len(pixel_data) < needed:
            log.debug("XWD: dados insuficientes (tem %d, precisa %d)", len(pixel_data), needed)
            return None

        # Bytes por pixel a partir de bytes_per_line
        actual_bpp = bytes_per_line // width if width > 0 else bits_per_pixel // 8

        if actual_bpp < 3:
            log.debug("XWD: actual_bpp=%d < 3 (bytes_per_line=%d, width=%d)", actual_bpp, bytes_per_line, width)
            return None

        # Extrai pixels
        raw = np.frombuffer(pixel_data[:needed], dtype=np.uint8)
        raw = raw.reshape((height, bytes_per_line))

        valid_w = min(width, expected_w)
        valid_h = min(height, expected_h)
        img = raw[:valid_h, :valid_w * actual_bpp].reshape((valid_h, valid_w, actual_bpp))
        img = img[:, :, :3].copy()

        # XWD é bottom-up — inverte
        img = np.flipud(img)

        # Detecta ordem RGB vs BGR pelos masks
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
