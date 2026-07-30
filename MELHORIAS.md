# NuDuck — Atualização (9 itens implementados)

Este documento descreve as alterações feitas no repositório NuDuck (server Python
+ app Android) conforme solicitado. Cada item está marcado no código com comentários
`Item N:` para facilitar a revisão.

---

## Item 1 — SERVER: Atalhos pré-salvos para todo usuário novo

**Arquivo:** `server/server.py` (função `_load_shortcuts`, constante `_DEFAULT_SHORTCUTS`)

Agora, quando o `shortcuts.json` não existe ou está vazio, o server popula
automaticamente 3 atalhos:

| Nome | Comando |
|---|---|
| Configuração | `DISPLAY=:1 gnome-control-center &` |
| Alt+F4 | `DISPLAY=:1 xdotool key Alt+F4 &` |
| Multitarefa | `DISPLAY=:1 xdotool key Super &` |

Atalhos criados/removidos pelo usuário **não são sobrescritos** — o seed só
roda na primeira execução.

---

## Item 2 — SERVER: Clonar aparência do display0 → display1

**Arquivo:** `server/virtual_display.py` (método `_clone_display0_appearance`,
chamado em `start()` e `resize()`)

Quando o modo Estender ativa o Xvfb em `:N`, o método novo:

1. Lê tema GTK, ícones, fonte, cursor e variante de cor do display `:0` via
   `gsettings get org.gnome.desktop.interface <key>` (GNOME) ou
   `xfconf-query` (XFCE).
2. Aplica cada um no display `:N` via `gsettings set` ou `feh` (papel de parede).
3. Cada passo é independente e falha silenciosamente — o start do Xvfb não
   é bloqueado se um ambiente não tem `gsettings` (caso do LXQt puro).

Resulta em display digital com a mesma aparência do monitor principal.

---

## Item 3 — SERVER: QRCode criptografado com token temporário (30s)

**Arquivos:** `server/server.py` (`_build_qr_token_payload`,
`_validate_qr_token`, handler `qr_token` no WebSocket, `_build_qr_photo`,
refresh Tk a cada 25s), `server/requirements.txt` (adiciona `cryptography>=42`)

**Formato do QR**: `ND1.<base64url(host:port)>.<base64url(nonce||ciphertext||tag)>`

- A chave AES-256 é gerada uma vez e persistida em
  `~/.config/NuDuck/qr_secret.key` (permissões 0600).
- O PIN e a expiração vão **dentro** do ciphertext; host:port ficam **fora**
  (são públicos via mDNS, mas o PIN não).
- Token expira em 30s. A UI Tk regenera o QR a cada 25s.
- Escaneando com app externo: só vê `ND1.<runa_base64>.<runa_base64>` — ilegível.
- Backward-compat: o app ainda aceita JSON legado de servers antigos.

---

## Item 4 — APP: Tela cheia imersiva em Espelhar/Estender

**Arquivo:** `android/.../ui/Immersive.kt` (novo), `android/.../MainActivity.kt`
(`ConnectedScreen`)

- `ImmersiveModeEffect(active)` entra/sai do modo imersivo "sticky"
  (`BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE`) automaticamente ao entrar/sair
  de `ConnectedScreen`.
- System bars (status + navegação) somem; swipe lateral/superior revela
  temporariamente.
- Edge-to-edge: o conteúdo desenha atrás das system bars.

---

## Item 5 — APP: Botão Voltar nativo (1 toque = ESC, 2 toques = sair)

**Arquivos:** `android/.../MainActivity.kt` (`BackHandler` em `ConnectedScreen`),
`android/.../MainViewModel.kt` (`handleSystemBack`),
`android/.../ui/FloatingMenu.kt` (`BackHandler` interno)

Hierarquia:

1. **FloatingMenu expandido/subpágina aberta**: Back fecha o submenu →
   volta pra EXPANDED → COLLAPSED (BackHandler interno do menu).
2. **Menu COLLAPSED** (transmissão ativa): 1 toque envia `escape` ao PC
   (`{"type":"key","key":"escape"}` — já suportado pelo server via
   `pyautogui.press`/`xdotool key`); 2 toques em <350ms desconectam.

---

## Item 6 — APP + SERVER: Rotação automática no modo Espelhar

**Arquivos:** `android/.../MainActivity.kt` (trigger `LaunchedEffect` ampliado),
`server/server.py` (método `_crop_to_phone_aspect` em `ScreenCaptureTrack`)

**App**: trigger de resize trocou de `configuration.orientation` (só
portrait↔landscape) para `configuration.screenWidthDp, screenHeightDp` —
dispara em foldable, multi-window, tablet.

**Server**: novo método `_crop_to_phone_aspect` recorta a região central do
monitor do PC que casa com o aspect do celular **antes** do `_letterbox`. Antes,
um celular em paisagem (aspect 2.2:1) com monitor 16:9 (1920×1080) ficava
"quadrado na horizontal" por causa das barras pretas. Agora, o crop elimina
as barras e o vídeo preenche a tela do celular.

---

## Item 7 — APP: Leitura de QRCode criptografado (depende do Item 3)

**Arquivos:** `android/.../MainViewModel.kt` (`onQrCodeScanned` refatorado),
`android/.../webrtc/SignalingClient.kt` (`sendQrToken`, handler
`onQrTokenError`)

Fluxo:

1. App escaneia QR no formato `ND1.<b64(host:port)>.<b64(ciphertext)>`.
2. App extrai host:port (parte pública, base64url).
3. App abre WS e envia `{"type":"qr_token","token":"ND1.<b64(ciphertext)>"}`.
4. Server decripta, valida `exp` e `pin`. Responde `pin_ok` ou
   `qr_token_error` (com reason `token_invalid_or_expired`).
