# NuDuck

NuDuck transforma um celular Android num monitor do PC Linux, direto pela
rede local (Wi-Fi, USB ou QR Code) usando WebRTC. Sem nuvem, sem conta,
sem cadastro — só um PIN de 6 dígitos exibido na tela do PC.

<<<<<<< HEAD
Dois modos de uso:
- **Espelhar** — o celular repete a tela do PC.
- **Estender** — o celular vira uma segunda tela de verdade (usando Xvfb,
  igual ao SpaceDesk). Funciona em qualquer hardware.
=======
Três modos de uso:

- **Espelhar** — o celular repete a tela do PC. A região central do
  monitor é recortada automaticamente para casar com a proporção da tela
  do celular, eliminando as barras pretas laterais em aparelhos "compridos".
- **Estender** — o celular vira uma segunda tela de verdade (usando Xvfb,
  igual ao SpaceDesk). Funciona em qualquer hardware, sem depender de GPU
  nem de xrandr.
- **Espelhar Janela** — espelha apenas uma janela específica selecionada
  na janela do servidor, em vez da tela inteira. Útil para acompanhar uma
  aplicação (terminal, player, editor) sem mostrar o resto do desktop.

O QR Code de conexão é **criptografado** (AES-256-GCM, token de 30s): um
leitor externo de QR (Google Lens, app de câmera) só vê uma runa
`ND1.<base64>.<base64>` ilegível — só o app NuDuck, que reenvia o token
ao servidor, consegue validar a conexão.
>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303

## Estrutura do projeto

```
NuDuck/
<<<<<<< HEAD
├── server/
│   ├── server.py            # Servidor principal (roda no PC)
│   ├── virtual_display.py   # Display virtual Xvfb (modo Estender)
│   ├── shortcuts.json       # Atalhos personalizados (modo Estender)
│   └── requirements.txt     # Dependências Python
└── android/                 # App Android (Kotlin + Compose)
=======
├── server/                         # Servidor Python (roda no PC)
│   ├── server.py                   # Servidor principal (aiohttp + aiortc + Tkinter)
│   ├── virtual_display.py          # Display virtual Xvfb (modo Estender)
│   ├── shortcuts.json              # Atalhos personalizados (gerenciado pelo server)
│   ├── requirements.txt            # Dependências Python
│   ├── nuduck.spec                 # Spec PyInstaller (build do binário desktop)
│   └── assets/
│       ├── icon.png                # Ícone PNG (usado no QR Code e na janela)
│       └── icon.ico                # Ícone ICO (embutido no .exe Windows)
├── android/                        # App Android (Kotlin + Jetpack Compose)
│   ├── app/
│   │   ├── build.gradle.kts        # Android 7.0+ (minSdk 24), targetSdk 34, Java 17
│   │   └── src/main/
│   │       ├── AndroidManifest.xml
│   │       ├── res/                # Strings (pt + en), tema, ícone, logo vetorial
│   │       └── java/com/droidmonitor/
│   │           ├── MainActivity.kt          # UI Compose (Discovery, PIN, Connected)
│   │           ├── MainViewModel.kt         # Estado, fluxo de telas, USB + QR
│   │           ├── DroidMonitorApplication.kt
│   │           ├── BootReceiver.kt          # "Iniciar ao ligar" (opcional)
│   │           ├── discovery/               # mDNS (JmDNS) + PcInfo
│   │           ├── settings/                # AppSettings, LocaleHelper, tema/idioma
│   │           ├── ui/                      # FloatingMenu, Immersive, QrScan, Settings, Theme
│   │           ├── usb/                     # UsbConnectionMonitor (Ancoragem USB)
│   │           └── webrtc/                  # WebRtcClient, SignalingClient, ControlEvents,
│   │                                        #   SharpUpscaleDrawer (AMD CAS), RemoteLog
│   ├── build.gradle.kts
│   ├── settings.gradle.kts
│   └── gradle.properties
├── .github/workflows/
│   └── build-desktop.yml           # GitHub Actions: build PyInstaller (Linux + Windows)
├── codemagic.yaml                  # Codemagic: APK Android + validação do server Python
├── MELHORIAS.md                    # Histórico detalhado das últimas 9 melhorias
├── README.md                       # Este arquivo
└── .gitignore
>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303
```

## Como instalar no PC (Linux)

### 1. Dependências do sistema

```bash
# Debian/Ubuntu
sudo apt install python3-tk xvfb xdotool openbox x11-xserver-utils x11-apps xterm x2x feh dconf-cli

# Fedora
sudo dnf install python3-tkinter xorg-x11-server-Xvfb xdotool openbox xorg-x11-server-utils xorg-x11-apps xterm
```

> ⚠️ **Atenção:** versões antigas deste README listavam um pacote chamado `xsetroot`,
> que **não existe** no apt/dnf (o comando `xsetroot` vem de dentro do pacote
> `x11-xserver-utils`, que já está na lista acima). Colocar um nome de pacote
> inválido no meio do comando faz o `apt`/`dnf` **cancelar a instalação inteira**,
> ou seja, nem o Xvfb era instalado — e por isso o modo Estender caía sempre
> no aviso "Xvfb não encontrado". Use o comando corrigido acima.

> O que cada pacote faz:
> - `python3-tk` — janela com o PIN e QR Code
> - `xvfb` — display virtual (modo Estender)
<<<<<<< HEAD
> - `xdotool` — envia cliques pro display virtual
=======
> - `xdotool` — envia cliques pro display virtual e lista janelas (modo Espelhar Janela)
>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303
> - `openbox` — window manager pro display virtual
> - `x11-xserver-utils` — fornece o `xsetroot` (cor de fundo do desktop virtual)
> - `x11-apps` — fornece o `xwd` (captura de tela de reserva, caso a captura rápida falhe)
> - `xterm` — terminal que abre automaticamente na tela virtual
<<<<<<< HEAD
=======
> - `feh` — papel de parede do display virtual (opcional; o server pinta sozinho se faltar)
> - `dconf-cli` — usado para clonar tema/ícone/cursor do GNOME pro display virtual
>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303

### 2. Dependências Python

```bash
cd server/
pip install -r requirements.txt
```

<<<<<<< HEAD
=======
Dependências (`requirements.txt`):

- `aiohttp` — servidor HTTP + WebSocket
- `aiortc` — implementação WebRTC (Python)
- `av`, `opencv-python-headless`, `mss`, `numpy` — captura e processamento de frames
- `pyautogui` — controle do mouse/teclado no modo Espelhar
- `zeroconf` — anúncio mDNS na rede local
- `qrcode[pil]` — geração do QR Code na janela Tk
- `python-xlib` — leitura do cursor real via XFixes (X11)
- `cryptography>=42` — **criptografia AES-256-GCM do token QR** (Item 3 do `MELHORIAS.md`)
- `PyJWT>=2.8` — fallback de token JWT (compatibilidade)

>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303
### 3. Rodar o servidor

```bash
python3 server.py
```

<<<<<<< HEAD
Abre uma janela com o PIN, QR Code e checkbox "Permitir controle".

## Como rodar no Android

1. Abra `android/` no Android Studio.
2. Deixe o Gradle sincronizar.
3. Rode no celular (Android 7.0+).

### Conexão Wi-Fi
Celular e PC na mesma rede. O app descobre o PC automaticamente (mDNS).

