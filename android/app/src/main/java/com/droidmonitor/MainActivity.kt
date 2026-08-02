package com.droidmonitor

import android.app.Activity
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.compose.BackHandler
import androidx.activity.viewModels
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Snackbar
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.material3.windowsizeclass.WindowWidthSizeClass
import androidx.compose.material3.windowsizeclass.calculateWindowSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.PointerEventType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.foundation.Canvas
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.unit.IntSize
import kotlin.math.roundToInt
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import com.droidmonitor.discovery.PcInfo
import com.droidmonitor.settings.AppTheme
import com.droidmonitor.ui.FloatingMenuHost
import com.droidmonitor.ui.QrScanScreen
import com.droidmonitor.ui.SettingsScreen
import com.droidmonitor.ui.theme.DroidMonitorTheme
import com.droidmonitor.webrtc.ControlEvents
import kotlinx.coroutines.delay
import org.webrtc.EglBase
import org.webrtc.RendererCommon
import org.webrtc.SurfaceViewRenderer
import org.webrtc.VideoTrack
import com.droidmonitor.webrtc.SharpUpscaleDrawer

@Composable
private fun qualityLabels(): Map<String, String> {
    val auto = stringResource(R.string.settings_quality_auto)
    return linkedMapOf(
        "144p" to "144p",
        "240p" to "240p",
        "360p" to "360p",
        "480p" to "480p",
        "720p" to "720p",
        "1080p" to "1080p",
        "auto" to auto,
    )
}

class MainActivity : ComponentActivity() {

    private val viewModel: MainViewModel by viewModels()

