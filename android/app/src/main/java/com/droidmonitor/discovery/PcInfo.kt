package com.droidmonitor.discovery

/** Representa um PC DroidMonitor encontrado na rede local via mDNS. */
data class PcInfo(
    val name: String,
    val host: String,
    val port: Int,
)