### Conexão USB (Ancoragem USB — recomendada para menos lag)
Plugue o cabo e ative **Ancoragem USB** nas configurações do celular
(Configurações → Rede e Internet → Ponto de acesso e ancoragem →
Ancoragem USB). O PC não precisa fazer nada — o próprio celular acha o
servidor automaticamente assim que a ancoragem liga. Depois é só tocar em
"Via cabo (USB)" no app.
=======
Abre uma janela com o PIN, QR Code e checkbox **"Permitir controle"**.
A janela tem abas para gerenciar atalhos personalizados (adicionar/remover)
e listar janelas abertas para o modo Espelhar Janela.

> **Alternativa — binário único:** quem preferir não instalar Python, pode
> baixar o binário pré-compilado (`NuDuck-Server` no Linux, `NuDuck-Server.exe`
> no Windows) gerado pelo GitHub Actions em cada push pra `main`. Ver seção
> *Build do desktop (PyInstaller)* abaixo. O binário persiste os atalhos
> em `~/.config/NuDuck/` (Linux), `%APPDATA%\NuDuck\` (Windows) ou
> `~/Library/Application Support/NuDuck/` (macOS), não na pasta temporária
> do PyInstaller.

## Como rodar no Android

1. Abra `android/` no Android Studio (Java 17 + Android SDK 34).
2. Deixe o Gradle sincronizar (o wrapper é gerado automaticamente se faltar).
3. Rode no celular — **Android 7.0+** (`minSdk = 24`).

### Três formas de conectar

#### Wi-Fi (mDNS)
Celular e PC na mesma rede. O app descobre o PC automaticamente — não
precisa digitar IP. Toque no PC listado e digite o PIN.

#### Cabo USB (Ancoragem USB — recomendado para menos lag)
Plugue o cabo e ative **Ancoragem USB** nas configurações do celular
(Configurações → Rede e Internet → Ponto de acesso e ancoragem →
Ancoragem USB). O celular varre a sub-rede criada pela ancoragem
(`rndis0`/`usb0`/`ncm0`) procurando o servidor na porta 8765, e quando
acha, mostra "Via cabo (USB)" na tela inicial do app.
>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303

> **Por que Ancoragem USB e não Depuração USB?** A Ancoragem cria uma
> interface de rede IP de verdade sobre o cabo (como se fosse um Wi-Fi a
> mais, só que via USB), então o vídeo consegue usar UDP nativamente —
> bem menos lag do que tunelar tudo por dentro do protocolo do `adb`
> (que só suporta TCP e adiciona uma camada extra de overhead).

<<<<<<< HEAD
=======
> **Bônus:** quando a conexão vem via cabo, o app ativa o **perfil
> `low_latency`** automaticamente — H264 em vez de VP8, trickle ICE (envia
> candidatos ICE assim que ficam prontos em vez de esperar o gathering
> completo), `MAXBUNDLE` (uma única porta ICE), `INTER_NEAREST` no
> redimensionamento, e pula o desenho do cursor no frame de vídeo.
> Resultado: ~1s a menos no setup e latência visivelmente menor.

#### QR Code (criptografado)
Escaneie o QR Code exibido na janela do servidor com a câmera do celular,
direto pela tela "QR Code" do app. O token no QR é **opaco** — um leitor
externo só vê `ND1.<runa_base64>.<runa_base64>`. O app extrai host:port
da parte pública (base64url) e envia o token cifrado pro servidor, que
decripta com uma chave AES-256 persistente em
`~/.config/NuDuck/qr_secret.key`, valida expiração (30s) e PIN, e responde
`pin_ok` ou `qr_token_error`. Ver seção *QR Code criptografado* abaixo.

> **Combinação cabo + QR:** se a Ancoragem USB estiver ativa quando o
> usuário escaneia o QR, o app **ignora o IP do QR** (que é o da rede
> Wi-Fi) e força a conexão pelo cabo — usando o host encontrado na
> varredura USB e ativando o perfil `low_latency`. Mensagem exibida:
> "Conectando via cabo (USB)".

>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303
### Solução de problemas — "Via cabo" não conecta

1. Confirme que a **Ancoragem USB** está ativa nas configurações de rede
   do celular (não é a mesma opção que "Depuração USB"/"Opções do
   desenvolvedor" — é em Rede e Internet → Ponto de acesso e ancoragem).
2. Tente outro cabo/porta USB — alguns cabos são só de carregamento, sem
   dados, e a Ancoragem USB não aparece como opção nesse caso.
3. Se o app mostrar "PC não encontrado no cabo", espere alguns segundos
   (o celular varre a rede criada pela ancoragem procurando o servidor) e
   tente de novo em "Via cabo (USB)".
4. Se nada disso resolver, desligue e ligue a Ancoragem USB de novo nas
   configurações do celular, e confira se o Firewall do PC não está
   bloqueando a porta 8765 na interface USB (`usb0`/`enp0s...` — o nome
   varia por distro).

## Modo Estender (segunda tela — igual SpaceDesk)

Quando o celular escolhe "Estender", o servidor cria um display virtual
usando **Xvfb** (X Virtual Framebuffer). Isso funciona em **qualquer
hardware** porque é software puro — não depende de GPU nem de xrandr.

O celular vira um segundo desktop interativo. Você pode abrir apps nele:

```bash
DISPLAY=:1 firefox &
DISPLAY=:1 xterm &
```

<<<<<<< HEAD
Toques no celular viram cliques no display virtual (via xdotool).

### Menu flutuante (dentro da transmissão)

Enquanto a tela do PC é exibida no celular, um menu flutuante oferece
controle rápido sem precisar desconectar:

#### Botão "Modo"

Abre um submenu com duas opções que alteram o modo de exibição
**instantaneamente**, sem precisar voltar à tela inicial do app:

- **Espelhar** — o celular passa a repetir a tela do PC (tela :0).
- **Estender** — o celular vira a segunda tela virtual (tela :1).

A troca é feita via sinalização WebSocket — o servidor reconfigura
a fonte de vídeo e o display virtual automaticamente.

#### Botão "Atalhos" (modo Estender)

Abre um submenu com atalhos personalizados definidos no servidor.
Cada atalho tem um **nome** visível (ex: "Abrir Firefox") e um **comando**
associado que é executado no display virtual (`DISPLAY=:1`).

O celular mostra apenas o **nome**; ao tocar, o comando é enviado ao
servidor e executado na tela virtual. Exemplo de uso:

```json
// server/shortcuts.json
=======
Toques no celular viram cliques no display virtual (via `xdotool`).

### Clonagem automática da aparência do display principal

Quando o Xvfb sobe, o servidor copia a aparência do monitor principal
(display `:0`) para o display virtual (`:N`) automaticamente:

- **Tema GTK, ícones, fonte e variante de cor** — lidos via
  `gsettings get org.gnome.desktop.interface <key>` (GNOME) ou
  `xfconf-query` (XFCE) e aplicados no display virtual.
- **Cursor do mouse** — mesma configuração do display principal.
- **Papel de parede** — via `feh` quando disponível; senão, o servidor
  pinta direto no root window via Xlib + Pillow (bibliotecas que já são
  dependência), sem precisar instalar mais nada.

Cada passo é independente e falha silenciosamente — o start do Xvfb não
é bloqueado se um ambiente não tem `gsettings` (caso do LXQt puro).

### Atalhos personalizados (menu flutuante)

