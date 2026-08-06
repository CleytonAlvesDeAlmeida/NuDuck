package com.droidmonitor.util

/**
 * Valida hosts (IPs ou hostnames) antes de usar em conexões.
 * Evita conectar a hosts inválidos ou corrompidos.
 */
object HostValidator {
    fun isValidHost(host: String?): Boolean {
        if (host == null || host.isBlank()) return false
        
        // Não permitir caracteres especiais perigosos
        if (host.contains("/") || host.contains("\\") || host.contains(" ")) return false
        
        // IPv6 link-local não funciona bem (contém %)
        if (host.contains("%")) return false
        
        // IPv6 entre colchetes é ok: [::1]
        if (host.startsWith("[") && host.contains("]")) return true
        
        // IPv4 ou hostname: apenas números, letras, pontos, hífens, dois-pontos
        val pattern = Regex("^[a-zA-Z0-9.:-]+$")
        return pattern.matches(host)
    }
}
