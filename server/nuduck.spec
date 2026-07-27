# -*- mode: python ; coding: utf-8 -*-
#
# Spec do PyInstaller para o NuDuck Server.
#
# IMPORTANTE: PyInstaller NÃO faz cross-compile. Rodando este spec no Linux
# gera um binário Linux; rodando no Windows gera um .exe do Windows. Para
# gerar os dois, use o workflow em .github/workflows/build-desktop.yml
# (roda em runners Linux e Windows separados).
#
# Uso manual:
#   pip install pyinstaller
#   pyinstaller nuduck.spec --clean --noconfirm

from PyInstaller.utils.hooks import collect_all

datas = [("assets/icon.png", "assets")]
binaries = []
hiddenimports = []

# aiortc/av/zeroconf/cryptography têm dados e libs nativas que o PyInstaller
# não detecta sozinho por análise estática — collect_all pega tudo.
for pkg in ("aiortc", "av", "zeroconf", "cryptography", "pylibsrtp", "pyee"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# pyautogui e suas dependências transitivas (nomes variam entre plataformas).
hiddenimports += [
    "pyautogui",
    "pymsgbox",
    "pytweening",
    "pyscreeze",
    "pygetwindow",
    "mouseinfo",
    "pyrect",
    "PIL",
    "PIL._tkinter_finder",
    "PIL.ImageTk",
    "tkinter",
    "Xlib",  # necessário no Linux (X11); ignorado silenciosamente no Windows
    "qrcode",
    "qrcode.image.pil",
]

block_cipher = None

a = Analysis(
    ["server.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="NuDuck-Server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=True: mantém uma janela de terminal aberta mostrando os logs
    # (além da janela Tkinter com o PIN). Evita problemas de stdout/stderr
    # ausentes em builds "windowed" no Windows.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)