No modo Estender, o menu flutuante no celular tem um botão **"Atalhos"**
que lista os atalhos configurados no servidor. Cada atalho tem um **nome**
(ex: "Abrir Firefox") visível no celular e um **comando** executado no
display virtual (`DISPLAY=:1`) ao tocar.

**Atalhos padrão (criados automaticamente na primeira execução):**

| Nome         | Comando                                  |
|--------------|------------------------------------------|
| Configuração | `DISPLAY=:1 gnome-control-center &`      |
| Alt+F4       | `DISPLAY=:1 xdotool key Alt+F4 &`        |
| Multitarefa  | `DISPLAY=:1 xdotool key Super &`         |

> Atalhos criados ou removidos pelo usuário **não são sobrescritos** —
> o seed só roda quando o arquivo `shortcuts.json` não existe ou está vazio.

Você pode editar atalhos de três formas:

1. **Pela janela Tk do servidor** — aba "Atalhos", com botões
   adicionar/remover.
2. **Editando o arquivo** `server/shortcuts.json` (ou
   `~/.config/NuDuck/shortcuts.json` no binário) — o servidor lê as
   alterações automaticamente na próxima vez que o app pede a lista.
3. **Pela REST API** — `POST /shortcuts` para adicionar/atualizar,
   `DELETE /shortcuts?name=...` para remover (ver seção *REST API* abaixo).

Exemplo de `shortcuts.json`:

```json
>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303
{
  "shortcuts": [
    {"name": "Abrir Firefox", "command": "DISPLAY=:1 firefox &"},
    {"name": "Terminal", "command": "DISPLAY=:1 xterm &"},
    {"name": "Navegador de arquivos", "command": "DISPLAY=:1 pcmanfm &"}
  ]
}
```

<<<<<<< HEAD
Edite o arquivo `server/shortcuts.json` para criar seus próprios atalhos.
As alterações são lidas automaticamente pelo servidor — não precisa
reiniciar.

### Rotação automática de resolução (modo Estender)

Quando o celular é rotacionado (retrato ↔ paisagem), o servidor
detecta a mudança de orientação e **reinicia o Xvfb automaticamente**
com a resolução correspondente ao novo formato. Isso significa que
o desktop virtual se adapta à tela do celular sem intervenção manual.

Resoluções padrão:
- **Paisagem:** largura > altura (ex: 1280×720)
- **Retrato:** altura > largura (ex: 720×1280)

O ajuste é feito via mensagem de sinalização WebSocket — o celular
envia a nova orientação e o servidor reconfigura o display virtual
em tempo real.

## Protocolo de sinalização (WebSocket, porta 8765)

```
Celular -> PC   {"type":"pin","pin":"123456"}
PC -> Celular   {"type":"pin_ok"}  ou  {"type":"pin_error"}

Celular -> PC   {"type":"offer","sdp":"...","sdpType":"offer","quality":"480p","mode":"mirror"}
PC -> Celular   {"type":"answer","sdp":"...","sdpType":"answer","mode":"mirror"}

Celular -> PC   {"type":"quality","value":"720p"}
Celular -> PC   {"type":"tap","x":0.5,"y":0.5}
Celular -> PC   {"type":"key","key":"enter"}

# Novas mensagens (menu flutuante e rotação)
Celular -> PC   {"type":"switch_mode","mode":"mirror"}  ou  {"type":"switch_mode","mode":"extend"}
Celular -> PC   {"type":"run_shortcut","command":"DISPLAY=:1 firefox &"}
Celular -> PC   {"type":"rotation","orientation":"portrait"}  ou  {"type":"rotation","orientation":"landscape"}
PC -> Celular   {"type":"shortcuts_list","shortcuts":[{"name":"Abrir Firefox","command":"..."},...]}
PC -> Celular   {"type":"mode_changed","mode":"extend"}
```

## Dicas de performance (PC ou celular fracos)

O servidor já reduz automaticamente o trabalho de CPU por frame (menos
conversões de imagem internamente, e um redimensionamento mais leve nas
qualidades baixas). Além disso:
=======
### Rotação automática de resolução (Estender e Espelhar)

Quando o celular é rotacionado (retrato ↔ paisagem), o app envia as novas
dimensões da tela (`screenWidth`, `screenHeight` no offer e mensagem
`resize` posteriormente):

- **Modo Estender:** o servidor **reinicia o Xvfb automaticamente** com a
  resolução correspondente ao novo formato (ex.: 1280×720 em paisagem,
  720×1280 em retrato).
- **Modo Espelhar:** o servidor **recorta a região central do monitor do
  PC** que casa com o aspect do celular **antes** do letterbox. Antes, um
  celular em paisagem (aspect 2.2:1) com monitor 16:9 (1920×1080) ficava
  "quadrado na horizontal" por causa das barras pretas. Agora o crop
  elimina as barras e o vídeo preenche a tela do celular.

O trigger de resize dispara em `screenWidthDp`/`screenHeightDp` (não só
em `orientation`), então funciona em foldable, multi-window, tablet e
Chromebook — não apenas em celular comum.

### Cursor no modo Estender

O cursor do mouse **não é mais desenhado dentro do frame de vídeo** —
antes, o PC desenhava uma setinha em cima de cada frame (rodando o tempo
todo, mesmo com o mouse parado). Agora:

1. O servidor manda a **posição** do cursor como uma mensagem pequena pelo
   DataChannel a ~30x/s (independente do ritmo do vídeo, que fica mais
   lento quando a tela não muda).
2. Quando disponível (X11 com extensão XFixes), o servidor lê o **desenho
   real do cursor no PC** (seta, texto "I", mãozinha, redimensionar, etc.)
   e manda como bitmap PNG — só quando o cursor muda de forma, não em
   todo frame.
3. O celular desenha o cursor por cima do vídeo, na posição certa.

Se o XFixes não estiver disponível, o app simplesmente mostra uma setinha
genérica — nada quebra.

## Modo Espelhar Janela

Além do Espelhar (tela inteira) e do Estender (segunda tela), o servidor
suporta espelhar **apenas uma janela específica** selecionada na aba
"Janelas" da interface Tk. A lista de janelas é obtida via `xdotool
search --onlyvisible --name ""`, e a geometria (x, y, largura, altura) é
recalculada a cada frame para acompanhar janelas que se movem ou
redimensionam.

A correção do mapeamento de toque (toque na janela recortada → clique na
posição certa dentro da janela, não na tela inteira) foi aplicada tanto
no caminho do toque quanto no desenho do cursor.

## Menu flutuante (dentro da transmissão)

Enquanto a tela do PC é exibida no celular, um menu flutuante oferece
controle rápido sem precisar desconectar. Ele pode ser arrastado para
qualquer canto da tela (segurar e mover), some parcialmente após 3.5s
sem interação, e reabre com um toque.

### Botão "Qualidade"

Submenu com as opções: `144p`, `240p`, `360p`, `480p` (padrão), `720p`,
`1080p`, e `Automático`. A qualidade `Automática` começa em 480p e sobe
ou desce sozinha conforme a carga da CPU do PC.

### Botão "Modo"

Abre um submenu com duas opções que alteram o modo de exibição
**instantaneamente**, sem precisar voltar à tela inicial do app:

- **Espelhar** — o celular passa a repetir a tela do PC (tela :0).
- **Estender** — o celular vira a segunda tela virtual (tela :1).

