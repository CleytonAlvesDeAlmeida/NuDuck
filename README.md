# NuDuck

NuDuck transforma um celular Android num monitor do PC Linux, direto pela
rede local (Wi-Fi, USB ou QR Code) usando WebRTC. Sem nuvem, sem conta,
sem cadastro — só um PIN de 6 dígitos exibido na tela do PC.

Dois modos de uso:
- **Espelhar** — o celular repete a tela do PC.
- **Estender** — o celular vira uma segunda tela de verdade (usando Xvfb,
  igual ao SpaceDesk). Funciona em qualquer hardware.

## Estrutura do projeto

```
NuDuck/
├── server/
│   ├── server.py            # Servidor principal (roda no PC)
│   ├── virtual_display.py   # Display virtual Xvfb (modo Estender)
│   └── requirements.txt     # Dependências Python
└── android/                 # App Android (Kotlin + Compose)
```

## Como instalar no PC (Linux)

### 1. Dependências do sistema

```bash
# Debian/Ubuntu
sudo apt install python3-tk xvfb xdotool openbox x11-xserver-utils xsetroot

# Fedora
sudo dnf install python3-tkinter xorg-x11-server-Xvfb xdotool openbox xorg-x11-server-utils xsetroot
```

> O que cada pacote faz:
> - `python3-tk` — janela com o PIN e QR Code
> - `xvfb` — display virtual (modo Estender)
> - `xdotool` — envia cliques pro display virtual
> - `openbox` — window manager pro display virtual
> - `xsetroot` — cor de fundo do desktop virtual
> - `x11-xserver-utils` — ferramentas X11

### 2. Dependências Python

```bash
cd server/
pip install -r requirements.txt
```

### 3. Rodar o servidor

```bash
python3 server.py
```

Abre uma janela com o PIN, QR Code e checkbox "Permitir controle".

## Como rodar no Android

1. Abra `android/` no Android Studio.
2. Deixe o Gradle sincronizar.
3. Rode no celular (Android 7.0+).

### Conexão Wi-Fi
Celular e PC na mesma rede. O app descobre o PC automaticamente (mDNS).

### Conexão USB
Plugue o cabo com depuração USB ativa. O servidor faz o `adb reverse`
sozinho — só toque em "Via cabo (USB)" no app.

## Modo Estender (segunda tela — igual SpaceDesk)

Quando o celular escolhe "Estender", o servidor cria um display virtual
usando **Xvfb** (X Virtual Framebuffer). Isso funciona em **qualquer
hardware** porque é software puro — não depende de GPU nem de xrandr.

O celular vira um segundo desktop interativo. Você pode abrir apps nele:

```bash
DISPLAY=:1 firefox &
DISPLAY=:1 xterm &
```

Toques no celular viram cliques no display virtual (via xdotool).

## Protocolo de sinalização (WebSocket, porta 8765)

```
Celular -> PC   {"type":"pin","pin":"123456"}
PC -> Celular   {"type":"pin_ok"}  ou  {"type":"pin_error"}

Celular -> PC   {"type":"offer","sdp":"...","sdpType":"offer","quality":"480p","mode":"mirror"}
PC -> Celular   {"type":"answer","sdp":"...","sdpType":"answer","mode":"mirror"}

Celular -> PC   {"type":"quality","value":"720p"}
Celular -> PC   {"type":"tap","x":0.5,"y":0.5}
Celular -> PC   {"type":"key","key":"enter"}
```

## Segurança

- Sem PIN correto, nada funciona.
- 5 tentativas erradas = bloqueio de 60s.
- Só aceita IPs da rede local (192.168.x, 10.x, 172.16-31.x, localhost).
- WebRTC com criptografia DTLS/SRTP obrigatória.
- Controle remoto só funciona se o checkbox estiver marcado no PC.
- Nada sai da rede local — sem nuvem, sem telemetria.
