package com.droidmonitor.usb

import android.content.Context
import com.droidmonitor.webrtc.RemoteLog
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.Socket
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

private const val TAG = "UsbConnectionMonitor"
private const val SERVER_PORT = 8765
private const val POLL_INTERVAL_MS = 1500L

// Nomes típicos de interface de Ancoragem USB no Android — varia por
// fabricante/versão/kernel: rndis0 (o mais comum, USB RNDIS), usb0,
// ncm0 (USB NCM, mais recente), às vezes prefixados com "r_".
private val TETHER_INTERFACE_REGEX = Regex("(?i)^(r_)?(rndis|usb|ncm)\\d*$")

/**
 * Monitora a **Ancoragem USB** (USB tethering) entre o celular e o PC.
 *
 * Por que detectar por `NetworkInterface` e não por `ConnectivityManager`
 * -----------------------------------------------------------------------
 * A primeira versão deste monitor usava
 * `ConnectivityManager.registerNetworkCallback` com
 * `NetworkCapabilities.TRANSPORT_USB`. Isso NUNCA disparava — motivo: no
 * Android, `TRANSPORT_USB` descreve uma rede que o **celular** está
 * *consumindo* através de USB (ex.: um adaptador Ethernet-USB dando
 * internet PRO celular). O sentido oposto — o celular *provendo* rede
 * pro PC via Ancoragem USB — é gerenciado pelo subsistema de Tethering
 * do Android e não aparece como um `Network`/`TRANSPORT_USB` comum pra
 * apps consultarem da forma esperada. Resultado: `onAvailable()` nunca
 * era chamado, a varredura nunca rodava, e o app ficava preso em "PC não
 * encontrado" pra sempre, mesmo com a ancoragem ligada.
 *
 * A forma que realmente funciona: quando a Ancoragem USB está ativa, o
 * Android cria uma interface de rede local de verdade (tipicamente
 * `rndis0`, às vezes `usb0` ou `ncm0`) com um IP dentro do subnet de
 * ancoragem — e essa interface é visível via `NetworkInterface`, a API
 * padrão do Java, sem depender do `ConnectivityManager`. Um polling leve
 * (a cada 1.5s) detecta quando ela aparece/desaparece.
 *
 * Como o PC recebe um IP via DHCP dentro desse subnet (Android usa `/24`,
 * ex.: 192.168.42.0/24 ou 192.168.43.0/24 dependendo da versão) e esse IP
 * final não é previsível, o celular varre o subnet (no máximo 253 hosts,
 * em paralelo, timeout curto de 200ms) procurando quem responde na porta
 * 8765 do servidor. É rápido porque esse link só tem 2 pontas (celular e
 * PC) — normalmente encontra em menos de 1s.
 *
 * Nota sobre roteamento: não é preciso vincular o socket a uma interface
 * específica (como seria necessário com `Network.socketFactory`) — o
 * subnet da Ancoragem USB é uma rota diretamente conectada, então o
 * kernel do Android já escolhe a interface certa automaticamente por
 * longest-prefix-match, mesmo com Wi-Fi ativo ao mesmo tempo.
 */
class UsbConnectionMonitor(private val context: Context) {

    /** Disparado quando o estado muda: true = achou o PC via Ancoragem USB. */
    var onStateChange: ((Boolean) -> Unit)? = null

    private val scanExecutor = Executors.newFixedThreadPool(24)
    private val scanGeneration = AtomicInteger(0)

    @Volatile private var discoveredHost: String? = null
    @Volatile private var lastReported = false
    @Volatile private var running = false
    private var pollThread: Thread? = null

    /**
     * Inicia o monitor (polling em background). Idempotente.
     */
    fun start() {
        if (running) return
        running = true
        pollThread = Thread({
            var wasTethering = false
            while (running) {
                try {
                    val iface = findTetherInterface()
                    if (iface != null) {
                        val (netIface, addr) = iface
                        if (!wasTethering) {
                            RemoteLog.i(TAG, "Interface de Ancoragem USB detectada: ${netIface.name} ($addr)")
                            wasTethering = true
                        }
                        if (discoveredHost == null) {
                            scanSubnet(netIface.name, addr)
                        }
                    } else {
                        if (wasTethering) {
                            RemoteLog.i(TAG, "Ancoragem USB não detectada mais.")
                            wasTethering = false
                        }
                        if (discoveredHost != null) {
                            discoveredHost = null
                            scanGeneration.incrementAndGet()
                            maybeNotify()
                        }
                    }
                } catch (exc: Exception) {
                    RemoteLog.w(TAG, "Erro ao checar interfaces de rede: ${exc.message}")
                }
                try {
                    Thread.sleep(POLL_INTERVAL_MS)
                } catch (_: InterruptedException) {
                    break
                }
            }
        }, "UsbTetherPoll").apply {
            isDaemon = true
            start()
        }
    }

    /** Procura uma interface de rede que pareça ser Ancoragem USB, com IPv4 configurado. */
    private fun findTetherInterface(): Pair<NetworkInterface, Inet4Address>? {
        val interfaces = try {
            NetworkInterface.getNetworkInterfaces()
        } catch (_: Exception) {
            null
        } ?: return null

        for (iface in interfaces) {
            try {
                if (!iface.isUp || iface.isLoopback) continue
                if (!TETHER_INTERFACE_REGEX.matches(iface.name)) continue
                val addr = iface.inetAddresses.asSequence()
                    .filterIsInstance<Inet4Address>()
                    .firstOrNull() ?: continue
                return iface to addr
            } catch (_: Exception) {
                continue
            }
        }
        return null
    }

    /**
     * Varre o subnet /24 da interface de Ancoragem USB procurando o
     * servidor NuDuck na porta 8765.
     */
    private fun scanSubnet(ifaceName: String, myAddr: Inet4Address) {
        val octets = myAddr.address.map { it.toInt() and 0xFF }
        val myLastOctet = octets[3]
        val generation = scanGeneration.incrementAndGet()
        val found = AtomicBoolean(false)
        val subnetPrefix = "${octets[0]}.${octets[1]}.${octets[2]}"

        RemoteLog.i(TAG, "Varrendo $subnetPrefix.0/24 via $ifaceName em busca do PC (porta $SERVER_PORT)…")

        for (i in 1..254) {
            if (i == myLastOctet) continue  // não testa o próprio IP do celular
            val candidate = "$subnetPrefix.$i"
            scanExecutor.execute {
                if (generation != scanGeneration.get() || found.get()) return@execute
                try {
                    Socket().use { socket ->
                        // Rota diretamente conectada — o kernel escolhe a
                        // interface certa sozinho (ver nota na doc da classe).
                        socket.connect(InetSocketAddress(candidate, SERVER_PORT), 200)
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
        running = false
        pollThread?.interrupt()
        pollThread = null
        discoveredHost = null
        scanGeneration.incrementAndGet() // cancela qualquer varredura pendente
    }
}
