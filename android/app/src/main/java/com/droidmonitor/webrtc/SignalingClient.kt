package com.droidmonitor.webrtc

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

private const val TAG = "SignalingClient"

/**
 * Fala o protocolo de sinalização do server.py via WebSocket em ws://host:port/ws:
 *   -> {"type":"pin","pin":"123456"}
 *   <- {"type":"pin_ok"} | {"type":"pin_error","blocked":bool}
 *   -> {"type":"offer","sdp":...,"sdpType":"offer","quality":"480p","mode":"mirror"|"extend"}
 *   <- {"type":"answer","sdp":...,"sdpType":"answer","mode":"mirror"|"extend","modeFallbackReason":string|null}
 *   -> {"type":"quality","value":"720p"}  // ou "auto", "144p".."1080p"
 *   -> {"type":"mode_change","mode":"mirror"|"extend"}
 *   <- {"type":"mode_changed","mode":"mirror"|"extend","modeFallbackReason":string|null}
 *   -> {"type":"resize","width":1080,"height":2400}
 *   -> {"type":"execute_shortcut","name":"Abrir Firefox"}
 *   -> {"type":"log","level":"INFO"|"WARN"|"ERROR","tag":"...","message":"..."}
 *      // eventos de conexão do app (ver RemoteLog), aparecem no terminal do PC
 *      // com o prefixo "[Celular]" — funciona mesmo antes do PIN ser aceito.
 *
 * "mode" no offer é um pedido; o PC responde no answer qual modo ficou
 * ativo de fato — "extend" só funciona se o PC conseguir uma segunda tela
 * de verdade (monitor extra já existente ou virtual criado via xrandr no
 * X11). Se não conseguir, o PC cai para "mirror" sozinho e manda o motivo
 * exato em "modeFallbackReason" (ex.: sessão Wayland, sem saída de vídeo
 * sobrando, xrandr ausente etc.).
 */