A troca é feita via mensagem `mode_change` no WebSocket — o servidor
reconfigura a fonte de vídeo e o display virtual automaticamente, e
responde com `mode_changed` confirmando o modo que ficou ativo de fato.

### Botão "Atalhos" (modo Estender)

Lista os atalhos personalizados definidos no servidor (ver seção
*Atalhos personalizados* acima). Carrega a lista do endpoint REST
`GET /shortcuts` ao abrir o submenu.

### Botão "Configuração" / "Desconectar"

- **Configuração** — abre o dialog de configurações sem interromper a
  transmissão (vídeo continua rodando atrás).
- **Desconectar** — fecha a conexão WebRTC e volta pra tela inicial.

O menu inteiro tem animações de abrir/fechar (fade + zoom leve) e de
"afundar" ao tocar nos botões.

## Tela cheia imersiva + botão Voltar

### Tela cheia imersiva

Ao entrar na tela de transmissão (`ConnectedScreen`), o app ativa
automaticamente o modo imersivo "sticky" do Android:

- Barra de status e barra de navegação somem.
- O conteúdo desenha atrás delas (edge-to-edge).
- Swipes laterais/superiores revelam as barras temporariamente — elas
  somem sozinhas de novo (`BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE`).

Ao sair da tela de transmissão (desconectar, voltar pra Discovery) ou ao
abrir o dialog de Configurações, o modo imersivo é desligado.

### Botão Voltar nativo (1 toque = ESC, 2 toques = sair)

Hierarquia do botão Voltar durante a transmissão:

1. **Menu flutuante expandido / submenu aberto** — Back fecha o submenu →
   volta para EXPANDED → COLLAPSED (gerenciado por `BackHandler` interno
   do menu).
2. **Menu COLLAPSED (transmissão ativa):**
   - **1 toque** envia `escape` ao PC (`{"type":"key","key":"escape"}`).
   - **2 toques em <350ms** desconectam.

Na tela raiz (Discovery), 2 toques em <2s confirmam a saída do app —
evita fechar acidentalmente.

## QR Code criptografado (Item 3 do `MELHORIAS.md`)

O QR Code exibido na janela do servidor **não embute mais o PIN em texto
puro**. Em vez disso, carrega um token opaco no formato:

```
ND1.<base64url(host:port)>.<base64url(nonce || ciphertext || tag)>
```

- **Prefixo `ND1`** — versão do formato (permite evoluir o esquema no futuro).
- **`host:port`** — fica **fora** da camada cifrada (é público via mDNS
  mesmo). Sem ele, o app não saberia pra onde conectar.
- **Ciphertext AES-256-GCM** — embute `{pin, exp, nonce, v}`. A chave AES
  é gerada uma vez e persistida em `~/.config/NuDuck/qr_secret.key`
  (permissões `0600`).
- **TTL de 30s** — a UI Tk regenera o QR a cada 25s pra sempre estar dentro
  da validade.

### Fluxo de validação

1. App escaneia o QR no formato `ND1.<b64>.<b64>`.
2. App extrai `host:port` (parte pública, base64url).
3. App abre WS e envia `{"type":"qr_token","token":"ND1.<b64(ciphertext)>"}`.
4. Server decripta, valida `exp` e `pin`. Responde `pin_ok` ou
   `qr_token_error` (com `reason: "token_invalid_or_expired"`).
5. Em caso de erro, app mostra "QRCode expirado. Escaneie novamente." e
   volta para a tela de scan.

O app **nunca** decifra o token — só o server tem a chave AES. Escaneando
com Google Lens ou qualquer leitor externo, só se vê `ND1.<runa>`, sem
PIN, sem nada útil.

### Fallback JWT (compatibilidade)

Se por algum motivo a biblioteca `cryptography` não estiver disponível
(ex.: build quebrado), o servidor cai automaticamente num esquema JWT
(HS256) usando a mesma chave como segredo. O token JWT carrega
`{pin, exp, iat, jti}` e é validado pela biblioteca `PyJWT` — menos
seguro que AES-GCM (JWT é só assinado, não cifrado), mas mantém o app
funcionando até a dependência ser corrigida.

## Dicas de performance (PC ou celular fracos)

O servidor já faz várias otimizações automáticas:

- **Captura de tela com 1 thread só** — o OpenCV não disputa CPU com a
  codificação de vídeo (`cv2.setNumThreads(1)`), evitando competição
  direta com as threads que codificam o vídeo e com o resto do PC.
- **Prioridade de CPU reduzida** — o processo roda com `os.nice(10)`,
  dizendo ao Linux pra dar preferência a outros programas (navegador,
  player de vídeo) sempre que a CPU estiver disputada.
- **Limite de 2 núcleos** — `os.sched_setaffinity(0, {0, 1})` prende o
  processo (e todas as threads internas) a exatamente 2 núcleos, deixando
  os outros núcleos livres pro resto do PC o tempo todo, não só em
  disputa. Importante: com 2 núcleos pra capturar E codificar, qualidades
  muito altas (1080p) ainda podem ficar mais lentas — use 720p ou menos.
- **Idle CPU saving** — quando a tela do PC não muda por vários frames
  seguidos (ex.: usuário parado lendo algo), o servidor aumenta
  gradualmente o intervalo entre capturas (até ~6x mais devagar). Volta
  ao normal assim que qualquer mudança real aparecer.
- **Upscaling no cliente (GPU do celular)** — o servidor transmite em
  resolução baixa de verdade; é o celular quem amplia usando a própria
  GPU. O `SharpUpscaleDrawer.kt` aplica um filtro de nitidez **AMD CAS**
  (Contrast Adaptive Sharpening) em tempo real — sem custo perceptível de
  CPU/bateria — pra deixar a imagem ampliada menos borrada.
- **Cursor via DataChannel** — o cursor não é mais desenhado dentro do
  frame de vídeo (rodava em todo frame, mesmo com mouse parado). Vem como
  mensagem pequena pelo DataChannel, e a forma real do cursor só é enviada
  quando muda (via XFixes).

Recomendações manuais:
>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303

- **Use "Automático" na qualidade de vídeo.** Ele começa em 480p e sobe
  ou desce sozinho conforme a carga da CPU do PC — é a opção mais segura
  quando você não sabe de antemão se o hardware aguenta mais.
- **Se o vídeo travar/atrasar, force uma qualidade baixa manualmente**
  (144p ou 240p) em vez de deixar no automático — em PCs muito fracos,
  o ajuste automático pode demorar alguns segundos para "descer" e
  nesse meio tempo a imagem pode engasgar.
- **Modo Estender consome mais CPU que Espelhar** (ele roda um Xvfb
  inteiro por trás). Em hardware fraco, prefira o modo Espelhar.
- **No celular**, o vídeo é decodificado usando o acelerador de
  hardware do aparelho sempre que disponível — celulares com Android
  7.0+ de qualquer faixa de preço normalmente conseguem decodificar
  480p/720p sem esforço; o gargalo típico em aparelhos fracos costuma
  ser a rede Wi-Fi (2.4GHz sobrecarregado) mais do que o processador.
- **Feche outros apps pesados no celular** durante o uso — o
  SurfaceView usado para exibir o vídeo já é o modo mais leve
  disponível no Android, mas a RAM livre ainda importa para não sofrer
  lag por troca de app em segundo plano.
