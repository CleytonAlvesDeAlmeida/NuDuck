package com.droidmonitor

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.droidmonitor.settings.SettingsRepository

private const val TAG = "BootReceiver"

/**
 * Recebe BOOT_COMPLETED e, só se "Iniciar ao ligar o celular" estiver
 * marcado nas Configurações, abre o NuDuck sozinho. Fica sempre registrado
 * no Manifest — quem decide se algo acontece é a preferência salva, não o
 * componente em si (mais simples e robusto do que habilitar/desabilitar o
 * receiver dinamicamente via PackageManager).
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return

        val settings = SettingsRepository(context).load()
        if (!settings.launchOnBoot) return

        Log.i(TAG, "Iniciando NuDuck automaticamente após o boot (preferência ativada).")
        val launchIntent = Intent(context, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(launchIntent)
    }
}
