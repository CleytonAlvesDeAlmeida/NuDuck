package com.droidmonitor

import android.app.Application
import com.droidmonitor.settings.LocaleHelper
import com.droidmonitor.settings.SettingsRepository

/**
 * Aplica o idioma salvo (ver Configurações) o mais cedo possível, antes de
 * qualquer Activity ser criada — evita um "flash" com o idioma errado na
 * primeira tela.
 */
class DroidMonitorApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        val settings = SettingsRepository(this).load()
        LocaleHelper.apply(settings.language)
    }
}