<<<<<<< HEAD
=======
- **Para menos lag, use cabo USB** em vez de Wi-Fi — além do link físico
  mais estável, o app ativa o perfil `low_latency` automaticamente (ver
  seção *Cabo USB* acima).
>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303

### Por que o PC fica mais lento (vídeo travando) enquanto transmite

É esperado que o uso de CPU suba bastante durante a transmissão: o
servidor precisa **codificar** cada frame da tela em vídeo (VP8/H.264)
em tempo real, e essa codificação é feita por software (sem usar a
placa de vídeo), então ela sempre consome uma boa fatia da CPU —
principalmente em qualidades altas (720p/1080p) ou em notebooks/PCs
<<<<<<< HEAD
mais fracos. Duas melhorias foram feitas para reduzir esse impacto:

1. **Menos disputa de CPU entre a captura de tela e a codificação de
   vídeo.** Antes, a biblioteca de captura (OpenCV) tentava usar todos
   os núcleos da CPU sozinha para redimensionar cada frame, competindo
   diretamente com as threads que codificam o vídeo — e com o resto do
   PC (navegador, player de vídeo). Agora ela usa só 1 thread, o que é
   suficiente para essa tarefa e elimina essa disputa.
2. **Prioridade mais baixa para o processo do servidor.** O servidor
   agora roda com prioridade de CPU reduzida (`nice`), o que diz ao
   Linux para dar preferência a outros programas — como o navegador
   rodando um vídeo do YouTube — sempre que a CPU estiver disputada.
   Isso ajuda bastante com o sintoma de "vídeo do YouTube travando"
   enquanto o celular está conectado.

Essas mudanças reduzem o **efeito colateral** sobre outros programas,
mas não eliminam o custo de CPU da codificação em si — se o PC
continuar lento, a recomendação continua sendo **baixar a qualidade de
vídeo** (ex.: 480p ou 360p) nas configurações do app, ou usar
"Automático".

## Segurança

- Sem PIN correto, nada funciona.
- 5 tentativas erradas = bloqueio de 60s.
- Só aceita IPs da rede local (192.168.x, 10.x, 172.16-31.x, localhost).
- WebRTC com criptografia DTLS/SRTP obrigatória.
- Controle remoto só funciona se o checkbox estiver marcado no PC.
- Nada sai da rede local — sem nuvem, sem telemetria.

## Changelog

### Atualização (02/08/2026) — Espelhar Janela: lista melhor + continua em segundo plano
- **Bug corrigido: a busca de janelas não encontrava programas
  minimizados/em outra área de trabalho, e às vezes trazia
  sub-janelas indesejadas** (menus, tooltips, caixinhas internas).
  Troquei o método de busca: agora usa a MESMA lista oficial de
  janelas que o alt-tab/barra de tarefas do seu ambiente gráfico usa,
  em vez de uma busca "crua" que pegava qualquer janela X11 existente.
  Resultado: encontra os programas de verdade, mesmo minimizados, sem
  trazer lixo de sub-janelas.
- **Novo: o Espelhar Janela continua mostrando a janela mesmo com ela
  atrás de outra (ou com outra na frente).** Antes, a captura só
  enxergava o que estava fisicamente visível na tela — se outra
  janela cobrisse a selecionada, o vídeo mostrava a de cima, não o
  conteúdo real da que você escolheu. Agora usa a extensão X
  Composite (a mesma técnica usada por compositores de janela e pelas
  miniaturas do alt-tab) pra ler o conteúdo da janela sempre, não
  importa o que está na frente. Se por algum motivo isso não funcionar
  no seu ambiente gráfico (extensão indisponível, formato de cor
  incomum), o app cai automaticamente de volta no método antigo em vez
  de travar — só perde esse benefício específico, sem quebrar o resto.
- **Não testado num X11 de verdade** (não tenho um servidor gráfico
  aqui pra testar) — validei a lógica e o formato dos dados que
  consigo verificar sem tela, mas se algo não funcionar exatamente
  como esperado no seu ambiente, me manda o log do `server.py` que eu
  ajusto.
=======
mais fracos.

As otimizações automáticas listadas acima reduzem o **efeito colateral**
sobre outros programas, mas não eliminam o custo de CPU da codificação em
si — se o PC continuar lento, a recomendação continua sendo **baixar a
qualidade de vídeo** (ex.: 480p ou 360p) nas configurações do app, ou
usar "Automático".

## Protocolo de sinalização (WebSocket, porta 8765)

Toda a sinalização roda em `ws://host:8765/ws` (texto claro na LAN; o
vídeo em si é sempre criptografado via DTLS/SRTP). As mensagens
envolvidas:

```
# Autenticação — PIN em texto ou token QR criptografado
Celular -> PC   {"type":"pin","pin":"123456"}
Celular -> PC   {"type":"qr_token","token":"ND1.<host_b64>.<blob_b64>"}
PC -> Celular   {"type":"pin_ok"}
PC -> Celular   {"type":"pin_error","blocked":bool}
PC -> Celular   {"type":"qr_token_error","reason":"token_invalid_or_expired","blocked":bool}

# Offer/Answer WebRTC
Celular -> PC   {"type":"offer","sdp":"...","sdpType":"offer","quality":"480p","mode":"mirror",
                 "profile":"standard"|"low_latency","maxBitrate":2500000,"maxFps":60,
                 "screenWidth":1080,"screenHeight":2400}
PC -> Celular   {"type":"answer","sdp":"...","sdpType":"answer","mode":"mirror",
                 "modeFallbackReason":string|null}

# Trickle ICE (apenas em perfil low_latency / cabo USB)
Celular -> PC   {"type":"ice_candidate","candidate":"...","sdpMid":"","sdpMLineIndex":0}

# Controle durante a transmissão
Celular -> PC   {"type":"quality","value":"720p"}
Celular -> PC   {"type":"mode_change","mode":"mirror"} | {"type":"mode_change","mode":"extend"}
PC -> Celular   {"type":"mode_changed","mode":"extend","modeFallbackReason":string|null}
Celular -> PC   {"type":"resize","width":1080,"height":2400}
PC -> Celular   {"type":"resize_ok"} | {"type":"resize_error","reason":"..."}

# Atalhos do modo Estender
Celular -> PC   {"type":"execute_shortcut","name":"Abrir Firefox"}

# Eventos de toque/tecla (via DataChannel "control", não WebSocket)
Celular -> PC   {"type":"tap","x":0.5,"y":0.5}
Celular -> PC   {"type":"move","x":0.5,"y":0.5}   # arrastar
Celular -> PC   {"type":"down","x":0.5,"y":0.5}  # botão apertado
Celular -> PC   {"type":"up","x":0.5,"y":0.5}    # botão solto
Celular -> PC   {"type":"key","key":"enter"}

# Cursor do mouse (PC -> Celular via DataChannel "control")
PC -> Celular   {"type":"cursor_pos","x":0.42,"y":0.18,
                 "shape":{"png":"<base64>","hotX":0,"hotY":0}}  # shape só quando muda

# Log remoto do app (aparece no terminal do PC com prefixo "[Celular]")
Celular -> PC   {"type":"log","level":"INFO"|"WARN"|"ERROR","tag":"...","message":"..."}

# Erros genéricos
PC -> Celular   {"type":"error","message":"..."}
```

