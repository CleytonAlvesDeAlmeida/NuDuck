package com.droidmonitor

import android.app.Application
import android.content.Context
import android.graphics.Point
import android.util.DisplayMetrics
import android.view.WindowManager
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.droidmonitor.discovery.MdnsDiscovery
import com.droidmonitor.discovery.PcInfo
import com.droidmonitor.settings.AppSettings
import com.droidmonitor.settings.LocaleHelper
import com.droidmonitor.settings.SettingsRepository
import com.droidmonitor.webrtc.RemoteLog
import com.droidmonitor.webrtc.SignalingClient
import com.droidmonitor.webrtc.WebRtcClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import org.webrtc.VideoTrack
import java.util.concurrent.TimeUnit

/** Telas possíveis do app. */
sealed class Screen {
    data object Discovery : Screen()
    data object QrScan : Screen()
    data class PinEntry(val pc: PcInfo) : Screen()
    data object Connecting : Screen()
    data object Connected : Screen()
    data class ConnectionError(val message: String) : Screen()
    /** Tela cheia de Configurações, acessada a partir da Descoberta. */
    data object Settings : Screen()
}

/** Representa um atalho vindo do servidor. */
data class ShortcutItem(val name: String)

data class UiState(
    val discoveredPcs: List<PcInfo> = emptyList(),
    val screen: Screen = Screen.Discovery,
    val quality: String = "480p",
    val pinError: String? = null,
    val settings: AppSettings = AppSettings(),
    /** PIN pré-preenchido ao abrir a tela de PIN, se "lembrar PIN" estiver ligado. */
    val prefilledPin: String = "",
    /** Modo escolhido para a próxima conexão ("mirror" ou "extend"); inicializado
     *  a partir de settings.defaultScreenMode, mas pode ser trocado na tela de PIN. */
    val screenMode: String = "mirror",
    /** Preenchido quando o PC não conseguiu atender ao modo pedido (ex.: pediu
     *  "extend" e o PC caiu para "mirror") — a UI mostra e depois limpa. */
    val modeNotice: String? = null,
    /** Modo atualmente ativo (resolvido pelo servidor). */
    val resolvedMode: String = "mirror",
    /** Lista de atalhos do servidor (nomes). */
    val shortcuts: List<ShortcutItem> = emptyList(),
    /** Se está carregando atalhos do servidor. */
    val shortcutsLoading: Boolean = false,
    /** PC conectado (para reconexão automática ao trocar modo). */
    val connectedPc: PcInfo? = null,
    /** PIN usado na conexão atual. */
    val connectedPin: String = "",
    /** True quando o PC foi encontrado via Ancoragem USB (ver [com.droidmonitor.usb.UsbConnectionMonitor]). */
    val usbTunnelActive: Boolean = false,
    /** Perfil de latência ativo: "standard" (Wi-Fi) ou "low_latency" (USB). */
    val latencyProfile: String = "standard",
    /** Mensagem curta para exibir como Toast/snackbar ao usuário
     *  (ex.: "Toque novamente para sair", "Conectando via cabo"). */
    val transientHint: String? = null,
    /** Token QR criptografado (formato ND1) pendente de envio no handshake.
     *  Quando preenchido, o app envia "qr_token" via WS em vez do PIN em texto. */
    val pendingQrToken: String? = null,
)

class MainViewModel(application: Application) : AndroidViewModel(application) {

    private val _uiState = MutableStateFlow(UiState())
    val uiState: StateFlow<UiState> = _uiState

    private val _remoteVideoTrack = MutableStateFlow<VideoTrack?>(null)
    val remoteVideoTrack: StateFlow<VideoTrack?> = _remoteVideoTrack

    private val mdnsDiscovery = MdnsDiscovery(application)
    private val settingsRepository = SettingsRepository(application)
    private var signalingClient: SignalingClient? = null
    private var webRtcClient: WebRtcClient? = null
    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .build()

    // ---- Estado interno para botão Voltar nativo (Item 5) ----
    // Timestamp do último toque no botão Voltar durante a transmissão.
    // 1 toque: envia ESC ao PC; 2 toques em <350ms: desconecta.
    private var lastBackAt: Long = 0L
    private val DOUBLE_BACK_THRESHOLD_MS = 350L

