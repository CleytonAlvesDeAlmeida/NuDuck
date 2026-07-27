package com.droidmonitor.webrtc

import org.json.JSONObject

/** Constrói as mensagens de controle no mesmo formato esperado por handle_control_message() no server.py. */
object ControlEvents {

    /** x, y normalizados entre 0.0 e 1.0 em relação ao tamanho da tela renderizada. */
    private fun pointerEvent(type: String, x: Float, y: Float) = JSONObject().apply {
        put("type", type)
        put("x", x.coerceIn(0f, 1f))
        put("y", y.coerceIn(0f, 1f))
    }

    fun tap(x: Float, y: Float): JSONObject = pointerEvent("tap", x, y)
    fun down(x: Float, y: Float): JSONObject = pointerEvent("down", x, y)
    fun move(x: Float, y: Float): JSONObject = pointerEvent("move", x, y)
    fun up(x: Float, y: Float): JSONObject = pointerEvent("up", x, y)

    fun key(keyName: String): JSONObject = JSONObject().apply {
        put("type", "key")
        put("key", keyName)
    }
}