> **Histórico:** versões antigas deste README listavam mensagens
> `switch_mode`, `run_shortcut` e `rotation` que **não existem mais no
> código**. Os nomes reais são `mode_change`, `execute_shortcut` (com
> campo `name`, não `command`) e `resize` (com `width`/`height`, não
> `orientation`). A lista de atalhos também não vem por WebSocket —
> vem pelo endpoint REST `GET /shortcuts`.

## REST API (HTTP, porta 8765)

O servidor também expõe endpoints REST úteis para automação e debugging.
Todos só aceitam conexões da rede local (192.168.x, 10.x, 172.16-31.x,
localhost).

| Método | Path                 | Descrição                                              |
|--------|----------------------|--------------------------------------------------------|
| GET    | `/status`            | `{name, allow_control, quality, current_mode}`         |
| GET    | `/windows`           | Lista janelas abertas (`[{id, name, pid}, ...]`)        |
| GET    | `/shortcuts`         | Lista atalhos (`{"shortcuts":[{"name":...}]}`)         |
| POST   | `/shortcuts`         | Adiciona/atualiza atalho (`{name, command}`)           |
| DELETE | `/shortcuts?name=...`| Remove atalho por nome                                 |
| POST   | `/shortcuts/execute` | Executa atalho no display virtual (`{name}`)           |

> O `GET /shortcuts` retorna só os **nomes** dos atalhos — os comandos
> nunca são expostos ao app, só executados no servidor.

## Segurança

- **PIN de 6 dígitos** — sem PIN correto (ou token QR válido), nada funciona.
- **Bloqueio por IP** — 5 tentativas erradas = bloqueio de 60s.
- **Rede local apenas** — só aceita IPs da rede local (192.168.x, 10.x,
  172.16-31.x, localhost). Conexões externas são recusadas no middleware.
- **WebRTC com criptografia DTLS/SRTP obrigatória** — não há como
  negociar mídia sem criptografia.
- **Controle remoto opt-in** — só funciona se o checkbox "Permitir
  controle" estiver marcado no PC. Sem ele, o celular só vê o vídeo.
- **QR Code criptografado** — AES-256-GCM com TTL de 30s (ver seção
  dedicada). A chave nunca sai do PC.
- **Nada sai da rede local** — sem nuvem, sem telemetria, sem conta.

## Build do desktop (PyInstaller)

Quem preferir distribuir o servidor como binário único (sem precisar
instalar Python), use o spec PyInstaller:

```bash
cd server/
pip install pyinstaller
pyinstaller nuduck.spec --clean --noconfirm
```

Gera `dist/NuDuck-Server` (Linux/macOS) ou `dist/NuDuck-Server.exe`
(Windows). O PyInstaller **não faz cross-compile** — rode no sistema
operacional alvo. O spec já embute:

- Ícone (`assets/icon.ico`)
- Dados do `aiortc`, `av`, `zeroconf`, `cryptography`, `pylibsrtp`, `pyee`
- Hidden imports do `pyautogui`, `Xlib`, `qrcode`, `PIL.ImageTk`,
  `tkinter` etc.

Quando rodando como binário, os dados do usuário (atalhos, chave AES do
QR) são persistidos em:

