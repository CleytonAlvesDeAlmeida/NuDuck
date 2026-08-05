"""MIT-SHM (X Shared Memory) para captura de janelas via X Composite.

Implementação manual dos requests MIT-SHM usando `Xlib.protocol.rq`, porque
o `python-xlib` não tem suporte nativo a essa extensão (ver
https://github.com/python-xlib/python-xlib — não existe `Xlib/ext/shm.py`).

Por que MIT-SHM?
----------------
Sem MIT-SHM, cada captura de janela copia os bytes do pixmap pelo socket
X11 — para uma janela 1920×1080 a 32 bits, são ~8 MB por frame que atravessam
o socket. Com MIT-SHM, o servidor X escreve os pixels DIRETO num segmento
de memória compartilhada (shmget/shmat do System V IPC), e o Python lê esse
segmento — zero bytes de pixels pelo socket.

Ganho típico:
  - 800×600:   ~3ms → ~1ms  (economia ~2ms/frame)
  - 1920×1080: ~8ms → ~3ms  (economia ~5ms/frame)
  - 4K:        ~25ms → ~10ms (economia ~15ms)

Fluxo:
  1. Verifica se a extensão MIT-SHM está presente no servidor X.
  2. Cria um segmento shm (System V) do tamanho da janela.
  3. Anexa o segmento ao servidor X (request ShmAttach).
  4. Para cada frame: chama ShmGetImage — o X server escreve os pixels
     no segmento shm; o Python lê via ctypes (shmat).
  5. Ao trocar de janela ou fechar: detacha o segmento e destrói o shm.

Limitações:
  - Só funciona no mesmo host (socket UNIX local). O NuDuck já roda só
    no localhost, então não é problema.
  - Se a janela mudar de tamanho, precisa realocar o segmento shm.
  - Requer permissão de criar segmentos shm (geralmente default).
"""

import ctypes
import ctypes.util
import logging

from Xlib.protocol import rq
from Xlib.xobject.drawable import Drawable

log = logging.getLogger(__name__)

# ============================================================================
# libc (SysV IPC: shmget/shmat/shmdt/shmctl)
# ============================================================================

_libc = ctypes.CDLL(ctypes.util.find_library("c"))
_IPC_CREAT = 0o1000
_IPC_RMID = 0x0100

_libc.shmget.restype = ctypes.c_int
_libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]

_libc.shmat.restype = ctypes.c_void_p
_libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]

_libc.shmdt.restype = ctypes.c_int
_libc.shmdt.argtypes = [ctypes.c_void_p]

_libc.shmctl.restype = ctypes.c_int
_libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]


# ============================================================================
# Estrutura XShmSegmentInfo (corresponde à struct C do mesmo nome em Xlib)
# ============================================================================

class XShmSegmentInfo(ctypes.Structure):
    _fields_ = [
        ("shmid", ctypes.c_int),
        ("shmaddr", ctypes.c_void_p),
        ("readOnly", ctypes.c_bool),
    ]


# ============================================================================
# Requests MIT-SHM (implementação manual via Xlib.protocol.rq)
# ============================================================================
# Opcodes dos requests MIT-SHM, conforme especificação oficial:
#   https://www.x.org/releases/X11R7.7/doc/xextproto/shm.html
#   0 = ShmQueryVersion
#   1 = ShmAttach
#   2 = ShmDetach
#   3 = ShmPutImage
#   4 = ShmGetImage
#   5 = ShmCreatePixmap

_EXT_NAME = "MIT-SHM"


class ShmQueryVersion(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8("opcode"),
        rq.Opcode(0),
        rq.RequestLength(),
    )
    _reply = rq.Struct(
        rq.ReplyCode(),
        rq.Pad(1),
        rq.Card16("sequence_number"),
        rq.ReplyLength(),
        rq.Card16("major_version"),
        rq.Card16("minor_version"),
        rq.Card16("uid"),
        rq.Card16("gid"),
        rq.Pad(8),
        rq.Bool("shared_pixmaps"),
        rq.Pad(15),
    )


class ShmAttach(rq.Request):
    _request = rq.Struct(
        rq.Card8("opcode"),
        rq.Opcode(1),
        rq.RequestLength(),
        rq.Card32("shmseg"),
        rq.Card32("shmid"),
        rq.Bool("readOnly"),
        rq.Pad(3),
    )


