"""
virtual_display.py - Display virtual Xvfb para o modo Estender do NuDuck.

Funciona igual ao SpaceDesk: cria um servidor X separado (Xvfb) que o
celular renderiza e controla, funcionando como uma segunda tela virtual.

Requisitos:
  sudo apt install xvfb xdotool openbox x11-xserver-utils x11-apps xterm x2x feh dconf-cli

Nota: pacotes chamados "xsetroot" ou "x11-utils" NÃO existem no apt para
esse fim (xsetroot vem dentro de x11-xserver-utils, e xwd vem dentro de
x11-apps, não de x11-utils). Colocar um nome errado no comando de
instalação faz o apt cancelar a instalação inteira.
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
import urllib.parse

try:
    import mss
except ImportError:
    mss = None

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


def _paint_wallpaper_pure_xlib(display_name: str, image_path: str) -> None:
    """Pinta o papel de parede direto no root window via Xlib + Pillow —
    sem depender de nenhum programa externo (feh, hsetroot, nitrogen...).

    Correção do aviso "feh não está instalado": antes, sem o `feh`
    instalado no PC, o papel de parede do display virtual (Estender)
    simplesmente não aparecia — só um aviso no log pedindo pra instalar
    o pacote. Só que `feh` é só um programinha que faz basicamente isto
    aqui: redimensiona a imagem "cobrindo" a tela toda (preservando a
    proporção, cortando o excesso) e desenha ela na janela-raiz do X.
    Como o server já depende de `python-xlib` (usado pra detectar o
    formato do cursor, ver item 4) e de `Pillow` (já vem com o
    `qrcode[pil]`), dá pra fazer a mesma coisa sem exigir mais nenhum
    pacote do sistema — então agora ISSO roda como alternativa quando o
    `feh` não está instalado, em vez de só avisar e desistir.

    Levanta uma exceção se algo der errado (a função que chama já trata
    isso com try/except e cai no fallback de cor sólida, igual fazia
    quando o feh falhava).
    """
    from Xlib import X, Xatom
    from Xlib import display as xlib_display
    from PIL import Image

    d = xlib_display.Display(display_name)
    try:
        screen = d.screen()
        root = screen.root
        width, height = screen.width_in_pixels, screen.height_in_pixels
        depth = screen.root_depth

        img = Image.open(image_path).convert("RGB")
        src_w, src_h = img.size
        if src_w <= 0 or src_h <= 0:
            raise ValueError("imagem de papel de parede com dimensões inválidas")

        # Mesmo comportamento do "--bg-scale" do feh: preserva a
        # proporção da imagem, preenche a tela toda e corta o excesso
        # (em vez de esticar/distorcer ou deixar sobra).
        scale = max(width / src_w, height / src_h)
        new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - width) // 2
        top = (new_h - height) // 2
        img = img.crop((left, top, left + width, top + height))

        # BGRX de 32 bits é o layout de pixel mais comum em visuais
        # true-color de desktops Linux (little-endian) — cobre a
        # esmagadora maioria dos casos reais.
        raw = img.tobytes("raw", "BGRX")

        gc = root.create_gc()
        pixmap = root.create_pixmap(width, height, depth)
        pixmap.put_image(gc, 0, 0, width, height, X.ZPixmap, depth, 0, raw)

        root.change_attributes(background_pixmap=pixmap)
        root.clear_area(0, 0, width, height, exposures=False)

        # Marca qual é o pixmap de fundo atual (_XROOTPMAP_ID) — é o
        # jeito padrão de outros programas saberem "qual é o papel de
        # parede", igual o feh também faz.
        atom = d.intern_atom("_XROOTPMAP_ID")
        root.change_property(atom, Xatom.PIXMAP, 32, [pixmap.id])

        d.sync()
    finally:
        d.close()


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
        self._x2x_process = None
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
        self._mss_instance = None

        # Correção de CPU alta no modo Estender: antes, esta thread
        # capturava o framebuffer do Xvfb a ~30fps FIXO, o tempo todo —
        # mesmo quando a qualidade escolhida pedia menos fps, e mesmo
        # quando a tela estava parada (ver throttling de tela parada em
        # server.py). Ou seja, era um segundo "motor" rodando na
        # velocidade máxima por trás, gastando CPU à toa, independente
        # do que a transmissão de vídeo realmente precisava. Agora o
        # intervalo é ajustável (ver set_capture_interval), e o
        # server.py mantém os dois sincronizados: quando a qualidade
        # muda ou a tela fica parada, esta thread desacelera junto.
        self._capture_interval = 1.0 / 30.0
        self._capture_interval_lock = threading.Lock()

        # Correção de bug (cursor "parado"/"não aparece" no modo Estender):
        # a versão anterior usava uma thread separada perguntando pro X11
        # (via Xlib) onde o mouse estava, ~20x/s. Isso depende de uma
        # segunda conexão X11 própria ficar de pé o tempo todo — se essa
        # conexão falhar silenciosamente (ex.: timing na inicialização do
        # Xvfb, autenticação), a posição nunca é atualizada e o cursor no
        # celular fica travado no valor padrão.
        #
        # Como o Xvfb não tem mouse físico nenhum — a ÚNICA forma da seta
        # se mexer é através de send_input() (o toque do celular, via
        # xdotool) — não faz sentido "perguntar" pro X11 onde o mouse
        # está: nós mesmos JÁ SABEMOS, porque fomos nós que mandamos ele
        # pra lá. Agora send_input() atualiza self._mouse_pos direto,
        # sem nenhuma conexão/consulta extra — impossível "não bater" com
        # a realidade, e uma thread a menos rodando o tempo todo.
        # Item novo: antes começava em None (cursor "invisível" até o
        # primeiro toque chegar do celular) — agora começa no centro da
        # tela, pra já aparecer alguma coisa assim que a transmissão
        # começar, mesmo sem nenhum toque ainda.
        self._mouse_pos = (width // 2, height // 2)
        self._mouse_pos_lock = threading.Lock()

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

        # Ativa o x2x assim que o terminal abre: mouse/teclado passam a
        # atravessar pro Display 2 pela borda direita da tela. Roda logo
        # depois do terminal existir, mesma lógica do clone de aparência
        # abaixo (precisa de uma janela já mapeada no display).
        self._start_x2x()

        # Clona aparência do display principal em segundo plano (com
        # retry — ver _clone_display0_appearance_with_retry). Roda
        # assíncrono de propósito: a versão síncrona anterior tinha sido
        # removida por deixar a conexão lenta; rodando em background, ela
        # não atrasa mais nada, e o retry resolve o "às vezes clona, às
        # vezes não" causado por timing (WM ainda não pronto no 1º try).
        self._clone_display0_appearance_async()

        # Inicia captura (thread, não processo — sem leak de memória compartilhada)
        self._start_capture_thread()
        # Idem para a posição do mouse — ver get_mouse_position().

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

    def _start_x2x(self):
        """Ativa o x2x: deixa o mouse/teclado do PC "atravessar" para o
        display virtual quando o cursor é levado até a borda direita
        ("east") do monitor principal — como se o Display 2 fosse um
        monitor físico a mais, sem precisar clicar em nada.

        Roda no display principal (:0) apontando para o display virtual
        (`-to <display_name>`), então precisa ser reiniciado sempre que o
        display virtual for recriado (resize/rotação) e ser parado ao sair
        do Estender — senão o cursor continuaria "vazando" pra um display
        que não existe mais.
        """
        if not shutil.which("x2x"):
            log.warning(
                "x2x não está instalado — mouse/teclado não vão atravessar "
                "automaticamente para o Display 2. Instale: sudo apt install x2x"
            )
            return
        if self._x2x_process and self._x2x_process.poll() is None:
            # Já tem uma instância rodando (ex.: resize sem stop antes) —
            # mata antes de subir outra, senão duas instâncias disputam o
            # mesmo par de displays.
            self._stop_x2x()
        try:
            env0 = self._build_session_env(":0")
            self._x2x_process = subprocess.Popen(
                ["x2x", "-east", "-to", self.display_name],
                env=env0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            log.info(
                "x2x ativado: mouse/teclado atravessam para %s pela borda direita da tela.",
                self.display_name,
            )
        except Exception as exc:
            log.warning("Erro ao iniciar x2x: %s", exc)

    def _stop_x2x(self):
        """Encerra o x2x — chamado ao sair do Estender, pra o cursor voltar
        a ficar preso no display principal em vez de continuar tentando
        atravessar para um display que não existe mais."""
        if self._x2x_process:
            try:
                self._x2x_process.terminate()
                self._x2x_process.wait(timeout=2)
            except Exception:
                try:
                    self._x2x_process.kill()
                except Exception:
                    pass
            self._x2x_process = None
            log.info("x2x encerrado.")

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

    def _build_session_env(self, display: str) -> dict:
        """Monta um env robusto para subprocessos gsettings/dconf/feh/xsetroot.

        Cobre o caso comum do server rodar fora de uma sessão gráfica completa
        (serviço systemd, terminal via SSH etc.), onde DBUS_SESSION_BUS_ADDRESS
        e XDG_RUNTIME_DIR não vêm no os.environ herdado pelo processo — e sem
        eles, todo `gsettings`/`dconf` falha silenciosamente (retorna vazio,
        sem erro visível), dando a impressão de que "nada foi clonado".
        """
        env = dict(os.environ)
        env["DISPLAY"] = display
        uid = os.getuid()
        if "DBUS_SESSION_BUS_ADDRESS" not in env:
            candidate = f"/run/user/{uid}/bus"
            if os.path.exists(candidate):
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={candidate}"
        if "XDG_RUNTIME_DIR" not in env:
            candidate = f"/run/user/{uid}"
            if os.path.isdir(candidate):
                env["XDG_RUNTIME_DIR"] = candidate
        return env

    def _clone_display0_appearance(self):
        """Clona tema GTK, ícones, fonte, cursor, papel de parede e variante
        de cor do display principal (:0) para o display virtual (:N).

        Duas causas raiz descobertas depois que a v1 desta função não estava
        clonando nada na prática:

        1. `gsettings` fala com o dconf via D-Bus e exige o pacote
           `gsettings-desktop-schemas` compilado/instalado. Em ambientes sem
           sessão gráfica completa (serviço systemd, SSH sem DISPLAY
           exportado, etc.) `DBUS_SESSION_BUS_ADDRESS`/`XDG_RUNTIME_DIR` não
           chegam no `os.environ` do processo do server — e todo
           `gsettings get` falha silenciosamente (stdout vazio, sem
           exception), então nada era lido e nada era aplicado.
           `dconf read` lê o valor direto do arquivo binário do dconf, sem
           depender de D-Bus nem de schema instalado — muito mais robusto
           nesse cenário, então tentamos ele primeiro e caímos para
           `gsettings` só como fallback.
        2. O `picture-uri` do GNOME vem URL-encoded (ex.: espaço vira
           "%20"). Sem decodificar, `os.path.exists()` falhava mesmo com o
           caminho correto, e o papel de parede nunca era aplicado.

        Nota importante: tema GTK/ícones/cursor/fonte já ficam
        automaticamente compartilhados entre displays pelo dconf (a
        configuração é por usuário, não por DISPLAY) — então a etapa que
        de fato muda algo *visível* no display virtual (que não roda uma
        sessão de desktop própria, só Xvfb + um WM leve) é o papel de
        parede, pintado direto no root window via `feh --bg-scale`
        apontado para o DISPLAY=:N. É por isso que ela recebe tratamento
        de fallback (cor sólida) caso a imagem não seja encontrada.
        """
        if not self.display_name:
            return
        if not shutil.which("dconf") and not shutil.which("gsettings") and not shutil.which("xfconf-query"):
            log.warning(
                "Nem dconf, gsettings nem xfconf-query disponíveis — aparência do display "
                "virtual não será clonada do display principal. Instale: "
                "sudo apt install dconf-cli feh"
            )
            return

        env0 = self._build_session_env(":0")
        env_n = self._build_session_env(self.display_name)

        # Lê uma chave direto do dconf (sem D-Bus, sem depender de schema).
        def read_dconf(path: str) -> str | None:
            if not shutil.which("dconf"):
                return None
            try:
                r = subprocess.run(
                    ["dconf", "read", path],
                    env=env0, capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0 and r.stdout.strip():
                    val = r.stdout.strip()
                    if val.startswith("'") and val.endswith("'"):
                        val = val[1:-1]
                    return val or None
            except Exception:
                pass
            return None

        # Lê um valor gsettings do display :0 (fallback se dconf não achar).
        def read_gsettings_d0(schema: str, key: str) -> str | None:
            if not shutil.which("gsettings"):
                return None
            try:
                r = subprocess.run(
                    ["gsettings", "get", schema, key],
                    env=env0, capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0:
                    val = r.stdout.strip()
                    if val.startswith("'") and val.endswith("'"):
                        val = val[1:-1]
                    elif val.startswith('"') and val.endswith('"'):
                        val = val[1:-1]
                    return val if val else None
            except Exception:
                pass
            return None

        def read_setting(dconf_path: str, gschema: str, key: str) -> str | None:
            return read_dconf(dconf_path) or read_gsettings_d0(gschema, key)

        # Aplica um valor gsettings no display virtual.
        def apply_gsettings_dn(schema: str, key: str, value: str) -> bool:
            if not shutil.which("gsettings") or not value:
                return False
            try:
                r = subprocess.run(
                    ["gsettings", "set", schema, key, f"'{value}'"],
                    env=env_n, capture_output=True, text=True, timeout=2,
                )
                return r.returncode == 0
            except Exception:
                return False

        # Lê paper-uri do GNOME (dark ou light), já com URL-decode.
        def read_gnome_wallpaper() -> str | None:
            for key in ("picture-uri-dark", "picture-uri"):
                val = read_setting(
                    f"/org/gnome/desktop/background/{key}",
                    "org.gnome.desktop.background", key,
                )
                if val:
                    if val.startswith("file://"):
                        val = val[7:]
                    return urllib.parse.unquote(val)
            return None

        # Lê wallpaper do XFCE (tenta as chaves antigas e a nova por workspace).
        def read_xfce_wallpaper() -> str | None:
            if not shutil.which("xfconf-query"):
                return None
            for prop in (
                "/backdrop/screen0/monitor0/workspace0/last-image",
                "/backdrop/screen0/monitor0/image-path",
                "/backdrop/screen0/monitorLVDS1/workspace0/last-image",
            ):
                try:
                    r = subprocess.run(
                        ["xfconf-query", "-c", "xfce4-desktop", "-p", prop],
                        env=env0, capture_output=True, text=True, timeout=2,
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        return urllib.parse.unquote(r.stdout.strip())
                except Exception:
                    pass
            return None

        applied_count = 0

        # --- Tema GTK ---
        gtk_theme = read_setting("/org/gnome/desktop/interface/gtk-theme",
                                  "org.gnome.desktop.interface", "gtk-theme")
        if gtk_theme and apply_gsettings_dn("org.gnome.desktop.interface", "gtk-theme", gtk_theme):
            applied_count += 1
            log.info("Tema GTK clonado: %s", gtk_theme)

        # --- Tema de ícones ---
        icon_theme = read_setting("/org/gnome/desktop/interface/icon-theme",
                                   "org.gnome.desktop.interface", "icon-theme")
        if icon_theme and apply_gsettings_dn("org.gnome.desktop.interface", "icon-theme", icon_theme):
            applied_count += 1
            log.info("Tema de ícones clonado: %s", icon_theme)

        # --- Fonte ---
        font_name = read_setting("/org/gnome/desktop/interface/font-name",
                                  "org.gnome.desktop.interface", "font-name")
        if font_name and apply_gsettings_dn("org.gnome.desktop.interface", "font-name", font_name):
            applied_count += 1
            log.info("Fonte clonada: %s", font_name)

        # --- Tema de cursor ---
        cursor_theme = read_setting("/org/gnome/desktop/interface/cursor-theme",
                                     "org.gnome.desktop.interface", "cursor-theme")
        if cursor_theme and apply_gsettings_dn("org.gnome.desktop.interface", "cursor-theme", cursor_theme):
            applied_count += 1
            log.info("Tema de cursor clonado: %s", cursor_theme)

        # --- Variante de cor (claro/escuro) ---
        color_scheme = read_setting("/org/gnome/desktop/interface/color-scheme",
                                     "org.gnome.desktop.interface", "color-scheme")
        if color_scheme and apply_gsettings_dn("org.gnome.desktop.interface", "color-scheme", color_scheme):
            applied_count += 1
            log.info("Variante de cor clonada: %s", color_scheme)

        # --- Papel de parede: a única etapa que muda algo de fato visível no
        # display virtual, já que ele não roda uma sessão de desktop própria. ---
        wallpaper = read_gnome_wallpaper() or read_xfce_wallpaper()
        applied_wallpaper = False
        if wallpaper and os.path.exists(wallpaper):
            if shutil.which("feh"):
                try:
                    r = subprocess.run(
                        ["feh", "--bg-scale", wallpaper],
                        env=env_n, capture_output=True, text=True, timeout=3,
                    )
                    if r.returncode == 0:
                        applied_count += 1
                        applied_wallpaper = True
                        log.info("Papel de parede clonado: %s", wallpaper)
                    else:
                        log.warning("feh falhou ao aplicar papel de parede: %s",
                                    r.stderr.strip() or r.stdout.strip())
                except Exception as exc:
                    log.warning("Erro ao aplicar papel de parede via feh: %s", exc)
            else:
                # Correção do aviso "feh não está instalado": antes,
                # sem o feh, o papel de parede simplesmente não
                # aparecia. Agora usa Xlib + Pillow direto (mesmas
                # dependências que o server já tem), sem precisar
                # instalar mais nada no PC.
                try:
                    _paint_wallpaper_pure_xlib(self.display_name, wallpaper)
                    applied_count += 1
                    applied_wallpaper = True
                    log.info("Papel de parede clonado (via Xlib, sem feh): %s", wallpaper)
                except Exception as exc:
                    log.warning(
                        "Não consegui pintar o papel de parede sem o feh (%s). "
                        "Alternativa: sudo apt install feh", exc,
                    )
        elif wallpaper:
            log.warning("Papel de parede do display principal não encontrado no disco: %s", wallpaper)
        else:
            log.warning(
                "Não consegui descobrir o papel de parede do display principal (dconf/"
                "gsettings/xfconf-query não retornaram nada — confira se o server tem "
                "acesso à sessão gráfica, ver _build_session_env)."
            )

        # Fallback: se não deu pra clonar o papel de parede, ao menos pinta o
        # fundo com uma cor sólida — evita o xadrez/preto cru padrão do Xvfb,
        # que reforça a impressão de "não clonou nada".
        if not applied_wallpaper and shutil.which("xsetroot"):
            try:
                subprocess.run(["xsetroot", "-solid", "#1a1a2e"], env=env_n,
                                capture_output=True, timeout=2)
            except Exception:
                pass

        if applied_count == 0:
            log.warning(
                "Nenhuma configuração visual foi clonada do display principal. Verifique se "
                "dconf/gsettings/feh estão instalados e se o server tem acesso à sessão "
                "gráfica (DBUS_SESSION_BUS_ADDRESS, ver logs acima para o motivo exato)."
            )
        else:
            log.info("Aparência clonada do display principal: %d configuração(ões).", applied_count)

        return applied_wallpaper

    def _clone_display0_appearance_with_retry(self, max_attempts: int = 3, delay: float = 1.0) -> bool:
        """Roda `_clone_display0_appearance()` com novas tentativas se o
        papel de parede não for aplicado de primeira.

        Essa é a causa mais provável do "às vezes clona, às vezes não":
        logo após o terminal abrir, o WM e a sessão gráfica do display
        virtual podem ainda não estar 100% prontos no exato instante em que
        a clonagem roda pela primeira vez (timing race, não um erro fixo) —
        então tentar de novo 1-2 vezes com um intervalo curto resolve sem
        precisar de um sleep fixo mais longo (que só atrasaria a conexão
        sempre, mesmo quando não precisa).
        """
        for attempt in range(1, max_attempts + 1):
            try:
                if self._clone_display0_appearance():
                    return True
            except Exception as exc:
                log.warning("Tentativa %d de clonar aparência falhou: %s", attempt, exc)
            if attempt < max_attempts:
                time.sleep(delay)
        return False

    def _clone_display0_appearance_async(self):
        """Dispara a clonagem (com retry) em background — não atrasa o
        start()/resize() do display virtual, que é justamente o motivo pelo
        qual a clonagem automática tinha sido removida (ficava lenta/
        bloqueando a conexão)."""
        threading.Thread(
            target=self._clone_display0_appearance_with_retry, daemon=True
        ).start()

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

        # Método 0 (preferido): mss — captura em processo, sem abrir um
        # programa novo a cada frame. É a mesma biblioteca já usada no modo
        # Espelhar, só que apontando pro display virtual (:N) em vez do
        # display real. É de longe o método mais rápido e mais leve de CPU:
        # xwd/import abrem um processo do zero (fork+exec) a cada frame, o
        # que é caro (~5-20ms só de overhead) e pesa bastante em PCs mais
        # fracos quando repetido ~30x por segundo.
        if capture_fn is None and mss is not None:
            log.info("Testando captura via mss em %s...", display_name)
            fn = self._test_mss(display_name)
            if fn is not None:
                capture_fn = fn
                log.info("Captura via mss OK em %s (método rápido)", display_name)
            else:
                log.warning("mss falhou em %s, tentando outro método", display_name)

        # Método 1: xwd (X Window Dump) — só testa se mss não funcionou
        if capture_fn is None and shutil.which("xwd"):
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
            with self._capture_interval_lock:
                interval = self._capture_interval
            self._stop_event.wait(interval)

    def set_capture_interval(self, interval: float):
        """Ajusta o ritmo desta thread de captura em tempo real — chamado
        pelo server.py sempre que a qualidade de vídeo muda ou quando a
        tela fica parada por um tempo (throttling de tela parada), pra
        manter as duas capturas (esta e a do vídeo em si) na mesma
        velocidade em vez de uma rodar sempre no talo por trás da outra.
        """
        interval = max(1.0 / 60.0, min(2.0, interval))  # entre ~60fps e 0.5fps
        with self._capture_interval_lock:
            self._capture_interval = interval

    def _test_mss(self, display_name):
        """Testa captura via mss (biblioteca já usada no modo Espelhar).

        Conecta uma única vez ao display virtual e reaproveita a conexão
        a cada frame — nada de abrir processo novo. Retorna a função de
        captura rápida, ou None se não conseguir conectar/capturar.
        """
        try:
            sct = mss.mss(display=display_name)
        except Exception as exc:
            log.warning("mss: não conseguiu conectar em %s: %s", display_name, exc)
            return None

        try:
            monitor = sct.monitors[0] if sct.monitors else {
                "left": 0, "top": 0, "width": self.width, "height": self.height,
            }
            raw = sct.grab(monitor)
            img = np.array(raw)[:, :, :3]  # BGRA -> BGR
        except Exception as exc:
            log.warning("mss: captura de teste falhou em %s: %s", display_name, exc)
            try:
                sct.close()
            except Exception:
                pass
            return None

        if img is None or img.shape[0] < 10 or img.shape[1] < 10:
            log.warning("mss: frame de teste inválido em %s", display_name)
            try:
                sct.close()
            except Exception:
                pass
            return None

        log.info("mss OK — frame %dx%d", img.shape[1], img.shape[0])
        self._mss_instance = sct

        with self._frame_lock:
            if img.shape[:2] == (self.height, self.width):
                np.copyto(self._latest_frame, img)
            else:
                self._latest_frame = np.ascontiguousarray(img)
        self._new_frame_event.set()

        def capture_mss():
            try:
                raw = sct.grab(monitor)
                arr = np.array(raw)[:, :, :3]
                with self._frame_lock:
                    if arr.shape[:2] == (self.height, self.width):
                        np.copyto(self._latest_frame, arr)
                    else:
                        self._latest_frame = np.ascontiguousarray(arr)
                self._new_frame_event.set()
            except Exception:
                pass

        return capture_mss

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

        # Encerra o x2x — sem isso o cursor continuaria "vazando" pela
        # borda direita da tela tentando ir pra um display que não existe
        # mais assim que o Estender for desligado.
        self._stop_x2x()

        # Espera thread terminar
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=3)
        self._capture_thread = None

        # Idem pra thread de polling do mouse.

        # Fecha conexão mss (se estava sendo usada pra captura)
        if self._mss_instance is not None:
            try:
                self._mss_instance.close()
            except Exception:
                pass
            self._mss_instance = None

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

    def resize(self, new_width: int, new_height: int) -> bool:
        """Redimensiona o display virtual Xvfb.

        O Xvfb não suporta redimensionamento dinâmico nativamente, então
        este método para e recria o Xvfb com as novas dimensões, mantendo
        o mesmo número de display.

        Retorna True se sucesso, False caso contrário.
        """
        if not self._started or not self.display_name:
            return False

        old_display_name = self.display_name
        old_display_num = self.display_num
        new_width = max(320, min(new_width, 3840))
        new_height = max(240, min(new_height, 2160))

        log.info("Redimensionando Xvfb %s: %dx%d -> %dx%d", old_display_name, self.width, self.height, new_width, new_height)

        # Para captura
        if self._stop_event:
            self._stop_event.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=3)
        self._capture_thread = None

        # Fecha mss
        if self._mss_instance is not None:
            try:
                self._mss_instance.close()
            except Exception:
                pass
            self._mss_instance = None

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

        # Limpa arquivos de socket
        for f in (f"/tmp/.X{old_display_num}-lock", f"/tmp/.X11-unix/X{old_display_num}"):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass

        # Atualiza dimensões
        self.width = new_width
        self.height = new_height
        self._shape = (new_height, new_width, 3)
        self.display_name = old_display_name
        self.display_num = old_display_num

        # Recria o Xvfb
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
                log.error("Xvfb falhou ao recriar (rc=%d): %s", self.xvfb_process.returncode, stderr)
                self._started = False
                return False
        except Exception as exc:
            log.error("Erro ao recriar Xvfb: %s", exc)
            self._started = False
            return False

        # Cor de fundo
        env = {**os.environ, "DISPLAY": self.display_name}
        try:
            subprocess.run(
                ["xsetroot", "-solid", "#1a1a2e"],
                env=env, capture_output=True, timeout=3,
            )
        except Exception:
            pass

        # Reinicia WM
        for wm in ("openbox", "fluxbox", "twm", "fvwm"):
            if shutil.which(wm):
                try:
                    self.wm_process = subprocess.Popen(
                        [wm], env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self._wm_name = wm
                    break
                except Exception:
                    continue

        # Abre terminal inicial
        self._open_initial_terminal(env)

        # Reinicia o x2x (o display virtual foi recriado, então o x2x
        # antigo, se ainda estivesse rodando, estaria apontando pra um
        # display que não existe mais).
        self._start_x2x()

        # Reaplica aparência do display0 no Xvfb recém-recriado (rotação),
        # em segundo plano com retry — mesmo motivo do start() inicial. É
        # essencial aqui: o resize() recria o Xvfb do zero (root window em
        # branco de novo), então sem isso o papel de parede sempre some
        # depois de girar a tela.
        self._clone_display0_appearance_async()

        # Reinicia captura
        self._start_capture_thread()
        # Idem pro polling de mouse — a conexão X11 antiga não serve mais
        # (o Xvfb foi recriado do zero).

        # Espera estabilizar
        time.sleep(2.0)
        if self._new_frame_event and self._new_frame_event.is_set():
            self._capture_ok = True
        else:
            self._capture_ok = False
            log.warning("Captura pode não estar funcionando após resize.")

        self._started = True
        log.info("Xvfb redimensionado com sucesso: %s (%dx%d)", self.display_name, self.width, self.height)
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

    def get_mouse_position(self):
        """Retorna a última posição (x, y) que NÓS MESMOS mandamos o
        cursor do display virtual pra (ver send_input) — leitura
        instantânea, sem nenhuma consulta ao X11, então pode ser chamada
        a cada frame de vídeo sem custo nenhum. Começa no centro da tela
        (ver __init__) até o primeiro toque chegar, já que o Xvfb não
        tem mouse físico nenhum, então não há "posição inicial" real.
        """
        with self._mouse_pos_lock:
            return self._mouse_pos

    # ------------------------------------------------------------------
    # Input (toque do celular -> Xvfb via xdotool)
    # ------------------------------------------------------------------


    def send_input(self, action, x=0, y=0, key=None):
        """Envia clique/movimento/tecla pro display virtual."""
        if not self._started or not self.display_name:
            return
        if not shutil.which("xdotool"):
            return

        # Correção de bug (cursor "parado"/"não aparece" no modo
        # Estender): a posição que a gente MANDA o cursor ir é a mesma
        # que reportamos pro celular (ver get_mouse_position) — não tem
        # como "não bater", porque é a mesma fonte. Atualiza ANTES de
        # chamar o xdotool (não depois), pra já refletir o toque atual
        # mesmo que o subprocess demore um pouco pra responder.
        if action in ("mousemove", "click", "mousedown", "mouseup"):
            with self._mouse_pos_lock:
                self._mouse_pos = (int(x), int(y))

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
                # Move pro ponto do toque ANTES de apertar o botão — sem isso
                # o clique inicial de um arrastar/rolar acontecia onde o mouse
                # já estava (posição de um toque anterior), não onde o dedo
                # realmente tocou a tela.
                subprocess.run(
                    ["xdotool", "mousemove", "--", str(int(x)), str(int(y)),
                     "mousedown", "1"],
                    env=env, capture_output=True, timeout=1,
                )
            elif action == "mouseup":
                subprocess.run(
                    ["xdotool", "mousemove", "--", str(int(x)), str(int(y)),
                     "mouseup", "1"],
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

        # Para 32-bit: canais são [pad/alpha, R, G, B] (LSBFirst) ou
        # [pad/alpha, R, G, B] (MSBFirst com BGRX).
        # O canal 0 costuma ser padding/alpha, os dados de cor estão em 1:4.
        if actual_bpp == 4:
            img = img[:, :, 1:4].copy()
        else:
            img = img[:, :, :3].copy()

        # Detecta ordem RGB vs BGR pelos masks.
        # Xvfb em x86 (LSBFirst): bytes = [B, G, R] → já é BGR, sem flip.
        # MSBFirst com masks típicos (R=0xFF0000, B=0x0000FF):
        #   bytes = [R, G, B] → precisa flip para BGR.
        # Quando red_mask > blue_mask (R em bits altos, B em bits baixos)
        # e byte_order é MSBFirst, os dados estão em RGB.
        if red_mask > 0 and blue_mask > 0 and red_mask > blue_mask:
            # Lê byte_order do header (offset 28, CARD32)
            byte_order = struct.unpack(">I", data[28:32])[0]
            if byte_order == 0:  # MSBFirst = big-endian
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