- **Linux:** `~/.config/NuDuck/`
- **Windows:** `%APPDATA%\NuDuck\`
- **macOS:** `~/Library/Application Support/NuDuck/`

Isso evita o bug clássico do PyInstaller `--onefile` de salvar dados numa
pasta TEMPORÁRIA que é apagada no fim do processo.

## CI (integração contínua)

O projeto tem dois pipelines de CI:

### GitHub Actions — `.github/workflows/build-desktop.yml`

Roda em cada push pra `main` (ou manualmente). Build do binário desktop
em **Linux e Windows** (matrix), instala as dependências de sistema
necessárias (libavdevice-dev, libopus-dev, libvpx-dev no Linux),
`pip install -r requirements.txt + pyinstaller`, roda o spec, e faz
upload dos artefatos `nuduck-server-linux` e `nuduck-server-windows`.

### Codemagic — `codemagic.yaml`

Dois workflows:

1. **`android-native-release`** — build do APK Android debug no
   `mac_mini_m2` (Java 17 + Android SDK pré-instalados). Faz upload do
   `*.apk` em `android/app/build/outputs/apk/debug/`.
2. **`server-validate`** — validação do server Python em instância Linux.
   Instala `cryptography>=42`, faz check de sintaxe (`ast.parse`) em
   `server.py` e `virtual_display.py`, e roda testes de round-trip do
   token QR criptografado (gera → valida → rejeita token expirado e
   token com PIN errado) e dos atalhos padrão (confirma os 3 atalhos
   `_DEFAULT_SHORTCUTS`).

## Configurações do app Android

A tela de Configurações (acessível pela engrenagem na tela inicial, ou
pelo botão "Configuração" do menu flutuante) permite ajustar:

- **Vídeo** — qualidade padrão (`144p`–`1080p` ou `Automático`).
- **Segunda tela** — modo padrão de conexão (Espelhar / Estender).
- **Tela e controle** — permitir controle por toque (envia toques ao PC;
  o PC também precisa permitir).
- **Lembrar PIN dos PCs** — evita redigitar o PIN da próxima vez (fica
  só no aparelho).
- **Aparência** — tema (Claro / Escuro / Automático do sistema) e idioma
  (Português / English / Automático do aparelho).
- **Inicialização** — abrir o NuDuck sozinho quando o celular ligar
  (precisa da permissão `RECEIVE_BOOT_COMPLETED`, concedida
  automaticamente na instalação).

## Changelog

### Atualização (05/08/2026) — Revisão completa do README
- **README reescrito do zero** depois de ler todos os arquivos do
  repositório (server Python, app Android Kotlin, configs e CI). A
  versão anterior estava defasada em vários pontos em relação ao código
  real:
  - Faltava o **modo Espelhar Janela** (terceiro modo, além de Espelhar
    e Estender) — captura só uma janela específica via `xdotool`, com
    geometria recalculada a cada frame.
  - Faltava a seção do **QR Code criptografado** (Item 3 do
    `MELHORIAS.md`) — token `ND1.<b64>.<b64>` com AES-256-GCM, TTL 30s,
    chave persistida em `~/.config/NuDuck/qr_secret.key`.
  - Faltava o **perfil `low_latency`** para cabo USB (Item 9) — H264,
    trickle ICE, MAXBUNDLE, `INTER_NEAREST`, sem cursor desenhado no frame.
  - Faltava a **tela cheia imersiva** e o **botão Voltar nativo** (1
    toque = ESC, 2 toques = sair) — Itens 4 e 5.
  - Faltava o **crop automático de aspect** no modo Espelhar (Item 6) e
    a **clonagem da aparência do display principal** no modo Estender
    (Item 2 — gsettings/feh).
  - Faltava a **REST API** (`/status`, `/windows`, `/shortcuts`,
    `/shortcuts/execute`).
  - Faltava a seção de **build do desktop via PyInstaller** (`nuduck.spec`)
    e do **CI** (GitHub Actions + Codemagic).
  - A seção de **protocolo de sinalização** tinha nomes errados de
    mensagens (`switch_mode` → `mode_change`, `run_shortcut` →
    `execute_shortcut`, `rotation` → `resize`), faltava `qr_token`,
    `qr_token_error`, `ice_candidate`, `cursor_pos`, `log`, e listava
    `shortcuts_list` que não existe (atalhos vêm por REST).
  - A seção de **dependências Python** não mencionava `cryptography>=42`
    nem `PyJWT>=2.8`.
  - A seção de **atalhos** não mencionava os 3 atalhos padrão seed
    (Configuração, Alt+F4, Multitarefa) nem o endpoint REST.
  - A seção de **performance** ainda falava em "1 núcleo de CPU" — o
    limite agora é 2 núcleos (`os.sched_setaffinity(0, {0, 1})`).
- Adicionei a estrutura completa do projeto (incluindo
  `.github/workflows/`, `codemagic.yaml`, `MELHORIAS.md`, `nuduck.spec`,
  `assets/`).
- Adicionei a seção de **Configurações do app Android** (tema, idioma,
  lembrar PIN, iniciar ao ligar, modo padrão).
>>>>>>> 2f8fc2548ceac9c569241a2cc11751cd87001303

### Atualização (02/08/2026) — Correção: mouse errado no modo "Espelhar Janela"
- **Bug corrigido: no modo Espelhar Janela (capturar só uma janela, não
  a tela toda), o cursor e o toque usavam a tela INTEIRA como
  referência**, mesmo o vídeo mostrando só a janela recortada — uma
  variável interna que deveria guardar "qual é a área real sendo
  mostrada" nunca era preenchida de verdade. Corrigido nos dois
  caminhos (toque e cursor desenhado), e validei a matemática
  simulando toques em vários pontos de uma janela recortada — bateu
  certo em todos.
- Revisei o caminho completo do mouse nos dois modos (Espelhar e
  Estender) — captura, letterbox, toque e cursor desenhado — e esse
  foi o único bug de posição que encontrei que ainda não tinha sido
  corrigido.

### Atualização (02/08/2026) — Correção: cursor "trava pra sempre" após um erro isolado
- **Bug corrigido: o laço que manda a posição do cursor podia morrer
  de vez.** Se qualquer coisa desse errado numa única atualização
  (situação rara, mas possível), o laço inteiro parava pra sempre — e
  o cursor ficava parado/invisível dali em diante, sem se recuperar
  sozinho. Agora um erro isolado só pula aquela atualização; a próxima
  (1/30 de segundo depois) tenta de novo normalmente, então o cursor
  não fica mais travado permanentemente por causa de um problema
  pontual.
- Se isso ainda continuar acontecendo depois de atualizar, me manda o
  que aparece no terminal do PC (onde o `server.py` está rodando)
  enquanto o problema acontece — com uma mensagem de erro exata eu
  consigo achar a causa certeira, em vez de eu ficar tentando adivinhar
  cenários.

### Atualização (02/08/2026) — Correção: toque errado no modo Estender
- **Bug corrigido: o toque no modo Estender não descontava as barras
  pretas do letterbox.** Essa correção já existia no modo Espelhar
  (quando a proporção da tela do celular é diferente da do PC/display
  virtual, o vídeo ganha barras pretas nas bordas) — mas só tinha sido
  aplicada no caminho do modo Espelhar, não no do Estender. Resultado:
  sempre que havia barras, o toque no modo Estender caía no lugar
  errado. Apliquei a mesma correção nos dois caminhos agora, e validei
  a matemática simulando vários pontos de toque — bateu certo em
  todos.

### Atualização (02/08/2026) — Servidor agora usa 2 núcleos de CPU
- O limite de CPU do servidor (que antes travava tudo em **1 núcleo
  só**) agora usa **2 núcleos**. Deixa mais folga pra capturar/codificar
  o vídeo em qualidades mais altas (720p, por exemplo) sem travar,
  continuando a deixar os outros núcleos livres pro resto do PC.
- Se um dia quiser ajustar de novo (mais ou menos núcleos), é só me
  pedir — é uma mudança de uma linha só no `server.py`.

### Atualização (02/08/2026) — Correções de mouse, modo Estender/Espelhar e menu
- **Bug corrigido: não dava pra voltar do Estender pro Espelhar.** Causa
  raiz: ao trocar de modo, o servidor não atualizava uma referência
  interna pra transmissão nova — então, numa segunda troca de modo, ele
  achava que já estava no modo pedido e não fazia nada. Corrigido; junto
  com isso, também corrigi o cursor parando de funcionar depois de
  trocar de modo (mesma causa).
- **Bug corrigido: cursor "parado"/"não aparece" no modo Estender.** A
  posição do cursor agora vem de uma fonte muito mais simples e
  confiável — o próprio comando que o servidor manda pro cursor virtual
  quando você toca a tela — em vez de depender de uma segunda conexão
  com o X11 que podia falhar silenciosamente.
- **Bug corrigido: cursor na posição errada no modo Espelhar.** O
  WebRTC pode desenhar barras pretas próprias quando a proporção do
  vídeo recebido não bate 100% com a da tela do celular (por causa de
  um pequeno arredondamento de resolução) — nem o toque nem o cursor
  levavam isso em conta antes. Agora os dois usam a área real onde o
  vídeo é desenhado, calculada a partir da resolução de fato recebida.
- **CPU alta no modo Estender.** Havia uma thread de captura interna
  do Xvfb rodando sempre a ~30fps fixo, não importa a qualidade
  escolhida nem se a tela estava parada — ela nunca desacelerava.
  Agora acompanha o ritmo real da transmissão (incluindo o throttling
  de tela parada). Além disso, enviar toques no modo Estender não
  trava mais o restante do vídeo por um instante (rodava um processo
  externo de forma bloqueante a cada toque/arrasto; agora roda em
  segundo plano).
- **Servidor limitado a 1 núcleo de CPU.** Além do `nice` (que já
  existia), o processo agora fica travado num único núcleo — os
  outros ficam livres pro resto do PC o tempo todo, não só quando há
  disputa. **Importante:** com só 1 núcleo pra capturar e codificar o
  vídeo, qualidades altas (720p/1080p) tendem a ficar mais lentas do
  que ficavam antes — se notar isso, o mais indicado é usar 480p ou
  menos (ou "Automático") nas configurações do app.
- **Menu flutuante:** botões reordenados para Qualidade, Modo, Atalhos,
  Configuração, Desconectar; adicionada uma animação simples de
  abrir/fechar o menu (fade + zoom leve) e uma animação de "afundar"
  ao tocar nos botões.

- **Refinamento extra no cursor do modo Estender:** mesmo com a fonte
  de posição já corrigida (comando enviado ao cursor virtual), o envio
  dessa posição pro celular ainda rodava junto com o ritmo do vídeo —
  que fica mais lento quando a tela não muda (economia de CPU). Como
  mover o mouse no modo Estender nunca muda os pixels (o Xvfb não
  desenha cursor), a atualização ficava "presa" nesse ritmo lento.
  Agora roda à parte, no seu próprio ritmo fixo (~30x/s), independente
  do vídeo. Também passei a mostrar o cursor no centro da tela assim
  que conecta, em vez de ficar invisível até o primeiro toque.
- **Papel de parede sem precisar instalar `feh`** (resposta à pergunta
  sobre "personalizar o display" — era isso: o aviso "feh não está
  instalado" no log). Antes, sem o `feh`, o papel de parede do Display
  2 (modo Estender) simplesmente não aparecia. Agora, quando o `feh`
  não está disponível, o server pinta o papel de parede sozinho (Xlib +
  Pillow, bibliotecas que já são dependência do projeto) — sem precisar
  instalar mais nada no PC.

### Atualização (01/08/2026) — AMD CAS + cursor no celular + economia de CPU parado
- **Nitidez trocada de "Unsharp Mask" para AMD CAS** (Contrast Adaptive
  Sharpening, do FidelityFX da AMD — algoritmo público/MIT). Diferença
  na prática: o CAS mede o contraste local antes de decidir o quanto
  realçar cada pixel, então realça pouco em áreas já detalhadas/com
  ruído (evita "auréolas" nas bordas) e mais em áreas lisas/borradas
  pela ampliação — resultado mais limpo que o unsharp mask simples.
- **Item 3 — cursor não é mais desenhado dentro do vídeo.** Antes, o PC
  desenhava uma setinha em cima de CADA frame (rodava o tempo todo,
  mesmo com o mouse parado). Agora ele só manda a posição do cursor
  como uma mensagem pequena pelo canal que já existia (DataChannel), e
  o celular desenha o cursor por conta própria, por cima do vídeo.
- **Item 4 — ícone do cursor de verdade.** Usando o X11 (extensão
  XFixes), o servidor detecta o desenho REAL do cursor no PC (seta,
  texto "I", mãozinha, redimensionar, etc.) e manda pro celular só
  quando ele muda de forma — não em todo frame. Se o XFixes não
  estiver disponível no ambiente do PC, o app simplesmente continua
  mostrando uma setinha genérica, sem quebrar nada.
- **Item 7 — menos CPU quando a tela do PC não muda.** O servidor
  agora percebe quando a tela fica parada (ex.: você lendo algo, sem
  mexer o mouse) e aumenta gradualmente o tempo entre capturas —
  economizando CPU de captura, redimensionamento e codificação
  exatamente quando não há nada de novo pra mostrar. Volta ao normal
  na hora que algo muda de novo na tela.
- Novo pacote Python necessário no PC: `python-xlib` (adicionado ao
  `requirements.txt` — se você instala as dependências manualmente,
  rode `pip install -r requirements.txt` de novo).

### Atualização (01/08/2026) — Correção: erro de build no Codemagic
- **Bug corrigido: `SharpUpscaleDrawer.kt` não compilava** (erro
  `Cannot access 'GlGenericDrawer': it is package-private in
  'org.webrtc'`). A causa foi eu ter usado uma classe interna da
  biblioteca do WebRTC que não pode ser estendida por código de fora
  dela. Reescrevi o arquivo inteiro implementando a interface pública
  `RendererCommon.GlDrawer` diretamente, usando só OpenGL ES padrão do
  Android — sem depender de nada interno do WebRTC. O comportamento
  (ampliação + nitidez na GPU) continua o mesmo, só a implementação por
  baixo dos panos mudou.

### Atualização (01/08/2026) — Correção: toque mapeado no lugar errado
- **Bug corrigido: o cursor do mouse sempre ia para o canto
  inferior/direito da tela, não importava onde a pessoa tocasse.**
  Causa raiz: no app Android, o tamanho da tela usado para calcular
  "onde" a pessoa tocou (em porcentagem, 0% a 100%) vinha de uma
  informação que às vezes não estava pronta a tempo, e ficava travada
  num valor mínimo (1 pixel) — fazendo qualquer toque virar "100%,
  100%" (canto inferior direito) depois de arredondado. Agora esse
  tamanho vem direto do sistema de layout do app (`onSizeChanged`), que
  está sempre correto e atualizado antes de qualquer toque ser
  processado — inclusive depois de girar a tela ou abrir/fechar o menu
  flutuante.
- **Correção extra: toques desalinhados quando a proporção da tela do
  celular é diferente da do PC.** Quando a tela do celular tem uma
  proporção muito diferente da do PC (ex.: celular bem "comprido"), o
  vídeo transmitido ganha barras pretas nas bordas para não distorcer a
  imagem. Antes, o servidor não sabia onde essas barras estavam e
  calculava a posição do toque como se elas não existissem — agora ele
  desconta exatamente o tamanho das barras, então 1 toque = 1 clique na
  posição exata, em qualquer combinação de resolução/proporção entre PC
  e celular.

### Atualização (01/08/2026) — Upscaling no lado do cliente
- **O servidor agora transmite em resolução baixa de verdade.** Antes, o
  frame era ampliado de volta para o tamanho físico da tela do celular
  *antes* de ser codificado — ou seja, mesmo em qualidade "144p" o
  codec de vídeo trabalhava com um frame do tamanho da tela inteira do
  celular (só borrado), gastando CPU e banda de rede à toa. Agora o
  servidor nunca amplia o frame; ele só ajusta a proporção (aspect
  ratio) da imagem para bater com a do celular, mantendo a resolução
  baixa escolhida na qualidade de vídeo.
- **Novo: `SharpUpscaleDrawer.kt` no app Android.** Como o vídeo agora
  chega pequeno, é o celular quem amplia — usando a própria GPU do
  aparelho (isso já era feito automaticamente pelo WebRTC ao desenhar
  o vídeo numa tela maior). Além de ampliar, esse novo desenhista
  aplica um filtro de nitidez em tempo real (GPU, sem custo perceptível
  de CPU/bateria) para deixar a imagem ampliada menos borrada.
- Resultado esperado: menos uso de CPU no PC (o codec de vídeo processa
  menos pixels), menos uso de rede/Wi-Fi, e uma imagem no celular ainda
  nítida graças ao realce de nitidez feito na GPU do aparelho.

### Atualização (01/08/2026)
- **Correção: PC lento / vídeos travando (ex.: YouTube) durante a
  transmissão.** O OpenCV não disputa mais CPU com a codificação de
  vídeo (`cv2.setNumThreads(1)`), e o processo do servidor agora roda
  com prioridade de CPU reduzida (`os.nice`), dando preferência a
  outros programas quando a CPU está disputada. Ver seção
  "Dicas de performance" para detalhes.

### Atualização (29/07/2026)
- **Novo: Botão "Modo" no menu flutuante** — permite trocar entre
  Espelhar e Estender instantaneamente durante a transmissão, sem
  voltar à tela inicial do app.
- **Novo: Botão "Atalhos" no menu flutuante** — no modo Estender,
  exibe atalhos personalizados definidos em `server/shortcuts.json`.
  Cada atalho tem um nome visível no celular e um comando que é
  executado no display virtual ao tocar.
- **Novo: Rotação automática de resolução** — ao rotacionar o celular
  no modo Estender, o Xvfb reinicia automaticamente com a resolução
  correspondente ao novo formato (retrato/paisagem).
- **Novo:** arquivo `server/shortcuts.json` para configuração de
  atalhos personalizados do display virtual.

### Atualização (27/07/2026)
- **Corrigido:** botão "Via cabo (USB)" não conectava quando o celular
  tinha depuração sem fio (Wi-Fi) ligada ao mesmo tempo do cabo — o
  `adb reverse` era rejeitado por ambiguidade ("mais de um
  dispositivo") e falhava em silêncio. Agora o servidor identifica cada
  aparelho conectado individualmente e aplica o comando em cada um.
- **Novo:** status de USB mais detalhado na janela do servidor
  (distingue "sem autorização" de "erro real"), ver seção *Solução de
  problemas* acima.
- **Performance:** captura de tela otimizada para usar menos CPU por
  frame (menos conversões de cor internas, redimensionamento mais leve
  nas qualidades 144p/240p) — ajuda tanto PCs mais fracos quanto a
  manter o "Automático" mais estável.