class ShmDetach(rq.Request):
    _request = rq.Struct(
        rq.Card8("opcode"),
        rq.Opcode(2),
        rq.RequestLength(),
        rq.Card32("shmseg"),
    )


class ShmGetImage(rq.ReplyRequest):
    """Copia o conteúdo de um Drawable para o segmento shm anexado.

    O servidor X escreve os pixels (formato ZPixmap, depth do drawable)
    diretamente no endereço `shmaddr` do segmento shm. O Python lê esses
    bytes via ctypes depois do reply voltar.

    Reply fields (após o header padrão):
      - depth (Card8): profundidade real da imagem capturada
      - visual (Card32): XID do visual usado
      - Pad(20): padding padrão para alinhar o reply a 32 bytes
    """
    _request = rq.Struct(
        rq.Card8("opcode"),
        rq.Opcode(4),
        rq.RequestLength(),
        rq.Drawable("drawable"),
        rq.Card32("shmseg"),
        rq.Card32("offset"),
        rq.Card16("x"),
        rq.Card16("y"),
        rq.Card16("width"),
        rq.Card16("height"),
        rq.Card32("plane_mask"),
        rq.Card8("format"),  # 2 = ZPixmap
        rq.Pad(3),
    )
    _reply = rq.Struct(
        rq.ReplyCode(),
        rq.Card8("depth"),
        rq.Card16("sequence_number"),
        rq.ReplyLength(),
        rq.Card32("visual"),
        rq.Pad(20),
    )


# ============================================================================
# Helper: descobrir o major opcode da extensão MIT-SHM
# ============================================================================

def _get_shm_opcode(display) -> int:
    """Descobre o major opcode da extensão MIT-SHM via QueryExtension.

    Retorna o opcode (int) ou 0 se a extensão não estiver presente.
    O `python-xlib` não registra MIT-SHM na lista `__extensions__`, mas o
    método `display.query_extension(name)` consulta o servidor X
    diretamente — funciona mesmo para extensões não suportadas nativamente.
    """
    try:
        info = display.query_extension(_EXT_NAME)
        if info and info.present:
            return info.major_opcode
    except Exception as exc:
        log.debug("query_extension('MIT-SHM') falhou: %s", exc)
    return 0


# ============================================================================
# Classe principal: XShmCapturer
# ============================================================================

