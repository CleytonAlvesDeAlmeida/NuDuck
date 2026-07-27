package com.droidmonitor.settings

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONObject

/** Tema visual do app. SYSTEM segue o modo claro/escuro do Android. */
enum class AppTheme { LIGHT, DARK, SYSTEM }

/** Idioma da interface. SYSTEM segue o idioma do aparelho. */
enum class AppLanguage(val tag: String?, val displayName: String) {
    SYSTEM(null, "Automático (idioma do aparelho)"),
    PORTUGUESE("pt", "Português"),
    ENGLISH("en", "English"),
}

/**
 * Preferências do app, persistidas localmente no aparelho (não há conta nem
 * nuvem no NuDuck — tudo fica só no dispositivo, igual ao resto do projeto).
 */
data class AppSettings(
    /** Qualidade usada como padrão na próxima conexão ("144p".."1080p" ou "auto"). */
    val defaultQuality: String = "480p",
    /** Modo padrão ao conectar: "mirror" (espelhar) ou "extend" (segunda tela de verdade). */
    val defaultScreenMode: String = "mirror",
    /** Se o app deve enviar toques/cliques ao PC (o PC só executa se o
     *  checkbox "Permitir controle" dele também estiver marcado — este
     *  ajuste é só o lado do celular, útil para forçar modo só-visualização). */
    val sendControlEvents: Boolean = true,
    /** Lembra o PIN de cada PC já conectado com sucesso, evitando digitar
     *  de novo da próxima vez (fica só no aparelho, nunca sai daqui). */
    val rememberPins: Boolean = true,
    val theme: AppTheme = AppTheme.SYSTEM,
    val language: AppLanguage = AppLanguage.SYSTEM,
    /** Abre o NuDuck sozinho quando o celular liga (precisa da permissão de
     *  inicialização automática, que alguns fabricantes escondem numa tela
     *  própria de "apps com início automático"). */
    val launchOnBoot: Boolean = false,
)

/**
 * Wrapper fino sobre [SharedPreferences]. Leitura/escrita são baratas e
 * síncronas (poucos valores, chamadas raras — não precisa de DataStore aqui).
 */
class SettingsRepository(context: Context) {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun load(): AppSettings {
        val defaults = AppSettings()
        return AppSettings(
            defaultQuality = prefs.getString(KEY_QUALITY, null) ?: defaults.defaultQuality,
            defaultScreenMode = prefs.getString(KEY_MODE, null) ?: defaults.defaultScreenMode,
            sendControlEvents = prefs.getBoolean(KEY_CONTROL, defaults.sendControlEvents),
            rememberPins = prefs.getBoolean(KEY_REMEMBER_PIN, defaults.rememberPins),
            theme = prefs.getString(KEY_THEME, null)?.let { runCatching { AppTheme.valueOf(it) }.getOrNull() }
                ?: defaults.theme,
            language = prefs.getString(KEY_LANGUAGE, null)?.let { runCatching { AppLanguage.valueOf(it) }.getOrNull() }
                ?: defaults.language,
            launchOnBoot = prefs.getBoolean(KEY_LAUNCH_ON_BOOT, defaults.launchOnBoot),
        )
    }

    fun save(settings: AppSettings) {
        prefs.edit()
            .putString(KEY_QUALITY, settings.defaultQuality)
            .putString(KEY_MODE, settings.defaultScreenMode)
            .putBoolean(KEY_CONTROL, settings.sendControlEvents)
            .putBoolean(KEY_REMEMBER_PIN, settings.rememberPins)
            .putString(KEY_THEME, settings.theme.name)
            .putString(KEY_LANGUAGE, settings.language.name)
            .putBoolean(KEY_LAUNCH_ON_BOOT, settings.launchOnBoot)
            .apply()
    }

    // --- PINs lembrados por PC ("host:port" -> pin), só usado se rememberPins estiver ligado ---

    fun getSavedPin(pcKey: String): String? {
        if (!load().rememberPins) return null
        val raw = prefs.getString(KEY_SAVED_PINS, null) ?: return null
        return runCatching { JSONObject(raw).optString(pcKey).ifBlank { null } }.getOrNull()
    }

    fun savePin(pcKey: String, pin: String) {
        if (!load().rememberPins) return
        val raw = prefs.getString(KEY_SAVED_PINS, null)
        val json = runCatching { JSONObject(raw ?: "{}") }.getOrDefault(JSONObject())
        json.put(pcKey, pin)
        prefs.edit().putString(KEY_SAVED_PINS, json.toString()).apply()
    }

    /** Chamado quando o usuário desliga "lembrar PIN" ou quando um PIN salvo falha. */
    fun clearSavedPins() {
        prefs.edit().remove(KEY_SAVED_PINS).apply()
    }

    fun forgetPin(pcKey: String) {
        val raw = prefs.getString(KEY_SAVED_PINS, null) ?: return
        val json = runCatching { JSONObject(raw) }.getOrDefault(JSONObject())
        json.remove(pcKey)
        prefs.edit().putString(KEY_SAVED_PINS, json.toString()).apply()
    }

    companion object {
        private const val PREFS_NAME = "droidmonitor_settings"
        private const val KEY_QUALITY = "default_quality"
        private const val KEY_MODE = "default_screen_mode"
        private const val KEY_CONTROL = "send_control_events"
        private const val KEY_REMEMBER_PIN = "remember_pins"
        private const val KEY_THEME = "theme"
        private const val KEY_LANGUAGE = "language"
        private const val KEY_LAUNCH_ON_BOOT = "launch_on_boot"
        private const val KEY_SAVED_PINS = "saved_pins_json"
    }
}
