package com.droidmonitor.discovery

import android.content.Context
import android.net.wifi.WifiManager
import android.util.Log
import javax.jmdns.JmDNS
import javax.jmdns.ServiceEvent
import javax.jmdns.ServiceListener
import java.net.InetAddress
import java.util.concurrent.Executors

private const val TAG = "MdnsDiscovery"
private const val SERVICE_TYPE = "_droidmonitor._tcp.local."

/**
 * Descobre servidores DroidMonitor anunciados na rede local (mesma sub-rede Wi-Fi).
 * Requer um MulticastLock (mDNS depende de multicast UDP), que é adquirido em start()
 * e liberado em stop().
 */
class MdnsDiscovery(private val context: Context) {

    private var jmdns: JmDNS? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    private val executor = Executors.newSingleThreadExecutor()

    private val listener = object : ServiceListener {
        override fun serviceAdded(event: ServiceEvent) {
            // Precisa resolver para obter host/porta.
            jmdns?.requestServiceInfo(event.type, event.name, 3000)
        }

        override fun serviceRemoved(event: ServiceEvent) {
            val name = event.info?.getPropertyString("name") ?: event.name
            onPcRemoved?.invoke(name)
        }

        override fun serviceResolved(event: ServiceEvent) {
            val info = event.info
            val addresses = info.inetAddresses
            if (addresses.isEmpty()) return
            val host = addresses[0].hostAddress ?: return
            val pcName = info.getPropertyString("name") ?: info.name
            val pc = PcInfo(name = pcName, host = host, port = info.port)
            Log.i(TAG, "PC encontrado: $pc")
            onPcFound?.invoke(pc)
        }
    }

    var onPcFound: ((PcInfo) -> Unit)? = null
    var onPcRemoved: ((String) -> Unit)? = null

    fun start() {
        executor.execute {
            try {
                acquireMulticastLock()
                val wifiManager = context.applicationContext
                    .getSystemService(Context.WIFI_SERVICE) as WifiManager
                val ip = intToInetAddress(wifiManager.connectionInfo.ipAddress)
                jmdns = JmDNS.create(ip, "droidmonitor-client")
                jmdns?.addServiceListener(SERVICE_TYPE, listener)
                Log.i(TAG, "Busca mDNS iniciada em $ip")
            } catch (e: Exception) {
                Log.e(TAG, "Falha ao iniciar mDNS: ${e.message}", e)
            }
        }
    }

    fun stop() {
        executor.execute {
            try {
                jmdns?.removeServiceListener(SERVICE_TYPE, listener)
                jmdns?.close()
                jmdns = null
            } catch (e: Exception) {
                Log.w(TAG, "Erro ao encerrar mDNS: ${e.message}")
            } finally {
                releaseMulticastLock()
            }
        }
    }

    private fun acquireMulticastLock() {
        val wifiManager = context.applicationContext
            .getSystemService(Context.WIFI_SERVICE) as WifiManager
        multicastLock = wifiManager.createMulticastLock("droidmonitor-mdns-lock").apply {
            setReferenceCounted(true)
            acquire()
        }
    }

    private fun releaseMulticastLock() {
        multicastLock?.let {
            if (it.isHeld) it.release()
        }
        multicastLock = null
    }

    private fun intToInetAddress(ip: Int): InetAddress {
        val bytes = byteArrayOf(
            (ip and 0xFF).toByte(),
            (ip shr 8 and 0xFF).toByte(),
            (ip shr 16 and 0xFF).toByte(),
            (ip shr 24 and 0xFF).toByte(),
        )
        return InetAddress.getByAddress(bytes)
    }
}
