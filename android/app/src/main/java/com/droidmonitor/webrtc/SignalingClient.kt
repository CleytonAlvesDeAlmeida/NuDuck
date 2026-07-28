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
        fun onAnswerReceived(sdp: String, sdpType: String, resolvedMode: String, modeFallbackReason: String?)
        fun onSignalingError(message: String)
        fun onSignalingClosed()
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
            "answer" -> listener?.onAnswerReceived(
                sdp = json.getString("sdp"),
                sdpType = json.getString("sdpType"),
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

    fun sendOffer(sdp: String, sdpType: String, quality: String, mode: String, screenWidth: Int = 0, screenHeight: Int = 0) {
        send(JSONObject().apply {
            put("type", "offer")
            put("sdp", sdp)
            put("sdpType", sdpType)
            put("quality", quality)
            put("mode", mode)
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
    }
}
