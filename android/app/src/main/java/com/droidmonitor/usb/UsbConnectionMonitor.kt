package com.droidmonitor.usb

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import com.droidmonitor.webrtc.RemoteLog
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

private const val TAG = "UsbConnectionMonitor"
private const val SERVER_PORT = 8765

/**
 * Monitora a **Ancoragem USB** (USB tethering) entre o celular e o PC.
 *
 * Por que Ancoragem USB e não Depuração USB (o antigo `adb reverse`)
 * -----------------------------------------------------------------------
 * A versão anterior usava Depuração USB: o PC rodava um loop chamando
 * `adb devices`/`adb reverse` a cada poucos segundos, e o celular
 * conectava em `127.0.0.1:8765` através do túnel TCP que o adb cria.
 * Dois problemas sérios para vídeo em tempo real, que é justamente o que
 * causava o lag reportado:
 *
 *   1. O protocolo do adb só encaminha TCP. WebRTC prefere UDP (ICE/SRTP)
 *      por ter bem menos overhead e não exigir retransmissão ordenada de
 *      pacote perdido (que trava o quadro seguinte esperando o anterior).
 *      Forçar tudo por dentro do túnel TCP do adb adiciona uma camada
 *      extra de serialização/multiplexação e aumenta o lag e o jitter.
 *   2. `adb reverse` falhava silenciosamente com mais de um dispositivo
 *      visível (comum com depuração sem fio + cabo ligados ao mesmo
 *      tempo) e exigia um loop no PC gastando CPU/subprocessos sem parar
 *      (item de consumo de recursos).
 *
 * Ancoragem USB (RNDIS) cria uma interface de rede IP de verdade sobre o
 * cabo — o mesmo tipo de link que o Wi-Fi, só que via USB — então o
 * WebRTC usa UDP nativamente nela, sem nenhuma camada extra. E o PC não
 * precisa rodar nada especial (nem adb, nem loop): o NuDuck Server já
 * escuta em todas as interfaces de rede, então basta o celular achar o
 * IP do PC dentro do link.
 *
 * Como o PC recebe um IP via DHCP dentro do subnet criado pelo celular
 * (Android usa `/24`, ex.: 192.168.42.0/24 ou 192.168.43.0/24 dependendo
 * da versão) e esse IP final não é previsível, o celular varre o subnet
 * (no máximo 253 hosts, em paralelo, timeout curto de 200ms) procurando
 * quem responde na porta 8765 do servidor. É rápido porque esse link só
 * tem 2 pontas (celular e PC) — normalmente encontra em menos de 1s.
 */
class UsbConnectionMonitor(private val context: Context) {

    /** Disparado quando o estado muda: true = achou o PC via Ancoragem USB. */
    var onStateChange: ((Boolean) -> Unit)? = null

    private var cm: ConnectivityManager? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private val scanExecutor = Executors.newFixedThreadPool(24)
    private val scanGeneration = AtomicInteger(0)

    @Volatile private var usbNetwork: Network? = null
    @Volatile private var discoveredHost: String? = null
    @Volatile private var lastReported: Boolean = false

    /**
     * Inicia o monitor. Idempotente.
     */
    fun start() {
        if (networkCallback != null) return  // já iniciado
        cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager

        try {
            val request = NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_USB)
                .build()
            val cb = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    RemoteLog.i(TAG, "Ancoragem USB disponível — procurando o PC no cabo…")
                    usbNetwork = network
                    scanSubnet(network)
                }

                override fun onLinkPropertiesChanged(network: Network, linkProperties: LinkProperties) {
                    // Em alguns aparelhos o IP só fica disponível um pouco
                    // depois do onAvailable — tenta de novo se ainda não achou.
                    if (network == usbNetwork && discoveredHost == null) {
                        scanSubnet(network, linkProperties)
                    }
                }

                override fun onLost(network: Network) {
                    if (network == usbNetwork) {
                        RemoteLog.i(TAG, "Ancoragem USB perdida.")
                        usbNetwork = null
                        discoveredHost = null
                        scanGeneration.incrementAndGet() // cancela varreduras em andamento
                        maybeNotify()
                    }
                }
            }
            cm?.registerNetworkCallback(request, cb)
            networkCallback = cb
        } catch (exc: Exception) {
            // Algumas ROMs lançam IllegalArgumentException em TRANSPORT_USB.
            RemoteLog.w(TAG, "NetworkCallback USB não suportado: ${exc.message}")
        }
    }

    /**
     * Varre o subnet da Ancoragem USB procurando o servidor NuDuck na
     * porta 8765. Só suporta prefixo /24 (o padrão do Android para
     * tethering) — outros tamanhos de subnet são raros nesse cenário e a
     * conta de "qual octeto varrer" fica errada pra eles, então preferimos
     * não tentar a arriscar varrer o subnet errado.
     */
    private fun scanSubnet(network: Network, linkPropertiesOverride: LinkProperties? = null) {
        val lp = linkPropertiesOverride ?: cm?.getLinkProperties(network) ?: return
        val myLink = lp.linkAddresses.firstOrNull { it.address is Inet4Address } ?: return
        val myAddr = myLink.address as Inet4Address

        if (myLink.prefixLength != 24) {
            RemoteLog.w(
                TAG,
                "Prefixo de rede inesperado (/${myLink.prefixLength}) na Ancoragem USB — " +
                    "varredura só suporta /24, abortando.",
            )
            return
        }

        val octets = myAddr.address.map { it.toInt() and 0xFF }
        val myLastOctet = octets[3]
        val generation = scanGeneration.incrementAndGet()
        val found = AtomicBoolean(false)
        val subnetPrefix = "${octets[0]}.${octets[1]}.${octets[2]}"

        RemoteLog.i(TAG, "Varrendo $subnetPrefix.0/24 em busca do PC (porta $SERVER_PORT)…")

        for (i in 1..254) {
            if (i == myLastOctet) continue  // não testa o próprio IP do celular
            val candidate = "$subnetPrefix.$i"
            scanExecutor.execute {
                if (generation != scanGeneration.get() || found.get()) return@execute
                try {
                    // network.socketFactory garante que a conexão sai pela
                    // interface da Ancoragem USB especificamente, mesmo que
                    // o Wi-Fi também esteja ativo ao mesmo tempo.
                    val socket = network.socketFactory.createSocket()
                    socket.use {
                        it.connect(InetSocketAddress(candidate, SERVER_PORT), 200)
                        if (found.compareAndSet(false, true) && generation == scanGeneration.get()) {
                            discoveredHost = candidate
                            RemoteLog.i(TAG, "PC encontrado via cabo em $candidate:$SERVER_PORT")
                            maybeNotify()
                        }
                    }
                } catch (_: Exception) {
                    // Não é esse host — esperado pra maioria dos 253 candidatos.
                }
            }
        }
    }

    /**
     * True se a Ancoragem USB estiver ativa E o PC já tiver sido
     * encontrado no cabo (a varredura já terminou com sucesso).
     */
    fun isTunnelActive(): Boolean = discoveredHost != null

    /** IP do PC descoberto no subnet de Ancoragem USB, ou null se ainda não achou. */
    fun discoveredPcHost(): String? = discoveredHost

    private fun maybeNotify() {
        val current = isTunnelActive()
        if (current != lastReported) {
            lastReported = current
            RemoteLog.i(TAG, "Ancoragem USB: ${if (current) "PC ENCONTRADO" else "PROCURANDO"}")
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
        usbNetwork = null
        discoveredHost = null
        scanGeneration.incrementAndGet() // cancela qualquer varredura pendente
    }
}
