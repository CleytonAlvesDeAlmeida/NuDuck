package com.droidmonitor.usb

import android.content.Context
import com.droidmonitor.util.HostValidator
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
// BUG FIX USB: reduzir polling de 1500ms → 500ms
// Motivo: Tablet sem WiFi/BT precisa reconhecer USB rápido (depuração USB é lento)
private const val POLL_INTERVAL_MS = 500L
// BUG FIX USB: aumentar timeout de 200ms para 1500ms
// Motivo: USB tethering inicial pode ser MUI lento, especialmente em tablets
// Com depuração USB, precisamos ser pacientes na primeira tentativa
private const val CONNECT_TIMEOUT_MS = 1500

// BUG FIX USB: Regex MUITO mais permissiva
// Nomes de interface variam insanamente entre fabricantes:
// - rndis0 (USB RNDIS, o mais comum)
// - usb0, usb1, etc (USB genérico)
// - ncm0, ncm-wwan0 (Qualcomm USB NCM)
// - eth0, eth1 (alguns tablets antigos)
// - usbnet0, usbnet1 (alguns Samsung)
// - neth0, neth1 (alguns Huawei)
// - bridge0, br0 (USB bridge mode)
// - ppp0, ppp1, wwan0 (alguns modems USB antigos)
// - tether0, tether1 (nome genérico)
// - ip0, ip1 (alguns 5G modems)
// Com prefixos opcionais "r_", "rev_", etc
// Ser permissivo é melhor do que perder uma interface legítima
private val TETHER_INTERFACE_REGEX = Regex(
    "(?i)^(r_|rev_)?" +  // prefixo opcional
    "(rndis|usb|ncm|neth|usbnet|eth|pan|bridge|ppp|wwan|br|tether|ip)" +  // nomes base
    "\\d*" +  // número da interface
    "(-wwan|-data)?$"  // sufixos opcionais
)

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
    private val adbReverseClient = AdbReverseClient()  // BUG FIX USB: ADB reverse fallback

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
            var adbReverseTried = false  // BUG FIX USB: evitar loop infinito de ADB
            
            while (running) {
                try {
                    val iface = findTetherInterface()
                    if (iface != null) {
                        val (netIface, addr) = iface
                        if (!wasTethering) {
                            RemoteLog.i(TAG, "✓ Interface Ancoragem USB detectada: ${netIface.name} (${addr.hostAddress})")
                            wasTethering = true
                            adbReverseTried = false  // resetar flag, nova oportunidade
                        }
                        if (discoveredHost == null) {
                            scanSubnet(netIface.name, addr)
                        }
                    } else {
                        if (wasTethering) {
                            RemoteLog.i(TAG, "✗ Ancoragem USB desconectada")
                            wasTethering = false
                        }
                        
                        // BUG FIX USB: Se USB falhou e não tentamos ADB ainda, tentar
                        if (discoveredHost == null && !adbReverseTried) {
                            RemoteLog.i(TAG, "Sem interface USB — tentando ADB reverse como fallback...")
                            if (adbReverseClient.setupReverse()) {
                                discoveredHost = "127.0.0.1"  // localhost via ADB reverse
                                RemoteLog.i(TAG, "✓ ADB reverse ativado: localhost:$SERVER_PORT")
                                maybeNotify()
                                adbReverseTried = true
                            } else {
                                RemoteLog.w(TAG, "ADB reverse não disponível ou falhou")
                                adbReverseTried = true  // não tentar de novo neste ciclo
                            }
                        } else if (discoveredHost != null) {
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
                        // CORREÇÃO USB: timeout de 1000ms em vez de 200ms
                        socket.connect(InetSocketAddress(candidate, SERVER_PORT), CONNECT_TIMEOUT_MS)
                        if (found.compareAndSet(false, true) && generation == scanGeneration.get()) {
                            // BUG FIX: validar host antes de usar
                            if (!HostValidator.isValidHost(candidate)) {
                                RemoteLog.w(TAG, "⚠️ Host inválido encontrado: '$candidate'")
                                found.set(false)
                                return@execute
                            }
                            
                            discoveredHost = candidate
                            RemoteLog.i(TAG, "✓ PC encontrado via cabo em $candidate:$SERVER_PORT")
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
        // BUG FIX USB: cleanup de ADB reverse
        adbReverseClient.cleanup()
        RemoteLog.i(TAG, "Monitor de Ancoragem USB parado")
    }
}