5. Em caso de erro, app mostra "QRCode expirado. Escaneie novamente." e
   volta para a tela de scan.

O app **nunca** decifra o token — só o server tem a chave AES.

---

## Item 8 — APP: Conexão via cabo + QRCode

**Arquivo:** `android/.../usb/UsbConnectionMonitor.kt` (novo),
`android/.../MainViewModel.kt` (prioriza cabo se túnel ativo)

`UsbConnectionMonitor` detecta o túnel adb reverse por dois caminhos:

1. **NetworkCallback com TRANSPORT_USB** (Android 23+) — dispara quando a
   network USB aparece/desaparece.
2. **Polling TCP** a cada 2s — abre Socket para `127.0.0.1:8765` com timeout
   200ms. Robusto contra ROMs que não reportam `TRANSPORT_USB`.

Em `onQrCodeScanned`, se `usbMonitor.isTunnelActive() == true`, o app
ignora o IP/porta do QR e força conexão em `127.0.0.1:8765`. Mostra hint
"Conectando via cabo (USB)".

---

## Item 9 — APP + SERVER: Perfil Baixa Latência para cabo

**Arquivos (app):** `android/.../webrtc/WebRtcClient.kt` (profile,
trickle ICE, MAXBUNDLE), `android/.../webrtc/SignalingClient.kt`
(`sendOffer` com `profile`, `maxBitrate`, `maxFps`; `sendIceCandidate`)
**Arquivos (server):** `server/server.py` (parâmetro `profile` em
`ScreenCaptureTrack`, handler `ice_candidate`, ajustes de captura)

Quando conectando via USB (host = 127.0.0.1 ou túnel ativo), ativa perfil
`low_latency`. Ajustes:

**App**:
- `bundlePolicy = MAXBUNDLE` (uma única porta ICE).
- `iceCandidatePoolSize = 0`.
- **Trickle ICE**: envia cada candidato ICE assim que disponível (via
  `sendIceCandidate`) em vez de esperar `COMPLETE` — corta ~1s do setup.
- Bitrate máximo 2.5 Mbps, fps máximo 60.
- H264 priorizado via `DefaultVideoEncoderFactory(egl, true, true)`.

**Server**:
- Aceita `profile` no offer e aplica em `ScreenCaptureTrack`.
- Aplica `maxBitrate` no `RTCRtpSender` (se suportado pelo aiortc).
- Handler `ice_candidate` no WS aplica trickle ICE via
  `pc.addIceCandidate()` (com parser defensivo de SDP).
- Em low_latency: usa `INTER_NEAREST` (mais rápido que `INTER_LINEAR`) e
  **pula o desenho do cursor** (corta 5-10ms por frame).

---

## Checklist final

| # | Item | Bloco | Status |
|---|---|---|---|
| 1 | Atalhos pré-salvos | Server | ✅ Implementado |
| 2 | Clonar aparência display0→display1 | Server | ✅ Implementado |
| 3 | QRCode criptografado (token 30s) | Server | ✅ Implementado |
| 4 | Tela cheia imersiva | App | ✅ Implementado |
| 5 | Botão Voltar nativo (1/2 toques) | App | ✅ Implementado |
| 6 | Rotação no Espelhar (crop de aspect) | App+Server | ✅ Implementado |
| 7 | Leitura QRCode criptografado | App | ✅ Implementado (dep. 3) |
| 8 | Cabo USB + QRCode | App | ✅ Implementado |
| 9 | Perfil Baixa Latência via cabo | App+Server | ✅ Implementado |

## Dependências cruzadas (resumo)

- **Item 7 ⇄ Item 3** (crítica): ambos precisam serem entregues juntos. O
  formato `ND1.<b64>.<b64>` é contrato entre app e server. Implementados
  de forma coordenada neste repositório.
- **Item 6 ⇄ Server**: trigger de resize é app-only, mas o fix visual real
  (crop) é server.
- **Item 9 ⇄ Server**: perfil LowLatency precisa de cooperação app+server.
- Itens 1, 2, 4, 5, 8 não têm dependência cruzada.

## Como testar (resumo)

1. **Item 1**: delete `~/.config/NuDuck/shortcuts.json` (ou o do projeto)
   e reinicie o server. Os 3 atalhos devem aparecer.
2. **Item 2**: ative modo Estender. O display digital deve ter o mesmo
   papel de parede e tema do monitor principal (requer `gsettings` no
   ambiente do display `:0` e `feh` instalado).
3. **Item 3**: escaneie o QR com Google Lens ou qualquer leitor externo —
   deve mostrar apenas `ND1.<runa>`, não o PIN.
4. **Item 4**: ao conectar, status bar e navbar somem; swipe para revelar.
5. **Item 5**: com menu COLLAPSED, 1 toque no back envia ESC ao PC; 2
   toques rápidos desconectam.
6. **Item 6**: gire o celular para paisagem no modo Espelhar — a imagem
   deve preencher a tela sem barras pretas laterais.
7. **Item 7**: escaneie o QR no app. Se demorar mais de 30s para escanear
   e o token expirar, mostra erro "QRCode expirado".
8. **Item 8**: plugue o cabo USB, autorize a depuração, espere o server
   aplicar `adb reverse`. Escaneie o QR — deve mostrar hint "Conectando
   via cabo" e a conexão deve usar o túnel USB.
9. **Item 9**: conectado via cabo, a transmissão deve estar visivelmente
   mais fluida (menos lag) do que via Wi-Fi.
