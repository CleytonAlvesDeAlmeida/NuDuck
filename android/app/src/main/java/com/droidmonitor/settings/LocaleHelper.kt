package com.droidmonitor.settings

import androidx.appcompat.app.AppCompatDelegate
import androidx.core.os.LocaleListCompat

/**
 * Aplica o idioma escolhido nas Configurações usando o mecanismo de
 * "idioma por app" do AndroidX (funciona a partir do Android 6, e sem
 * precisar que a Activity herde de AppCompatActivity — o appcompat 1.6+
 * intercepta o ciclo de vida de qualquer Activity para isso).
 */
object LocaleHelper {
    fun apply(language: AppLanguage) {
        val locales = if (language.tag == null) {
            LocaleListCompat.getEmptyLocaleList() // "Automático": segue o idioma do aparelho
        } else {
            LocaleListCompat.forLanguageTags(language.tag)
        }
        AppCompatDelegate.setApplicationLocales(locales)
    }
}
