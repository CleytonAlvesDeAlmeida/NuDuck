package com.droidmonitor.usb

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Build
import com.droidmonitor.webrtc.RemoteLog
import java.net.InetSocketAddress
import java.net.Socket

private const val TAG = "UsbConnectionMonitor"

/**
 * Monitora a disponibilidade de túnel USB (adb reverse) entre o celular e o PC.
 *
 * O NuDuck Server executa `adb -s <serial> reverse tcp:8765 tcp:8765` no PC
 * assim que detecta um celular plugado com depuração USB autorizada. Isso
 * cria um túnel no celular: conectar em `127.0.0.1:8765` no celular alcança
 * a porta 8765 no PC.
 *
 * **Como detectamos**:
 *
 * 1. **NetworkCallback com TRANSPORT_USB** (Android 23+): dispara quando uma
 *    network com transporte USB aparece/desaparece. Esse é o caminho "nativo"
 *    para detectar a conexão de rede criada pelo ADB. Funciona em Android 7+
 *    e é o mais confiável em Android 11+.
 *
 * 2. **Fallback TCP reachability test**: abrimos um Socket com timeout curto
 *    (200ms) para `127.0.0.1:8765`. Se abrir, o túnel adb reverse está ativo.
 *    Esse teste é executado periodicamente (a cada 2s) porque em alguns
 *    fabricantes o TRANSPORT_USB não dispara via NetworkCallback.
 *
 * Item 8 do NuDuck: quando o usuário escaneia o QR Code enquanto o cabo está
 * conectado, o app ignora o IP do QR e força a conexão via 127.0.0.1:8765.
 * Sem isso, o celular tenta alcançar o IP da LAN do QR pela interface Wi-Fi
 * em vez do túnel USB, e a conexão falha.
 */
class UsbConnectionMonitor(private val context: Context) {

    /** Callback disparado quando o estado do túnel muda. True = túnel ativo. */
    var onStateChange: ((Boolean) -> Unit)? = null

    private var cm: ConnectivityManager? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    @Volatile
    private var networkTransportUsb: Boolean = false

    @Volatile
    private var tcpReachable: Boolean = false

    @Volatile
    private var lastReported: Boolean = false

    private val pollExecutor = java.util.concurrent.ScheduledThreadPoolExecutor(1)
    private var pollFuture: java.util.concurrent.ScheduledFuture<*>? = null

    /**
     * Inicia o monitor. Idempotente.
     */
    fun start() {
        if (networkCallback != null) return  // já iniciado
        cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager

        // ---- Caminho 1: NetworkCallback com TRANSPORT_USB ----
        // Dispara quando o adb reverse cria a network USB. Alguns fabricantes
        // (Xiaomi, Huawei) não reportam TRANSPORT_USB corretamente — por isso
        // mantemos o polling TCP abaixo como segundo caminho.
        try {
            val request = NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_USB)
                .build()
            val cb = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    RemoteLog.i(TAG, "Network USB disponível (callback).")
                    networkTransportUsb = true
                    maybeNotify()
                }

                override fun onLost(network: Network) {
                    RemoteLog.i(TAG, "Network USB perdida (callback).")
                    networkTransportUsb = false
                    maybeNotify()
                }
            }
            cm?.registerNetworkCallback(request, cb)
            networkCallback = cb
        } catch (exc: Exception) {
            // Algumas ROMs lançam IllegalArgumentException em TRANSPORT_USB.
            RemoteLog.w(TAG, "NetworkCallback USB não suportado: ${exc.message}. Usando só TCP polling.")
        }

        // ---- Caminho 2: Polling TCP a cada 2s ----
        // Robusto: independe de OEM/ROM. Custo: um socket de 200ms a cada 2s.
        pollFuture = pollExecutor.scheduleWithFixedDelay({
            try {
                val socket = Socket()
                socket.connect(InetSocketAddress("127.0.0.1", 8765), 200)
                socket.close()
                tcpReachable = true
            } catch (_: Exception) {
                tcpReachable = false
            }
            maybeNotify()
        }, 0, 2, java.util.concurrent.TimeUnit.SECONDS)
    }

    /**
     * True se o túnel USB estiver ativo agora.
     *
     * Considera ativo se OU o NetworkCallback reportou USB OU o TCP polling
     * conseguiu alcançar 127.0.0.1:8765. Esse OR cobre ROMs onde só um dos
     * dois detecta corretamente.
     */
    fun isTunnelActive(): Boolean = networkTransportUsb || tcpReachable

    private fun maybeNotify() {
        val current = isTunnelActive()
        if (current != lastReported) {
            lastReported = current
            RemoteLog.i(TAG, "Túnel USB: ${if (current) "ATIVO" else "INATIVO"}")
            onStateChange?.invoke(current)
        }
    }

    /**
     * Para o monitor e libera recursos. Idempotente.
     */
    fun stop() {
        try {
            networkCallback?.let { cm?.unregisterNetworkCallback(it) }
        } catch (_: Exception) {
            // já desregistrada
        }
        networkCallback = null
        pollFuture?.cancel(false)
        pollFuture = null
    }
}
