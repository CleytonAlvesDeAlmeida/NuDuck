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
│   ├── shortcuts.json       # Atalhos personalizados (modo Estender)
│   └── requirements.txt     # Dependências Python
└── android/                 # App Android (Kotlin + Compose)
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
> - `xdotool` — envia cliques pro display virtual
> - `openbox` — window manager pro display virtual
> - `x11-xserver-utils` — fornece o `xsetroot` (cor de fundo do desktop virtual)
> - `x11-apps` — fornece o `xwd` (captura de tela de reserva, caso a captura rápida falhe)
> - `xterm` — terminal que abre automaticamente na tela virtual

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

### Conexão USB (Ancoragem USB — recomendada para menos lag)
Plugue o cabo e ative **Ancoragem USB** nas configurações do celular
(Configurações → Rede e Internet → Ponto de acesso e ancoragem →
Ancoragem USB). O PC não precisa fazer nada — o próprio celular acha o
servidor automaticamente assim que a ancoragem liga. Depois é só tocar em
"Via cabo (USB)" no app.

> **Por que Ancoragem USB e não Depuração USB?** A Ancoragem cria uma
> interface de rede IP de verdade sobre o cabo (como se fosse um Wi-Fi a
> mais, só que via USB), então o vídeo consegue usar UDP nativamente —
> bem menos lag do que tunelar tudo por dentro do protocolo do `adb`
> (que só suporta TCP e adiciona uma camada extra de overhead).

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
{
  "shortcuts": [
    {"name": "Abrir Firefox", "command": "DISPLAY=:1 firefox &"},
    {"name": "Terminal", "command": "DISPLAY=:1 xterm &"},
    {"name": "Navegador de arquivos", "command": "DISPLAY=:1 pcmanfm &"}
  ]
}
```

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

## Segurança

- Sem PIN correto, nada funciona.
- 5 tentativas erradas = bloqueio de 60s.
- Só aceita IPs da rede local (192.168.x, 10.x, 172.16-31.x, localhost).
- WebRTC com criptografia DTLS/SRTP obrigatória.
- Controle remoto só funciona se o checkbox estiver marcado no PC.
- Nada sai da rede local — sem nuvem, sem telemetria.

## Changelog

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
