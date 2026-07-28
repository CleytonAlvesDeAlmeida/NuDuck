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

> **Se o celular tiver "Depuração sem fio" (wireless debugging) ligada ao
> mesmo tempo do cabo**, o `adb` enxerga dois dispositivos ao mesmo tempo
> e recusa comandos ambíguos — isso já é tratado automaticamente pelo
> servidor (ele aplica o `adb reverse` em cada dispositivo autorizado,
> não só "no primeiro que aparecer"). Se ainda assim não conectar, veja
> a seção **Solução de problemas** abaixo.

### Solução de problemas — "Via cabo" não conecta

1. Confirme que a depuração USB está **autorizada** no celular (aparece
   uma caixa de diálogo na primeira vez que você pluga o cabo em um PC
   novo — toque em "Permitir").
2. Na janela do servidor no PC, olhe o texto "Cabo USB: ...":
   - **"plugue e autorize depuração"** → o PC não está vendo nenhum
     celular. Tente outro cabo/porta USB (alguns cabos são só de
     carregamento, sem dados).
   - **"autorize a depuração no celular"** → o PC já vê o aparelho, mas
     ele ainda não foi autorizado. Olhe a tela do celular.
   - **"erro ao aplicar adb reverse"** → normalmente falta o pacote
     `adb` no PC. Instale com `sudo apt install android-tools-adb`
     (Debian/Ubuntu) ou `sudo dnf install android-tools` (Fedora).
   - **"pronto ✅"** → está tudo certo do lado do PC; toque em "Via cabo
     (USB)" no app.
3. Se nada disso resolver, feche o app no celular, desconecte e
   reconecte o cabo, e espere uns 5 segundos antes de abrir o app de
   novo (o servidor verifica dispositivos a cada 3 segundos).

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
