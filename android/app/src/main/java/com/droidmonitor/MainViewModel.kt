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

    /** Conexão manual via USB: o servidor já aplica `adb reverse tcp:8765 tcp:8765`
     *  sozinho assim que detecta o celular com depuração USB autorizada. */
    fun connectViaCable() {
        val name = getApplication<Application>().getString(R.string.pc_via_cable)
        selectPc(PcInfo(name = name, host = "127.0.0.1", port = 8765))
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
     * Payload esperado, gerado pelo NuDuck Server no QR Code exibido junto ao PIN:
     * `{"host":"192.168.x.x","port":8765,"name":"meu-pc","pin":"123456"}`.
     * Se o PIN vier no QR, conecta direto; senão, cai na tela normal de PIN.
     */
    fun onQrCodeScanned(rawValue: String) {
        val pc: PcInfo
        val pin: String
        val app = getApplication<Application>()
        try {
            val json = org.json.JSONObject(rawValue)
            pc = PcInfo(
                name = json.optString("name").ifBlank { app.getString(R.string.pc_via_qrcode) },
                host = json.getString("host"),
                port = json.optInt("port", 8765),
            )
            pin = json.optString("pin", "")
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
        _uiState.update { it.copy(screen = Screen.Connecting) }

        val signaling = SignalingClient(pc.host, pc.port)
        signalingClient = signaling

        val rtc = WebRtcClient(getApplication(), signaling)
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
                settingsRepository.savePin(pcKey(pc), pin)
                val screen = getRealScreenSize()
                rtc.startConnection(_uiState.value.quality, mode, screen.first, screen.second)
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
            signaling.sendPin(pin)
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
        _uiState.update { it.copy(screen = Screen.Discovery, pinError = null, modeNotice = null, shortcuts = emptyList()) }
    }

    override fun onCleared() {
        super.onCleared()
        mdnsDiscovery.stop()
        webRtcClient?.close()
        signalingClient?.close()
    }
}