class SignalingClient(
    private val host: String,
    private val port: Int,
) {
    interface Listener {
        fun onPinAccepted()
        fun onPinRejected(blocked: Boolean)
        /** Item 7: Token QR criptografado rejeitado pelo server (expirado/inválido). */
        fun onQrTokenError(reason: String?, blocked: Boolean) {}
        fun onAnswerReceived(sdp: String, sdpType: String, resolvedMode: String, modeFallbackReason: String?)
        fun onSignalingError(message: String)
        fun onSignalingClosed()
        /** Chamado quando o servidor confirma a mudança de modo. */
        fun onModeChanged(resolvedMode: String, modeFallbackReason: String?)
    }

    private var webSocket: WebSocket? = null
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS) // WebSocket: sem timeout de leitura
        .build()

    var listener: Listener? = null

    // Referência estável (mesma instância sempre) pra RemoteLog.attach/detach
    // conseguirem se identificar com "===" e nunca destacar o sink errado.
    private val remoteLogSink: (JSONObject) -> Unit = { payload -> send(payload) }

    fun connect() {
        val request = Request.Builder()
            .url("ws://$host:$port/ws")
            .build()

        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                RemoteLog.attach(remoteLogSink)
                RemoteLog.i(TAG, "WebSocket conectado a $host:$port")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleMessage(text)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                RemoteLog.e(TAG, "Falha no WebSocket", t)
                listener?.onSignalingError(t.message ?: "Erro de conexão")
                RemoteLog.detach(remoteLogSink)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                RemoteLog.i(TAG, "WebSocket fechado: $reason")
                listener?.onSignalingClosed()
                RemoteLog.detach(remoteLogSink)
            }
        })
    }

    private fun handleMessage(text: String) {
        val json = try {
            JSONObject(text)
        } catch (e: Exception) {
            RemoteLog.w(TAG, "Mensagem inválida do servidor: $text")
            return
        }

        when (json.optString("type")) {
            "pin_ok" -> listener?.onPinAccepted()
            "pin_error" -> listener?.onPinRejected(json.optBoolean("blocked", false))
            // Item 7: server rejeitou o token QR criptografado.
            "qr_token_error" -> listener?.onQrTokenError(
                reason = if (json.isNull("reason")) null else json.optString("reason"),
                blocked = json.optBoolean("blocked", false),
            )
            "answer" -> listener?.onAnswerReceived(
                sdp = json.getString("sdp"),
                sdpType = json.getString("sdpType"),
                resolvedMode = json.optString("mode", "mirror"),
                modeFallbackReason = if (json.isNull("modeFallbackReason")) null else json.optString("modeFallbackReason"),
            )
            "mode_changed" -> listener?.onModeChanged(
                resolvedMode = json.optString("mode", "mirror"),
                modeFallbackReason = if (json.isNull("modeFallbackReason")) null else json.optString("modeFallbackReason"),
            )
            "error" -> listener?.onSignalingError(json.optString("message", "Erro desconhecido"))
        }
    }

    fun sendPin(pin: String) {
        send(JSONObject().apply {
            put("type", "pin")
            put("pin", pin)
        })
    }

    /**
     * Item 7: envia token QR criptografado (formato ND1) como alternativa ao PIN.
     *
     * O server decripta com sua chave AES persistente, valida expiração e
     * responde `pin_ok` (autentica) ou `qr_token_error` (token inválido/expirado).
     * O token é opaco para o app — nunca é decifrado localmente.
     *
     * O parâmetro `token` deve ser apenas a parte cifrada (sem o prefixo "ND1."
     * e sem o host:port, que são públicos). O server recompõe o token completo
     * internamente — ele sabe qual é a parte pública porque foi ele quem gerou.
     */
    fun sendQrToken(token: String) {
        send(JSONObject().apply {
            put("type", "qr_token")
            // O token já vem completo ("ND1.<host_b64>.<blob_b64>") desde
            // onQrCodeScanned — NÃO reanexar "ND1." aqui de novo, senão o
            // pedaço do host no meio se perde e o server rejeita o token
            // por formato inválido (era exatamente esse o bug).
            put("token", token)
        })
    }

    /**
     * Item 9: `profile` pode ser "standard" (Wi-Fi) ou "low_latency" (USB).
     * O server usa para ajustar bitrate, fps, qualidade JPEG e codec.
     * `maxBitrate` e `maxFps` são dicas explícitas para o server limitar a
     * saída de mídia (quando suportado pelo aiortc).
     */
    fun sendOffer(
        sdp: String,
        sdpType: String,
        quality: String,
        mode: String,
        screenWidth: Int = 0,
        screenHeight: Int = 0,
        profile: String = "standard",
        maxBitrate: Int = 0,
        maxFps: Int = 0,
    ) {
        send(JSONObject().apply {
            put("type", "offer")
            put("sdp", sdp)
            put("sdpType", sdpType)
            put("quality", quality)
            put("mode", mode)
            put("profile", profile)
            if (maxBitrate > 0) put("maxBitrate", maxBitrate)
            if (maxFps > 0) put("maxFps", maxFps)
            if (screenWidth > 0 && screenHeight > 0) {
                put("screenWidth", screenWidth)
                put("screenHeight", screenHeight)
            }
        })
    }

    fun sendQualityChange(quality: String) {
        send(JSONObject().apply {
            put("type", "quality")
            put("value", quality)
        })
    }

    /** Solicita mudança de modo (Espelhar <-> Estender) durante conexão ativa. */
    fun sendModeChange(mode: String) {
        send(JSONObject().apply {
            put("type", "mode_change")
            put("mode", mode)
        })
    }

    /**
     * Item 9: Trickle ICE — envia um candidato ICE avulso para o server.
     * Usado em perfil LowLatency (cabo USB) para enviar cada candidato assim
     * que disponível, sem esperar ICE gathering completar. Em LAN, isso corta
     * ~1s do tempo até o primeiro frame.
     *
     * O server aplica via `pc.addIceCandidate()`. Se o server não suportar
     * trickle, simplesmente ignora — a conexão ainda funciona, só mais lenta.
     */
    fun sendIceCandidate(sdp: String, sdpMid: String?, sdpMLineIndex: Int) {
        send(JSONObject().apply {
            put("type", "ice_candidate")
            put("candidate", sdp)
            put("sdpMid", sdpMid ?: "")
            put("sdpMLineIndex", sdpMLineIndex)
        })
    }

    /** Envia as novas dimensões da tela do celular (rotação). */
    fun sendResize(width: Int, height: Int) {
        send(JSONObject().apply {
            put("type", "resize")
            put("width", width)
            put("height", height)
        })
    }

    /** Solicita execução de um atalho no displaydigital. */
    fun sendExecuteShortcut(name: String) {
        send(JSONObject().apply {
            put("type", "execute_shortcut")
            put("name", name)
        })
    }

    private fun send(payload: JSONObject) {
        // "log" nunca deve tentar logar sua própria falha de envio (evita
        // um loop de log tentando logar que não conseguiu logar).
        if (webSocket?.send(payload.toString()) != true && payload.optString("type") != "log") {
            RemoteLog.w(TAG, "Tentativa de envio sem conexão ativa")
        }
    }

    fun close() {
        webSocket?.close(1000, "Cliente desconectou")
        webSocket = null
        RemoteLog.detach(remoteLogSink)
        client.dispatcher.executorService.shutdown()
        try {
            client.dispatcher.executorService.awaitTermination(2, TimeUnit.SECONDS)
        } catch (_: InterruptedException) {
            client.dispatcher.executorService.shutdownNow()
        }
    }
}