    // Timestamp do último toque no botão Voltar na tela raiz (Discovery,
    // sem tela anterior pra voltar). Separado de [lastBackAt] pra não
    // misturar com a lógica de ESC durante a transmissão. Janela maior
    // (2s) porque aqui é só uma confirmação de saída, não uma ação
    // repetida rapidamente durante controle remoto.
    private var lastRootBackAt: Long = 0L
    private val ROOT_DOUBLE_BACK_THRESHOLD_MS = 2000L

    // ---- Monitor de Ancoragem USB (Item 8) ----
    // Detecta se o PC foi encontrado via Ancoragem USB (varredura de subnet,
    // ver UsbConnectionMonitor). Se sim, qualquer conexão QR é redirecionada
    // para o cabo (ignora o IP do QRCode, que é da rede Wi-Fi).
    private val usbMonitor = com.droidmonitor.usb.UsbConnectionMonitor(application)

    init {
        val initialSettings = settingsRepository.load()
        _uiState.update {
            it.copy(
                settings = initialSettings,
                quality = initialSettings.defaultQuality,
                screenMode = initialSettings.defaultScreenMode,
            )
        }

        mdnsDiscovery.onPcFound = { pc ->
            _uiState.update { state ->
                if (state.discoveredPcs.any { it.host == pc.host && it.port == pc.port }) state
                else state.copy(discoveredPcs = state.discoveredPcs + pc)
            }
        }
        mdnsDiscovery.onPcRemoved = { name ->
            _uiState.update { state ->
                state.copy(discoveredPcs = state.discoveredPcs.filterNot { it.name == name })
            }
        }
        mdnsDiscovery.start()

        // Item 8: monitora a Ancoragem USB. Quando o PC é encontrado no
        // cabo, o QR Code deve ser redirecionado para o IP achado no
        // subnet USB em vez do IP da LAN Wi-Fi do QR.
        usbMonitor.onStateChange = { active ->
            _uiState.update { it.copy(usbTunnelActive = active) }
        }
        usbMonitor.start()
    }

    private fun pcKey(pc: PcInfo) = "${pc.host}:${pc.port}"

    fun selectPc(pc: PcInfo) {
        val settings = _uiState.value.settings
        val savedPin = settingsRepository.getSavedPin(pcKey(pc)).orEmpty()
        _uiState.update {
            it.copy(
                screen = Screen.PinEntry(pc),
                pinError = null,
                prefilledPin = savedPin,
                screenMode = settings.defaultScreenMode,
            )
        }
    }

    /** Conexão manual via cabo (Ancoragem USB): usa o IP do PC já
     *  encontrado pela varredura de subnet (ver [UsbConnectionMonitor]).
     *  Se ainda não achou (cabo não plugado, ancoragem desligada, ou
     *  varredura ainda rolando), mostra um erro orientando o usuário em
     *  vez de tentar conectar num host que não existe. */
    fun connectViaCable() {
        val app = getApplication<Application>()
        val host = usbMonitor.discoveredPcHost()
        if (host == null) {
            _uiState.update {
                it.copy(screen = Screen.ConnectionError(app.getString(R.string.usb_pc_not_found)))
            }
            return
        }
        val name = app.getString(R.string.pc_via_cable)
        selectPc(PcInfo(name = name, host = host, port = 8765))
    }

    fun openQrScan() {
        _uiState.update { it.copy(screen = Screen.QrScan) }
    }

    fun cancelQrScan() {
        _uiState.update { it.copy(screen = Screen.Discovery) }
    }

    fun openSettings() {
        _uiState.update { it.copy(screen = Screen.Settings) }
    }

    fun closeSettings() {
        _uiState.update { it.copy(screen = Screen.Discovery) }
    }

    /** Aplica e persiste uma mudança de configurações, vinda da SettingsScreen
     *  (tela cheia ou dialog durante a conexão). */
    fun updateSettings(newSettings: AppSettings) {
        val previous = _uiState.value.settings
        settingsRepository.save(newSettings)
        if (previous.language != newSettings.language) {
            LocaleHelper.apply(newSettings.language)
        }
        if (!newSettings.rememberPins) {
            settingsRepository.clearSavedPins()
        }
        _uiState.update { it.copy(settings = newSettings) }
    }

    fun setScreenMode(mode: String) {
        _uiState.update { it.copy(screenMode = mode) }
    }

    fun dismissModeNotice() {
        _uiState.update { it.copy(modeNotice = null) }
    }

