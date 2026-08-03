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

### Por que o PC fica mais lento (vídeo travando) enquanto transmite

É esperado que o uso de CPU suba bastante durante a transmissão: o
servidor precisa **codificar** cada frame da tela em vídeo (VP8/H.264)
em tempo real, e essa codificação é feita por software (sem usar a
placa de vídeo), então ela sempre consome uma boa fatia da CPU —
principalmente em qualidades altas (720p/1080p) ou em notebooks/PCs
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
