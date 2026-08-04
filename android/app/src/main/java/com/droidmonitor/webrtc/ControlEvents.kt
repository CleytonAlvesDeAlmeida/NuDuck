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

    /** Scroll com um dedo. amount > 0 = para cima, < 0 = para baixo. */
    fun scroll(x: Float, y: Float, amount: Int): JSONObject = JSONObject().apply {
        put("type", "scroll")
        put("x", x.coerceIn(0f, 1f))
        put("y", y.coerceIn(0f, 1f))
        put("amount", amount)
    }

    /** Pinch-to-zoom com dois dedos.
     *  cx, cy: centro do pinch (normalizado 0-1).
     *  scaleDelta: razão de escala (ex.: 1.1 = zoom in de 10%, 0.9 = zoom out de 10%).
     */
    fun pinchZoom(cx: Float, cy: Float, scaleDelta: Float): JSONObject = JSONObject().apply {
        put("type", "pinch_zoom")
        put("cx", cx.coerceIn(0f, 1f))
        put("cy", cy.coerceIn(0f, 1f))
        put("scale_delta", scaleDelta)
    }
}