    /**
     * Payload aceito (Item 7):
     *
     * 1) Token criptografado (formato novo, gerado pelo server após Item 3):
     *    `ND1.<base64url(host:port)>.<base64url(ciphertext)>`
     *    O app não decifra — extrai só host:port (parte pública) e reenvia
     *    o token opaco para o server via WS ("qr_token"). Se o server
     *    devolver `qr_token_error`, mostra mensagem de "QRCode expirado".
     *
     * 2) JSON legado (formato antigo, backward-compat):
     *    `{"host":"...","port":8765,"name":"...","pin":"123456"}`
     *    Mantido para servers antigos; recomenda-se atualizar o server.
     *
     * Item 8: se o PC já foi encontrado via Ancoragem USB, o IP do QR é
     * ignorado e a conexão é forçada pelo cabo (mais rápido, menos lag).
     */
    fun onQrCodeScanned(rawValue: String) {
        val app = getApplication<Application>()

        // ---- Item 7: formato novo (token criptografado ND1) ----
        if (rawValue.startsWith("ND1.")) {
            val parts = rawValue.split(".", limit = 2)
            if (parts.size != 2) {
                _uiState.update {
                    it.copy(screen = Screen.ConnectionError(app.getString(R.string.qr_invalid)))
                }
                return
            }
            // parts[1] = "<b64(host:port)>.<b64(ciphertext)>"
            val sub = parts[1].split(".", limit = 2)
            if (sub.size != 2) {
                _uiState.update {
                    it.copy(screen = Screen.ConnectionError(app.getString(R.string.qr_invalid)))
                }
                return
            }
            val hostPort = try {
                val paddedLen = (sub[0].length + 3) / 4 * 4
                String(android.util.Base64.decode(
                    sub[0].padEnd(paddedLen, '='),
                    android.util.Base64.URL_SAFE or android.util.Base64.NO_WRAP,
                ), Charsets.UTF_8)
            } catch (_: Exception) {
                _uiState.update {
                    it.copy(screen = Screen.ConnectionError(app.getString(R.string.qr_invalid)))
                }
                return
            }
            // hostPort = "192.168.1.10:8765"
            val hostMatch = Regex("^([^:]+):(\\d+)$").find(hostPort)
            val qrHost = hostMatch?.groupValues?.getOrNull(1) ?: ""
            val qrPort = hostMatch?.groupValues?.getOrNull(2)?.toIntOrNull() ?: 8765

            // ---- Item 8: prioriza cabo USB se o PC já foi encontrado ----
            val usbHost = usbMonitor.discoveredPcHost()
            val (effectiveHost, effectivePort, viaUsb) = if (usbHost != null) {
                Triple(usbHost, 8765, true)
            } else {
                Triple(qrHost, qrPort, false)
            }

            if (effectiveHost.isBlank()) {
                _uiState.update {
                    it.copy(screen = Screen.ConnectionError(app.getString(R.string.qr_invalid)))
                }
                return
            }

            val pc = PcInfo(
                name = app.getString(R.string.pc_via_qrcode),
                host = effectiveHost,
                port = effectivePort,
            )
            // Conecta direto, sem passar pela tela de PIN: o token já
            // autentica sozinho (o server valida no lugar do PIN). Pedir um
            // PIN de 6 dígitos aqui só confundia o usuário (o campo ficava
            // vazio e o botão "Conectar" desabilitado) e ainda desperdiçava
            // parte da janela de 30s de validade do token com o usuário
            // parado numa tela esperando digitar algo que não era necessário.
            //
            // Guarda o token ND1 INTEIRO (rawValue), não só sub[1]. O
            // formato é "ND1.<host_b64>.<blob_b64>" (3 partes) — o server
            // espera exatamente essas 3 partes pra validar. Guardar só o
            // ciphertext e reanexar "ND1." na hora de enviar (como era
            // antes) perdia o pedaço do host no meio, e o server rejeitava
            // o token sempre por formato inválido — era por isso que a
            // conexão via QR nunca completava e caía pro fluxo de PIN.
            _uiState.update {
                it.copy(
                    pendingQrToken = rawValue,
                    transientHint = if (viaUsb) "Conectando via cabo (USB)" else null,
                )
            }
            submitPin(pc, "")
            return
        }

        // ---- Formato legado: JSON com host/port/pin ----
        val pc: PcInfo
        val pin: String
        try {
            val json = org.json.JSONObject(rawValue)
            val qrHost = json.getString("host")
            val qrPort = json.optInt("port", 8765)
            // Item 8: prioriza cabo se o PC já foi encontrado via Ancoragem USB.
            val usbHost = usbMonitor.discoveredPcHost()
            val (effectiveHost, effectivePort, viaUsb) = if (usbHost != null) {
                Triple(usbHost, 8765, true)
            } else {
                Triple(qrHost, qrPort, false)
            }
            pc = PcInfo(
                name = json.optString("name").ifBlank { app.getString(R.string.pc_via_qrcode) },
                host = effectiveHost,
                port = effectivePort,
            )
            pin = json.optString("pin", "")
            if (viaUsb) {
                _uiState.update { it.copy(transientHint = "Conectando via cabo (USB)") }
            }
        } catch (_: Exception) {
            _uiState.update {
                it.copy(screen = Screen.ConnectionError(app.getString(R.string.qr_invalid)))
            }
            return
        }

        if (pin.length == 6 && pin.all(Char::isDigit)) {
            submitPin(pc, pin)
        } else {
            selectPc(pc)
        }
    }

