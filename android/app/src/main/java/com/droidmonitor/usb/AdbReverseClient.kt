package com.droidmonitor.usb

import com.droidmonitor.webrtc.RemoteLog
import java.net.InetSocketAddress
import java.net.Socket
import kotlin.math.min

private const val TAG = "AdbReverseClient"
private const val ADB_FORWARD_PORT = 8765  // porta padrão do servidor NuDuck
private const val ADB_LOCAL_PORT = 8765     // porta no celular (local)
private const val LOOPBACK = "127.0.0.1"    // localhost
private const val CONNECT_TIMEOUT_MS = 2000 // timeout pra conectar ao localhost

/**
 * Cliente ADB Reverse Tunneling para suportar conexão via "Depuração USB"
 * (quando o tablet está conectado via cabo USB com adb debugging ligado).
 *
 * Como funciona:
 * 1. Detecta se `adb` está disponível no caminho do sistema
 * 2. Executa: `adb reverse tcp:8765 tcp:8765`
 * 3. Redireciona conexões pra localhost:8765 → servidor remoto:8765
 * 4. Testa a conexão
 * 5. Se falhar, faz cleanup automático
 *
 * Benefício: Funciona quando APENAS Depuração USB está ativa (sem Ancoragem USB).
 * Fallback: Se ADB não estiver disponível, UsbConnectionMonitor tenta varredura.
 */
class AdbReverseClient {

    private var isReversed = false
    private var lastAdbPath: String? = null

    /**
     * Verifica se `adb` está disponível no sistema.
     * Testa em caminhos comuns (Android SDK, PATH, etc).
     */
    fun isAdbAvailable(): Boolean {
        // Primeiro, se já detectamos antes, reutilizar
        if (lastAdbPath != null) return true

        // Tentar caminhos comuns
        val possiblePaths = listOf(
            "adb",  // se estiver no PATH
            "/usr/bin/adb",  // Linux
            "/usr/local/bin/adb",  // macOS
            "C:\\platform-tools\\adb.exe",  // Windows
            "C:\\Android\\platform-tools\\adb.exe",  // Windows (alternativo)
            "/opt/android-sdk/platform-tools/adb",  // SDK path padrão
        )

        for (path in possiblePaths) {
            try {
                val process = Runtime.getRuntime().exec(arrayOf(path, "version"))
                val returnCode = process.waitFor()
                if (returnCode == 0) {
                    lastAdbPath = path
                    RemoteLog.i(TAG, "✓ adb encontrado em: $path")
                    return true
                }
            } catch (e: Exception) {
                // Não está neste path, tentar próximo
            }
        }

        RemoteLog.w(TAG, "✗ adb não encontrado — ADB reverse indisponível")
        return false
    }

    /**
     * Configura o reverse tunneling: `adb reverse tcp:8765 tcp:8765`
     * Com retry 3x e teste imediato de conexão.
     * Retorna true APENAS se a conexão foi testada e funcionou.
     */
    fun setupReverse(): Boolean {
        if (!isAdbAvailable()) {
            RemoteLog.w(TAG, "✗ ADB não disponível — reverse tunneling impossível")
            return false
        }

        val adbPath = lastAdbPath ?: return false
        
        repeat(3) { attempt ->
            try {
                RemoteLog.i(TAG, "🔄 Tentativa ${attempt + 1}/3 de ADB reverse...")
                
                val process = Runtime.getRuntime().exec(
                    arrayOf(adbPath, "reverse", "tcp:$ADB_FORWARD_PORT", "tcp:$ADB_LOCAL_PORT")
                )

                // Aguardar com timeout
                val completed = process.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)
                if (!completed) {
                    process.destroyForcibly()
                    RemoteLog.w(TAG, "⏱️ ADB reverse timeout (tentativa ${attempt + 1})")
                    return@repeat
                }

                val returnCode = process.exitValue()
                if (returnCode != 0) {
                    val error = process.errorStream.bufferedReader().readText()
                    RemoteLog.w(TAG, "⚠️ ADB reverse falhou (código $returnCode): $error")
                    return@repeat
                }

                RemoteLog.i(TAG, "✓ ADB reverse setup OK")
                
                // Testar IMEDIATAMENTE (não confiar cegamente)
                if (testConnection()) {
                    isReversed = true
                    RemoteLog.i(TAG, "✓✓ ADB reverse FUNCIONANDO (teste passou)")
                    return true  // SUCESSO!
                } else {
                    RemoteLog.w(TAG, "❌ ADB reverse setup OK mas teste de conexão falhou")
                    teardownReverse()  // cleanup se teste falhou
                }
            } catch (e: Exception) {
                RemoteLog.w(TAG, "❌ Erro ADB reverse tentativa ${attempt + 1}: ${e.message}")
            }
            
            // Aguardar antes de retry (exceto na última)
            if (attempt < 2) {
                try {
                    Thread.sleep(500)
                } catch (_: InterruptedException) {
                }
            }
        }
        
        RemoteLog.e(TAG, "✗ ADB reverse FALHOU após 3 tentativas")
        return false
    }

    /**
     * Testa se consegue se conectar a localhost:8765
     * Simula o que o WebRTC va fazer.
     */
    private fun testConnection(): Boolean {
        try {
            Socket().use { socket ->
                RemoteLog.i(TAG, "Testando conexão em $LOOPBACK:$ADB_FORWARD_PORT...")
                socket.connect(InetSocketAddress(LOOPBACK, ADB_FORWARD_PORT), CONNECT_TIMEOUT_MS)
                RemoteLog.i(TAG, "✓ Teste de conexão bem-sucedido")
                return true
            }
        } catch (e: Exception) {
            RemoteLog.w(TAG, "Teste de conexão falhou: ${e.message}")
            return false
        }
    }

    /**
     * Remove o reverse tunneling: `adb reverse --remove tcp:8765`
     * Chamado no cleanup.
     */
    fun teardownReverse() {
        if (!isReversed) return

        val adbPath = lastAdbPath ?: return

        try {
            RemoteLog.i(TAG, "Removendo ADB reverse")
            val process = Runtime.getRuntime().exec(
                arrayOf(adbPath, "reverse", "--remove", "tcp:$ADB_FORWARD_PORT")
            )
            process.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)
            isReversed = false
            RemoteLog.i(TAG, "✓ ADB reverse removido")
        } catch (e: Exception) {
            RemoteLog.w(TAG, "Erro ao remover ADB reverse: ${e.message}")
        }
    }

    /**
     * Limpa tudo. Chamado em onCleared() do ViewModel.
     */
    fun cleanup() {
        teardownReverse()
    }
}