class XShmCapturer:
    """Gerencia um segmento shm anexado ao X server para captura de janelas.

    Uso típico:
        capturer = XShmCapturer(display)
        if capturer.available:
            pixels_bgra = capturer.capture(pixmap_id, width, height)
            if pixels_bgra is not None:
                # converter para BGR (descartar alpha) e seguir o pipeline
        capturer.close()

    O segmento shm é criado sob demanda no tamanho da janela. Se a janela
    mudar de tamanho, o segmento é realocado automaticamente no próximo
    `capture()`.
    """

    def __init__(self, display):
        self._display = display
        self._opcode = _get_shm_opcode(display)
        self._available = bool(self._opcode)
        # Estado do segmento shm atual (criado sob demanda)
        self._shmid = -1
        self._shmaddr = None  # endereço retornado por shmat
        self._shmseg_xid = 0  # XID do shmseg no servidor X
        self._attached = False
        self._seg_size = 0  # tamanho atual do segmento (bytes)
        self._seg_width = 0
        self._seg_height = 0

    @property
    def available(self) -> bool:
        """True se a extensão MIT-SHM está disponível no servidor X."""
        return self._available

    def _ensure_segment(self, width: int, height: int) -> bool:
        """Cria (ou recria) o segmento shm com o tamanho necessário.

        Se o tamanho da janela mudou desde o último `_ensure_segment`,
        destrói o segmento antigo e cria um novo. Mantém o mesmo
        segmento se o tamanho bate (evita realocar a cada frame).
        """
        size = width * height * 4  # BGRA sempre 4 bytes/pixel
        if size <= 0:
            return False

        # Segmento atual ainda serve?
        if (self._attached and self._seg_size == size
                and self._seg_width == width and self._seg_height == height):
            return True

        # Precisa realocar — limpa o antigo primeiro
        self._detach_segment()

        # Cria segmento System V shm (IPC_PRIVATE = 0, key gerada pelo kernel)
        shmid = _libc.shmget(0, size, _IPC_CREAT | 0o777)
        if shmid == -1 or shmid < 0:
            log.debug("shmget(%d bytes) falhou (errno via ctypes)", size)
            return False

        addr = _libc.shmat(shmid, None, 0)
        if addr in (-1, None) or addr == ctypes.c_void_p(-1).value:
            # shmat falhou — remove o segmento
            _libc.shmctl(shmid, _IPC_RMID, None)
            return False

        # Aloca um XID para o shmseg
        try:
            shmseg_xid = self._display.allocate_resource_id()
        except Exception:
            _libc.shmdt(addr)
            _libc.shmctl(shmid, _IPC_RMID, None)
            return False

        # Anexa o segmento ao servidor X
        try:
            ShmAttach(
                display=self._display,
                opcode=self._opcode,
                shmseg=shmseg_xid,
                shmid=shmid,
                readOnly=False,
            )
        except Exception as exc:
            log.debug("ShmAttach falhou: %s", exc)
            self._display.free_resource_id(shmseg_xid)
            _libc.shmdt(addr)
            _libc.shmctl(shmid, _IPC_RMID, None)
            return False

        self._shmid = shmid
        self._shmaddr = addr
        self._shmseg_xid = shmseg_xid
        self._attached = True
        self._seg_size = size
        self._seg_width = width
        self._seg_height = height
        return True

    def _detach_segment(self):
        """Detacha e destrói o segmento shm atual (se houver)."""
        if self._attached and self._shmseg_xid:
            try:
                ShmDetach(
                    display=self._display,
                    opcode=self._opcode,
                    shmseg=self._shmseg_xid,
                )
            except Exception:
                pass
            try:
                self._display.free_resource_id(self._shmseg_xid)
            except Exception:
                pass

        if self._shmaddr:
            try:
                _libc.shmdt(self._shmaddr)
            except Exception:
                pass

        if self._shmid >= 0:
            try:
                _libc.shmctl(self._shmid, _IPC_RMID, None)
            except Exception:
                pass

        self._shmid = -1
        self._shmaddr = None
        self._shmseg_xid = 0
        self._attached = False
        self._seg_size = 0
        self._seg_width = 0
        self._seg_height = 0

    def capture(self, drawable_id: int, width: int, height: int):
        """Captura o conteúdo do drawable (pixmap) via MIT-SHM.

        Retorna:
          - numpy.ndarray (H, W, 3) BGR se sucesso
          - None se falhou (extensão indisponível, segmento não criado, etc.)

        O caller deve tratar None como "use o fallback get_image".
        """
        if not self._available:
            return None

        if not self._ensure_segment(width, height):
            return None

        try:
            reply = ShmGetImage(
                display=self._display,
                opcode=self._opcode,
                drawable=drawable_id,
                shmseg=self._shmseg_xid,
                offset=0,
                x=0, y=0,
                width=width, height=height,
                plane_mask=0xFFFFFFFF,
                format=2,  # ZPixmap
            )
        except Exception as exc:
            log.debug("ShmGetImage falhou: %s", exc)
            return None

        if reply is None:
            return None

        depth = reply.depth
        if depth not in (24, 32):
            log.debug("ShmGetImage: profundidade incomum (%s bits)", depth)
            return None

        # Lê os pixels do segmento shm (BGRA, 4 bytes/pixel).
        import numpy as np
        try:
            buf = ctypes.string_at(self._shmaddr, self._seg_size)
        except Exception as exc:
            log.debug("Leitura do shm falhou: %s", exc)
            return None

        arr = np.frombuffer(buf, dtype=np.uint8)
        expected = width * height * 4
        if arr.size < expected:
            return None
        arr = arr[:expected].reshape(height, width, 4)
        # BGR (descarta o 4º byte — alpha/buffer)
        return np.ascontiguousarray(arr[:, :, :3])

    def close(self):
        """Libera todos os recursos (chame ao trocar de janela ou fechar)."""
        self._detach_segment()