    @OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            // Recalculado a cada recomposição da Activity (rotação, dobra,
            // redimensionamento em multi-janela) — permite adaptar o layout
            // a celular, tablet, dobrável e modo paisagem/retrato sem
            // reiniciar a Activity (ver configChanges no Manifest).
            val windowSizeClass = calculateWindowSizeClass(this)
            val uiState by viewModel.uiState.collectAsState()
            DroidMonitorTheme(theme = uiState.settings.theme) {
                Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    DroidMonitorApp(viewModel, windowSizeClass)
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
@Composable
fun DroidMonitorApp(viewModel: MainViewModel, windowSizeClass: WindowSizeClass) {
    val uiState by viewModel.uiState.collectAsState()
    val context = LocalContext.current
    val activity = context as? Activity

    // Item 5 (revisado): o hint "Toque novamente para sair" já existia no
    // ViewModel, mas nenhuma tela chegava a exibi-lo — o usuário nunca via
    // feedback nenhum ao apertar Voltar. Mostra como Toast sempre que o
    // ViewModel definir um hint, em qualquer tela.
    LaunchedEffect(uiState.transientHint) {
        val hint = uiState.transientHint
        if (hint != null) {
            Toast.makeText(context, hint, Toast.LENGTH_SHORT).show()
            viewModel.clearTransientHint()
        }
    }

    when (val screen = uiState.screen) {
        is Screen.Discovery -> {
            // Item 5 (revisado): antes, Voltar na tela raiz saía do app
            // direto no 1º toque (comportamento padrão do Android quando
            // nenhum BackHandler consome o evento). Agora pede confirmação
            // com "2 toques sai do app", igual ao resto do app.
            BackHandler(enabled = true) {
                viewModel.handleRootBack(onExit = { activity?.finish() })
            }
            DiscoveryScreen(
                pcs = uiState.discoveredPcs,
                windowSizeClass = windowSizeClass,
                onPcSelected = viewModel::selectPc,
                onQrScanRequested = viewModel::openQrScan,
                onCableConnectRequested = viewModel::connectViaCable,
                onSettingsRequested = viewModel::openSettings,
            )
        }

        is Screen.QrScan -> {
            BackHandler(enabled = true) { viewModel.cancelQrScan() }
            QrScanScreen(
                onScanned = viewModel::onQrCodeScanned,
                onCancel = viewModel::cancelQrScan,
            )
        }

        is Screen.PinEntry -> {
            BackHandler(enabled = true) { viewModel.cancelPinEntry() }
            PinEntryScreen(
                pc = screen.pc,
                pinError = uiState.pinError,
                prefilledPin = uiState.prefilledPin,
                screenMode = uiState.screenMode,
                onScreenModeChange = viewModel::setScreenMode,
                onCancel = viewModel::cancelPinEntry,
                onSubmit = { pin -> viewModel.submitPin(screen.pc, pin) },
            )
        }

        is Screen.Connecting -> {
            // Permite cancelar uma conexão travada (ex.: token QR expirado
            // no meio do handshake) em vez de ficar preso na tela.
            BackHandler(enabled = true) { viewModel.disconnect() }
            ConnectingScreen()
        }

        is Screen.Connected -> ConnectedScreen(
            viewModel = viewModel,
            quality = uiState.quality,
            settings = uiState.settings,
            modeNotice = uiState.modeNotice,
            onDismissModeNotice = viewModel::dismissModeNotice,
            onSettingsChange = viewModel::updateSettings,
            resolvedMode = uiState.resolvedMode,
            shortcuts = uiState.shortcuts,
            shortcutsLoading = uiState.shortcutsLoading,
            connectedPc = uiState.connectedPc,
        )

        is Screen.ConnectionError -> {
            BackHandler(enabled = true) { viewModel.disconnect() }
            ConnectionErrorScreen(
                message = screen.message,
                onDismiss = viewModel::disconnect,
            )
        }

        is Screen.Settings -> {
            BackHandler(enabled = true) { viewModel.closeSettings() }
            SettingsScreen(
                settings = uiState.settings,
                onSettingsChange = viewModel::updateSettings,
                onClose = viewModel::closeSettings,
            )
        }
    }
}

@OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
@Composable
fun DiscoveryScreen(
    pcs: List<PcInfo>,
    windowSizeClass: WindowSizeClass,
    onPcSelected: (PcInfo) -> Unit,
    onQrScanRequested: () -> Unit,
    onCableConnectRequested: () -> Unit,
    onSettingsRequested: () -> Unit,
) {
    // Padding e nº de colunas da lista escalam com a classe de largura da
    // janela: celular em pé (Compact), celular deitado/tablet pequeno
    // (Medium) e tablet grande/dobrável aberto/Chromebook (Expanded).
    val isWide = windowSizeClass.widthSizeClass != WindowWidthSizeClass.Compact
    val horizontalPadding = if (isWide) 32.dp else 20.dp
    val maxContentWidth = if (isWide) 900.dp else Dp.Unspecified

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background),
        contentAlignment = Alignment.TopCenter,
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .let { if (maxContentWidth != Dp.Unspecified) it.widthIn(max = maxContentWidth) else it }
                .padding(horizontal = horizontalPadding, vertical = 20.dp),
        ) {
            // Cabeçalho escuro com o logo branco do NuDuck, igual ao mockup "inicio.svg".
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(20.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(horizontal = 20.dp, vertical = 18.dp),
            ) {
                Image(
                    painter = painterResource(id = R.drawable.ic_nuduck_logo),
                    contentDescription = null,
                    modifier = Modifier.size(width = 28.dp, height = 32.dp),
                )
                Spacer(modifier = Modifier.size(14.dp))
                Text(
                    text = "NuDuck",
                    style = MaterialTheme.typography.headlineSmall,
                    color = MaterialTheme.colorScheme.onSurface,
                    modifier = Modifier.weight(1f),
                )
                IconButton(onClick = onSettingsRequested) {
                    Icon(
                        imageVector = Icons.Filled.Settings,
                        contentDescription = stringResource(R.string.discovery_settings_cd),
                        tint = MaterialTheme.colorScheme.onSurface,
                    )
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            // Formas alternativas de conectar: ler o QR Code do PC ou usar cabo USB
            // (Ancoragem USB — o celular acha o PC sozinho varrendo o subnet
            // criado pela ancoragem, ver UsbConnectionMonitor). Em telas largas os
            // botões ficam mais compactos (não esticam a largura toda) para não
            // parecerem gigantes ao lado da lista de PCs.
            Row(
                horizontalArrangement = Arrangement.spacedBy(12.dp),
                modifier = if (isWide) Modifier else Modifier.fillMaxWidth(),
            ) {
                OutlinedButton(
                    onClick = onQrScanRequested,
                    modifier = if (isWide) Modifier.widthIn(min = 160.dp) else Modifier.weight(1f),
                ) { Text(stringResource(R.string.discovery_qr_code)) }
                OutlinedButton(
                    onClick = onCableConnectRequested,
                    modifier = if (isWide) Modifier.widthIn(min = 160.dp) else Modifier.weight(1f),
                ) { Text(stringResource(R.string.discovery_cable)) }
            }

            Spacer(modifier = Modifier.height(20.dp))

            Text(
                text = stringResource(R.string.discovery_pcs_found),
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onBackground,
            )
            Spacer(modifier = Modifier.height(16.dp))

            if (pcs.isEmpty()) {
                Box(
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                    contentAlignment = Alignment.Center,
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
                        Spacer(modifier = Modifier.height(16.dp))
                        Text(
                            text = stringResource(R.string.discovery_searching),
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        Text(
                            text = stringResource(R.string.discovery_searching_hint),
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            } else {
                // GridCells.Adaptive: 1 coluna em celular estreito, 2+ colunas em
                // tablet/paisagem/dobrável — a própria largura disponível decide,
                // sem precisar ramificar a UI por dispositivo.
                //
                // weight(1f) em vez de fillMaxSize(): como filho comum (sem peso)
                // de uma Column, fillMaxSize() pedia a ALTURA TOTAL da tela pra
                // grade, por cima do cabeçalho/botões/rótulo que já ocupavam
                // espaço acima — o conteúdo excedia a tela e ficava cortado
                // embaixo. weight(1f) faz a grade usar só o espaço que sobra.
                LazyVerticalGrid(
                    columns = GridCells.Adaptive(minSize = 280.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                ) {
                    items(pcs) { pc ->
                        Card(
                            modifier = Modifier.fillMaxWidth(),
                            colors = CardDefaults.cardColors(
                                containerColor = MaterialTheme.colorScheme.surface,
                            ),
                        ) {
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(16.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Text("🖥", style = MaterialTheme.typography.headlineSmall)
                                Spacer(modifier = Modifier.size(12.dp))
                                Column(modifier = Modifier.weight(1f)) {
                                    Text(pc.name, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurface)
                                    Text(
                                        "${pc.host}:${pc.port}",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                }
                                Button(onClick = { onPcSelected(pc) }) {
                                    Text(stringResource(R.string.discovery_connect_button))
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun PinEntryScreen(
    pc: PcInfo,
    pinError: String?,
    prefilledPin: String,
    screenMode: String,
    onScreenModeChange: (String) -> Unit,
    onCancel: () -> Unit,
    onSubmit: (String) -> Unit,
) {
    var pin by remember(pc) { mutableStateOf(prefilledPin) }

    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(
            modifier = Modifier
                .widthIn(max = 420.dp)
                .verticalScroll(rememberScrollState())
                .padding(32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(stringResource(R.string.pin_entry_title, pc.name), style = MaterialTheme.typography.titleLarge)
            Spacer(modifier = Modifier.height(8.dp))
            Text(stringResource(R.string.pin_entry_subtitle), style = MaterialTheme.typography.bodyMedium)
            Spacer(modifier = Modifier.height(16.dp))

            OutlinedTextField(
                value = pin,
                onValueChange = { if (it.length <= 6 && it.all(Char::isDigit)) pin = it },
                label = { Text(stringResource(R.string.pin_entry_label)) },
                isError = pinError != null,
                supportingText = pinError?.let { { Text(it) } },
            )

            Spacer(modifier = Modifier.height(20.dp))

            // Escolha entre espelhar a tela do PC ou pedir uma segunda tela de
            // verdade ("estender"). O PC pode não conseguir atender "estender"
            // (precisa de X11 + uma saída de vídeo extra) — nesse caso ele avisa
            // e a conexão continua em modo Espelhar automaticamente.
            Text(
                stringResource(R.string.pin_entry_mode_label),
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.primary,
            )
            Spacer(modifier = Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                ScreenModeChip(
                    label = stringResource(R.string.pin_entry_mode_mirror),
                    hint = stringResource(R.string.pin_entry_mode_mirror_hint),
                    selected = screenMode == "mirror",
                    onClick = { onScreenModeChange("mirror") },
                    modifier = Modifier.weight(1f),
                )
                ScreenModeChip(
                    label = stringResource(R.string.pin_entry_mode_extend),
                    hint = stringResource(R.string.pin_entry_mode_extend_hint),
                    selected = screenMode == "extend",
                    onClick = { onScreenModeChange("extend") },
                    modifier = Modifier.weight(1f),
                )
            }

            Spacer(modifier = Modifier.height(24.dp))

            Row {
                TextButton(onClick = onCancel) { Text(stringResource(R.string.pin_entry_cancel)) }
                Spacer(modifier = Modifier.size(8.dp))
                Button(
                    onClick = { onSubmit(pin) },
                    enabled = pin.length == 6,
                ) { Text(stringResource(R.string.pin_entry_connect)) }
            }
        }
    }
}

@Composable
private fun ScreenModeChip(
    label: String,
    hint: String,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val background = if (selected) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surface
    val contentColor = if (selected) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurface
    Column(
        modifier = modifier
            .clip(RoundedCornerShape(12.dp))
            .background(background)
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 10.dp),
    ) {
        Text(label, color = contentColor, style = MaterialTheme.typography.bodyMedium)
        Text(hint, color = contentColor.copy(alpha = 0.8f), style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
fun ConnectingScreen() {
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            CircularProgressIndicator()
            Spacer(modifier = Modifier.height(12.dp))
            Text(stringResource(R.string.connecting_title))
        }
    }
}

@Composable
fun ConnectionErrorScreen(message: String, onDismiss: () -> Unit) {
    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(
            modifier = Modifier
                .widthIn(max = 420.dp)
                .verticalScroll(rememberScrollState())
                .padding(32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(stringResource(R.string.error_title), style = MaterialTheme.typography.titleLarge)
            Spacer(modifier = Modifier.height(8.dp))
            Text(message, style = MaterialTheme.typography.bodyMedium)
            Spacer(modifier = Modifier.height(24.dp))
            Button(onClick = onDismiss) { Text(stringResource(R.string.error_dismiss)) }
        }
    }
}

@Composable
fun ConnectedScreen(
    viewModel: MainViewModel,
    quality: String,
    settings: com.droidmonitor.settings.AppSettings,
    modeNotice: String?,
    onDismissModeNotice: () -> Unit,
    onSettingsChange: (com.droidmonitor.settings.AppSettings) -> Unit,
    resolvedMode: String,
    shortcuts: List<com.droidmonitor.ShortcutItem>,
    shortcutsLoading: Boolean,
    connectedPc: com.droidmonitor.discovery.PcInfo?,
) {
    val remoteTrack by viewModel.remoteVideoTrack.collectAsState()
    var showSettingsDialog by remember { mutableStateOf(false) }

    // ---- Tela cheia imersiva (Item 4) ----
    // Enquanto estivermos em ConnectedScreen, esconde system bars (status +
    // navegação) e desenha edge-to-edge. Sai automaticamente ao desmontar
    // (volta para Discovery) — ver ImmersiveModeEffect.
    com.droidmonitor.ui.ImmersiveModeEffect(active = true)

    // ---- Botão Voltar nativo (Item 5) ----
    // BackHandler tem precedência sobre BackHandler interno do FloatingMenu
    // (que só está ativo quando o menu está expandido). Quando o menu está
    // COLLAPSED, este BackHandler dispara: 1 toque = enviar ESC ao PC,
    // 2 toques rápidos (< 350ms) = sair da transmissão.
    BackHandler(enabled = !showSettingsDialog) {
        viewModel.handleSystemBack(
            onPromptExit = {
                // 1 toque: envia ESC. O hint "Toque novamente para sair"
                // é mostrado pelo ViewModel via uiState.transientHint.
            },
            onExit = {
                // 2 toques: desconecta e volta para Discovery.
                viewModel.disconnect()
            },
        )
    }

    // Detecta rotação do celular e envia nova resolução ao servidor.
    // No modo Estender, isso faz o Xvfb ser redimensionado automaticamente.
    // No modo Espelhar, faz o server recalcular o crop de aspect ratio (Item 6).
    //
    // Antes era só configuration.orientation (só dispara em portrait↔landscape).
    // Agora dispara em QUALQUER mudança de dimensão — cobre foldable abrindo,
    // multi-window redimensionado, tablet em modo livre, etc. (Item 6).
    val configuration = LocalConfiguration.current
    LaunchedEffect(configuration.screenWidthDp, configuration.screenHeightDp) {
        val (w, h) = viewModel.getRealScreenSize()
        viewModel.sendScreenResize(w, h)
    }

    // Some sozinho depois de alguns segundos, sem precisar de interação.
    // Mais tempo que antes: agora o aviso pode incluir o motivo técnico
    // exato reportado pelo PC (ex.: "sessão Wayland detectada"), então
    // precisa de mais tempo de leitura do que um aviso curto genérico.
    LaunchedEffect(modeNotice) {
        if (modeNotice != null) {
            delay(12000)
            onDismissModeNotice()
        }
    }

    // Sem barra fixa: durante a transmissão a tela é só o vídeo remoto,
    // com o menu flutuante do NuDuck sobreposto (arrastável, abre/fecha por toque).
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black),
    ) {
        val track = remoteTrack
        if (track != null) {
            // Bug corrigido: RemoteVideoView e CursorOverlay agora usam a
            // MESMA área real do vídeo (ver videoContentRect) — antes,
            // cada um assumia que o vídeo ocupava a view inteira, o que
            // ficava errado sempre que o WebRTC desenhava barras pretas
            // próprias por conta de um pequeno descompasso de proporção.
            var videoFrameSize by remember { mutableStateOf(IntSize.Zero) }
            RemoteVideoView(
                videoTrack = track,
                eglBaseContext = viewModel.activeWebRtcClient?.eglBase?.eglBaseContext,
                onControlEvent = { json -> viewModel.sendControlEvent(json) },
                onFrameResolutionChanged = { videoFrameSize = it },
            )
            // Item 3/4: cursor do mouse do PC, desenhado aqui pelo app (não
            // vem mais embutido no vídeo) — ver CursorOverlay abaixo.
            val cursorState by viewModel.cursorState.collectAsState()
            CursorOverlay(cursorState = cursorState, videoFrameSize = videoFrameSize)
        } else {
            Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = Color.White)
            }
        }

        FloatingMenuHost(
            quality = quality,
            qualityLabels = qualityLabels(),
            onQualityChange = viewModel::changeQuality,
            onOpenSettings = { showSettingsDialog = true },
            onDisconnect = viewModel::disconnect,
            currentMode = resolvedMode,
            onModeChange = viewModel::switchMode,
            shortcuts = shortcuts,
            shortcutsLoading = shortcutsLoading,
            onExecuteShortcut = viewModel::executeShortcut,
            onRefreshShortcuts = {
                connectedPc?.let { viewModel.fetchShortcuts(it.host, it.port) }
            },
        )

        if (modeNotice != null) {
            Snackbar(
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .padding(16.dp)
                    .clickable { onDismissModeNotice() },
            ) { Text(modeNotice) }
        }
    }

    if (showSettingsDialog) {
        Dialog(onDismissRequest = { showSettingsDialog = false }) {
            Surface(
                modifier = Modifier
                    .fillMaxWidth()
                    .widthIn(max = 480.dp),
                color = MaterialTheme.colorScheme.background,
                shape = RoundedCornerShape(16.dp),
            ) {
                SettingsScreen(
                    settings = settings,
                    onSettingsChange = onSettingsChange,
                    onClose = { showSettingsDialog = false },
                    modifier = Modifier.padding(vertical = 8.dp),
                )
            }
        }
    }
}

/**
 * Renderiza a track de vídeo remota em um SurfaceViewRenderer e converte toques
 * do usuário em eventos de controle normalizados (0.0-1.0), enviados pelo DataChannel.
 */
@Composable
fun RemoteVideoView(
    videoTrack: VideoTrack,
    eglBaseContext: org.webrtc.EglBase.Context?,
    onControlEvent: (org.json.JSONObject) -> Unit,
    onFrameResolutionChanged: (IntSize) -> Unit = {},
) {
    // BUG CORRIGIDO: o tamanho da view usado para normalizar o toque
    // (viewWidth/viewHeight) vinha do "update" do AndroidView, que só
    // reflete o tamanho real da View do Android depois que ela é
    // medida/desenhada pelo sistema — e nem sempre roda a tempo. Na
    // prática, viewWidth/viewHeight ficavam travados no valor inicial
    // (1, 1), então QUALQUER toque virava (posição / 1), um número bem
    // maior que 1, que era limitado (coerceIn) para 1.0 — ou seja,
    // sempre "canto inferior direito", não importa onde a pessoa
    // tocasse. Agora o tamanho vem de onSizeChanged, que é o próprio
    // Compose informando o tamanho de LAYOUT da view (sempre correto e
    // atualizado antes dos toques serem processados, inclusive após
    // rotação de tela ou o menu flutuante aparecer/sumir).
    var viewSize by remember { mutableStateOf(IntSize.Zero) }

    // Bug corrigido (cursor/toque na posição errada no modo Espelhar):
    // o SurfaceViewRenderer usa SCALE_ASPECT_FIT, que desenha o vídeo
    // AMPLIADO MAS RESPEITANDO A PROPORÇÃO — se a proporção do vídeo
    // recebido não bater 100% com a da view (isso pode acontecer por
    // um arredondamento pequeno, já que o servidor ajusta o vídeo em
    // múltiplos de 16 pixels), o próprio WebRTC desenha barras pretas
    // ADICIONAIS por conta própria, sem avisar o resto do app. Antes, o
    // toque (e o cursor, em CursorOverlay) assumia que o vídeo ocupava
    // a view inteira — então ficava sistematicamente deslocado sempre
    // que essas barras apareciam. Agora rastreamos a resolução real do
    // vídeo (via RendererEvents) e calculamos a área exata onde ele é
    // desenhado, usando essa área (não a view inteira) como referência.
    var videoSize by remember { mutableStateOf(IntSize.Zero) }

    AndroidView(
        modifier = Modifier
            .fillMaxSize()
            .onSizeChanged { viewSize = it }
            .pointerInput(Unit) {
                awaitPointerEventScope {
                    while (true) {
                        val event = awaitPointerEvent()
                        val position = event.changes.firstOrNull()?.position ?: continue
                        val rect = videoContentRect(viewSize, videoSize)
                        if (rect.width <= 0f || rect.height <= 0f) continue
                        val nx = ((position.x - rect.left) / rect.width).coerceIn(0f, 1f)
                        val ny = ((position.y - rect.top) / rect.height).coerceIn(0f, 1f)

                        when (event.type) {
                            PointerEventType.Press -> onControlEvent(ControlEvents.down(nx, ny))
                            PointerEventType.Move -> onControlEvent(ControlEvents.move(nx, ny))
                            PointerEventType.Release -> onControlEvent(ControlEvents.up(nx, ny))
                            else -> {}
                        }
                    }
                }
            },
        factory = { ctx ->
            SurfaceViewRenderer(ctx).apply {
                val rendererEvents = object : RendererCommon.RendererEvents {
                    override fun onFirstFrameRendered() {}
                    override fun onFrameResolutionChanged(videoWidth: Int, videoHeight: Int, rotation: Int) {
                        val rotated = rotation == 90 || rotation == 270
                        val size = if (rotated) IntSize(videoHeight, videoWidth) else IntSize(videoWidth, videoHeight)
                        videoSize = size
                        onFrameResolutionChanged(size)
                    }
                }
                // Upscaling no lado do cliente: o servidor manda a tela do
                // PC em resolução baixa de propósito (economiza CPU/rede no
                // PC); aqui a GPU do celular amplia e aplica um realce de
                // nitidez em tempo real (ver SharpUpscaleDrawer) em vez de
                // usar o desenhista padrão do WebRTC (que só amplia "cru").
                eglBaseContext?.let {
                    init(it, rendererEvents, EglBase.CONFIG_PLAIN, SharpUpscaleDrawer())
                }
                setScalingType(RendererCommon.ScalingType.SCALE_ASPECT_FIT)
                setEnableHardwareScaler(true)
                videoTrack.addSink(this)
            }
        },
    )

    DisposableEffect(videoTrack) {
        onDispose {
            // A limpeza do sink acontece implicitamente quando a PeerConnection fecha
            // (viewModel.disconnect / onCleared), evitando referenciar a view já destruída aqui.
        }
    }
}

/**
 * Calcula o retângulo (em pixels, dentro da view) onde o vídeo é
 * REALMENTE desenhado. Usado tanto pro toque (RemoteVideoView) quanto
 * pro cursor (CursorOverlay), pra garantir que os dois concordem com o
 * que está de fato na tela — ver comentário em RemoteVideoView.
 */
private fun videoContentRect(boxSize: IntSize, videoSize: IntSize): Rect {
    if (boxSize.width <= 0 || boxSize.height <= 0) {
        return Rect(0f, 0f, 0f, 0f)
    }
    if (videoSize.width <= 0 || videoSize.height <= 0) {
        // Ainda não recebemos nenhum frame — assume que ocupa a view
        // inteira (melhor aproximação possível até o primeiro frame).
        return Rect(0f, 0f, boxSize.width.toFloat(), boxSize.height.toFloat())
    }
    val boxAspect = boxSize.width.toFloat() / boxSize.height.toFloat()
    val videoAspect = videoSize.width.toFloat() / videoSize.height.toFloat()
    return if (videoAspect > boxAspect) {
        // Vídeo "mais largo" que a view -> barras em cima/embaixo.
        val displayW = boxSize.width.toFloat()
        val displayH = displayW / videoAspect
        val offsetY = (boxSize.height - displayH) / 2f
        Rect(0f, offsetY, displayW, offsetY + displayH)
    } else {
        // Vídeo "mais alto" que a view -> barras nas laterais.
        val displayH = boxSize.height.toFloat()
        val displayW = displayH * videoAspect
        val offsetX = (boxSize.width - displayW) / 2f
        Rect(offsetX, 0f, offsetX + displayW, displayH)
    }
}

/**
 * Item 3/4: desenha o cursor do mouse do PC por cima do vídeo.
 *
 * Antes, o servidor desenhava uma setinha DENTRO de cada frame de vídeo
 * — agora ele só manda a posição (e, quando muda, o desenho real do
 * cursor) como mensagens pequenas pelo DataChannel, e é o celular quem
 * desenha. Isso tira do PC um trabalho que rodava em todo frame, mesmo
 * parado.
 *
 * Enquanto nenhum desenho de cursor real chegou ainda (`bitmap == null`
 * — normal logo após conectar), desenha uma setinha simples genérica no
 * lugar, só pra não ficar sem nada.
 */
@Composable
fun CursorOverlay(cursorState: CursorState, videoFrameSize: IntSize, modifier: Modifier = Modifier) {
    var boxSize by remember { mutableStateOf(IntSize.Zero) }

    Box(
        modifier = modifier
            .fillMaxSize()
            .onSizeChanged { boxSize = it },
    ) {
        // Usa a mesma área real do vídeo que RemoteVideoView usa pro
        // toque (ver videoContentRect) — sem isso, o cursor ficava
        // deslocado sempre que o vídeo não ocupava a view inteira.
        val contentRect = videoContentRect(boxSize, videoFrameSize)
        if (contentRect.width <= 0f || contentRect.height <= 0f) return@Box

        val cursorSizeDp: Dp = 24.dp
        val density = LocalDensity.current
        val cursorSizePx = with(density) { cursorSizeDp.toPx() }

        val xPx = contentRect.left + cursorState.x * contentRect.width
        val yPx = contentRect.top + cursorState.y * contentRect.height

        val bitmap = cursorState.bitmap
        if (bitmap != null) {
            // Desloca pelo "ponto quente" real do cursor (ex.: a ponta da
            // seta, não o canto do bitmap) — assim a posição fica exata.
            val offsetX = (xPx - cursorState.hotXNorm * cursorSizePx).roundToInt()
            val offsetY = (yPx - cursorState.hotYNorm * cursorSizePx).roundToInt()
            Image(
                bitmap = bitmap.asImageBitmap(),
                contentDescription = null,
                modifier = Modifier
                    .offset { IntOffset(offsetX, offsetY) }
                    .size(cursorSizeDp),
            )
        } else {
            // Setinha genérica de fallback (mesmo desenho que o servidor
            // usava antes de embutir no vídeo), só até a primeira forma
            // real chegar do PC.
            Canvas(
                modifier = Modifier
                    .offset { IntOffset(xPx.roundToInt(), yPx.roundToInt()) }
                    .size(cursorSizeDp),
            ) {
                val w = size.width
                val h = size.height
                val path = Path().apply {
                    moveTo(0f, 0f)
                    lineTo(0f, h * 0.78f)
                    lineTo(w * 0.35f, h * 0.58f)
                    lineTo(w * 0.55f, h * 1.0f)
                    lineTo(w * 0.70f, h * 0.90f)
                    lineTo(w * 0.50f, h * 0.52f)
                    lineTo(w * 0.85f, h * 0.45f)
                    close()
                }
                drawPath(path, color = Color.White)
                drawPath(path, color = Color.Black, style = Stroke(width = 1.5f))
            }
        }
    }
}