    fun cancelPinEntry() {
        _uiState.update { it.copy(screen = Screen.Discovery, pinError = null) }
    }

    fun submitPin(pc: PcInfo, pin: String) {
        val mode = _uiState.value.screenMode
        // Item 9: se conectando pelo host achado via Ancoragem USB, ativa
        // o perfil de baixa latência.
        val viaUsb = pc.host == usbMonitor.discoveredPcHost()
        val profile = if (viaUsb) "low_latency" else "standard"
        val pendingToken = _uiState.value.pendingQrToken
        _uiState.update {
            it.copy(
                screen = Screen.Connecting,
                latencyProfile = profile,
                transientHint = null,
            )
        }

        val signaling = SignalingClient(pc.host, pc.port)
        signalingClient = signaling

        // Item 9: repassa o perfil para o WebRtcClient aplicar ajustes de latência.
        val rtc = WebRtcClient(getApplication(), signaling, profile)
        webRtcClient = rtc

        rtc.listener = object : WebRtcClient.Listener {
            override fun onRemoteVideoTrack(track: VideoTrack) {
                _remoteVideoTrack.value = track
                _uiState.update {
                    it.copy(
                        screen = Screen.Connected,
                        connectedPc = pc,
                        connectedPin = pin,
                    )
                }
                // Ao conectar, busca atalhos do servidor
                fetchShortcuts(pc.host, pc.port)
            }

            override fun onControlChannelReady() {
                // Canal pronto; eventos de toque já podem ser enviados.
            }

            override fun onConnectionFailed(reason: String) {
                _uiState.update { it.copy(screen = Screen.ConnectionError(reason)) }
            }

            override fun onModeResolved(requestedMode: String, resolvedMode: String, reason: String?) {
                _uiState.update { it.copy(resolvedMode = resolvedMode) }
                if (requestedMode != resolvedMode) {
                    RemoteLog.w("MainViewModel", "Modo pedido '$requestedMode' caiu para '$resolvedMode'" + (reason?.let { ": $it" } ?: ""))
                    val app = getApplication<Application>()
                    val base = app.getString(R.string.mode_fallback_notice)
                    val notice = if (!reason.isNullOrBlank()) "$base ($reason)" else base
                    _uiState.update { it.copy(modeNotice = notice) }
                }
            }
        }

        signaling.listener = object : SignalingClient.Listener {
            override fun onPinAccepted() {
                // Só guarda PIN no repositório se foi PIN legado (não token).
                if (pendingToken == null) {
                    settingsRepository.savePin(pcKey(pc), pin)
                }
                val screen = getRealScreenSize()
                // Item 9: profile repassado para o offer (server aplica ajustes).
                rtc.startConnection(_uiState.value.quality, mode, screen.first, screen.second, profile)
            }

            override fun onPinRejected(blocked: Boolean) {
                settingsRepository.forgetPin(pcKey(pc))
                RemoteLog.w("MainViewModel", if (blocked) "PIN bloqueado (muitas tentativas erradas)" else "PIN incorreto")
                val app = getApplication<Application>()
                if (blocked) {
                    _uiState.update {
                        it.copy(screen = Screen.ConnectionError(app.getString(R.string.pin_too_many_attempts)))
                    }
                } else {
                    _uiState.update {
                        it.copy(
                            screen = Screen.PinEntry(pc),
                            pinError = app.getString(R.string.pin_incorrect),
                            prefilledPin = "",
                        )
                    }
                }
            }

            // Item 7: token QR rejeitado (expirado ou inválido).
            override fun onQrTokenError(reason: String?, blocked: Boolean) {
                val app = getApplication<Application>()
                settingsRepository.forgetPin(pcKey(pc))
                if (blocked) {
                    _uiState.update {
                        it.copy(
                            screen = Screen.ConnectionError(app.getString(R.string.pin_too_many_attempts)),
                            pendingQrToken = null,
                        )
                    }
                } else {
                    _uiState.update {
                        it.copy(
                            screen = Screen.ConnectionError("QRCode expirado. Escaneie novamente."),
                            pendingQrToken = null,
                        )
                    }
                }
            }

            override fun onAnswerReceived(sdp: String, sdpType: String, resolvedMode: String, modeFallbackReason: String?) {
                rtc.applyRemoteAnswer(sdp, sdpType, resolvedMode, modeFallbackReason)
            }

            override fun onSignalingError(message: String) {
                _uiState.update { it.copy(screen = Screen.ConnectionError(message)) }
            }

            override fun onSignalingClosed() {
                if (_uiState.value.screen is Screen.Connected) {
                    val app = getApplication<Application>()
                    _uiState.update { it.copy(screen = Screen.ConnectionError(app.getString(R.string.signaling_closed_by_pc))) }
                }
            }

            override fun onModeChanged(resolvedMode: String, modeFallbackReason: String?) {
                _uiState.update { it.copy(resolvedMode = resolvedMode) }
                if (modeFallbackReason != null) {
                    val app = getApplication<Application>()
                    val base = app.getString(R.string.mode_fallback_notice)
                    val notice = "$base ($modeFallbackReason)"
                    _uiState.update { it.copy(modeNotice = notice) }
                }
            }
        }

        viewModelScope.launch {
            signaling.connect()
            if (pendingToken != null) {
                // Item 7: envia token criptografado opaco ao server.
                signaling.sendQrToken(pendingToken)
            } else {
                signaling.sendPin(pin)
            }
        }
    }

