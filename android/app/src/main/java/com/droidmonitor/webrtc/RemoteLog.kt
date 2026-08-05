package com.droidmonitor.webrtc

import android.util.Log
import org.json.JSONObject

/**
 * Loga localmente no Logcat (igual sempre foi) e, quando há uma sinalização
 * ativa com o PC, encaminha a mesma mensagem por lá — o NuDuck Server então
 * imprime no próprio terminal (janela "Ver terminal"), com o prefixo
 * "[Celular]". Isso deixa bem mais fácil achar o motivo de uma conexão
 * falhar sem precisar plugar o celular no PC e rodar `adb logcat`.
 *
 * Só os eventos relevantes pra depurar conexão (WebSocket, ICE, erros) usam
 * isto — não é um substituto geral do Log.*, então o volume enviado é baixo.
 */
object RemoteLog {
    private var sink: ((JSONObject) -> Unit)? = null

    /** Chamado pelo SignalingClient assim que o WebSocket com o PC abre. */
    internal fun attach(newSink: (JSONObject) -> Unit) {
        sink = newSink
    }

    /** Chamado pelo SignalingClient quando a conexão cai/fecha. */
    internal fun detach(oldSink: (JSONObject) -> Unit) {
        if (sink === oldSink) sink = null
    }

    fun i(tag: String, message: String) {
        Log.i(tag, message)
        forward("INFO", tag, message)
    }

    fun w(tag: String, message: String) {
        Log.w(tag, message)
        forward("WARN", tag, message)
    }

    fun e(tag: String, message: String, throwable: Throwable? = null) {
        if (throwable != null) Log.e(tag, message, throwable) else Log.e(tag, message)
        val full = if (throwable != null) "$message: ${throwable.message}" else message
        forward("ERROR", tag, full)
    }

    private fun forward(level: String, tag: String, message: String) {
        val currentSink = sink ?: return
        try {
            currentSink(
                JSONObject().apply {
                    put("type", "log")
                    put("level", level)
                    put("tag", tag)
                    put("message", message)
                },
            )
        } catch (_: Exception) {
            // Nunca deixa uma falha ao encaminhar o log derrubar o app.
        }
    }
}