    fun changeQuality(quality: String) {
        _uiState.update { it.copy(quality = quality) }
        signalingClient?.sendQualityChange(quality)
    }

    /** Solicita mudança de modo durante conexão ativa (Espelhar <-> Estender). */
    fun switchMode(newMode: String) {
        _uiState.update { it.copy(screenMode = newMode) }
        signalingClient?.sendModeChange(newMode)
    }

    /** Busca atalhos do servidor via REST. */
    fun fetchShortcuts(host: String, port: Int) {
        _uiState.update { it.copy(shortcutsLoading = true) }
        viewModelScope.launch(Dispatchers.IO) {
            try {
                val url = "http://$host:$port/shortcuts"
                val request = Request.Builder().url(url).get().build()
                val response = httpClient.newCall(request).execute()
                if (response.isSuccessful) {
                    val body = response.body?.string() ?: "{}"
                    val json = JSONObject(body)
                    val arr = json.optJSONArray("shortcuts")
                    val items = mutableListOf<ShortcutItem>()
                    if (arr != null) {
                        for (i in 0 until arr.length()) {
                            val obj = arr.getJSONObject(i)
                            items.add(ShortcutItem(name = obj.getString("name")))
                        }
                    }
                    _uiState.update {
                        it.copy(
                            shortcuts = items,
                            shortcutsLoading = false,
                        )
                    }
                } else {
                    _uiState.update { it.copy(shortcutsLoading = false) }
                }
            } catch (e: Exception) {
                RemoteLog.w("MainViewModel", "Erro ao buscar atalhos: ${e.message}")
                _uiState.update { it.copy(shortcutsLoading = false) }
            }
        }
    }

    /** Executa um atalho no displaydigital do servidor. */
    fun executeShortcut(name: String) {
        signalingClient?.sendExecuteShortcut(name)
    }

    /** Envia as novas dimensões da tela (rotação do celular). */
    fun sendScreenResize(width: Int, height: Int) {
        signalingClient?.sendResize(width, height)
    }

    /** Ignorado silenciosamente se "Permitir controle por toque" estiver desligado
     *  nas Configurações (modo só-visualização, do lado do celular). */
    fun sendControlEvent(json: org.json.JSONObject) {
        if (!_uiState.value.settings.sendControlEvents) return
        webRtcClient?.sendControlEvent(json)
    }

    /** Exposto para a UI poder inicializar o SurfaceViewRenderer com o mesmo EglBase do cliente WebRTC. */
    val activeWebRtcClient: WebRtcClient?
        get() = webRtcClient

    /** Retorna a resolução real da tela (incluindo barras do sistema). */
    fun getRealScreenSize(): Pair<Int, Int> {
        val windowManager = getApplication<Application>()
            .getSystemService(Context.WINDOW_SERVICE) as? WindowManager
        if (windowManager != null) {
            val display = windowManager.defaultDisplay
            val point = Point()
            display.getRealSize(point)
            if (point.x > 0 && point.y > 0) {
                return Pair(point.x, point.y)
            }
        }
        // Fallback: DisplayMetrics
        val metrics = getApplication<Application>().resources.displayMetrics
        return Pair(metrics.widthPixels, metrics.heightPixels)
    }

    fun disconnect() {
        webRtcClient?.close()
        signalingClient?.close()
        webRtcClient = null
        signalingClient = null
        _remoteVideoTrack.value = null
        _uiState.update {
            it.copy(
                screen = Screen.Discovery,
                pinError = null,
                modeNotice = null,
                shortcuts = emptyList(),
                pendingQrToken = null,
                latencyProfile = "standard",
                transientHint = null,
            )
        }
    }

    /**
     * Lógica do botão Voltar nativo (Item 5).
     *
     * - 1 toque: envia ESC ao PC (volta/cancela ação no app ativo do display).
     *   Mostra o hint "Toque novamente para sair".
     * - 2 toques em <350ms: chama [onExit] (desconecta e volta pra Discovery).
     *
     * Não chama nada de UI direto — só dispara callbacks para a Composable
     * dona do BackHandler (em ConnectedScreen) decidir o que fazer.
     */
    fun handleSystemBack(onPromptExit: () -> Unit, onExit: () -> Unit) {
        val now = System.currentTimeMillis()
        if (now - lastBackAt < DOUBLE_BACK_THRESHOLD_MS) {
            // Toque duplo: sair.
            lastBackAt = 0L
            _uiState.update { it.copy(transientHint = null) }
            onExit()
        } else {
            // Toque simples: envia ESC ao PC e pede outro toque pra sair.
            lastBackAt = now
            _uiState.update { it.copy(transientHint = "Toque novamente para sair") }
            // Envia ESC. O server já trata {"type":"key","key":"escape"} via
            // pyautogui (mirror) ou xdotool (extend). Sem necessidade de
            // mudança no server — o tipo "key" já existe no protocolo.
            if (_uiState.value.settings.sendControlEvents) {
                webRtcClient?.sendControlEvent(com.droidmonitor.webrtc.ControlEvents.key("escape"))
            }
            onPromptExit()
        }
    }

    /** Limpa o hint transitório (chamado pela UI depois de mostrar o Toast). */
    fun clearTransientHint() {
        _uiState.update { it.copy(transientHint = null) }
    }

    /**
     * Lógica do botão Voltar na tela raiz (Discovery — não há tela anterior
     * pra voltar, então "1 toque volta" não se aplica; só "2 toques sai do
     * app" faz sentido aqui).
     *
     * - 1 toque: mostra o hint "Toque novamente para sair" (via Toast na UI).
     * - 2 toques em <2s: chama [onExit] (a UI fecha a Activity).
     */
    fun handleRootBack(onExit: () -> Unit) {
        val now = System.currentTimeMillis()
        if (now - lastRootBackAt < ROOT_DOUBLE_BACK_THRESHOLD_MS) {
            lastRootBackAt = 0L
            _uiState.update { it.copy(transientHint = null) }
            onExit()
        } else {
            lastRootBackAt = now
            _uiState.update { it.copy(transientHint = "Toque novamente para sair") }
        }
    }

    override fun onCleared() {
        super.onCleared()
        mdnsDiscovery.stop()
        usbMonitor.stop()
        webRtcClient?.close()
        signalingClient?.close()
    }
}
