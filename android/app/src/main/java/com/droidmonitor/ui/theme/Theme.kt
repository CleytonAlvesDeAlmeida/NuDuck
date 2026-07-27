package com.droidmonitor.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import com.droidmonitor.settings.AppTheme

private val DarkColors = darkColorScheme(
    primary = Color(0xFF38BDF8),
    secondary = Color(0xFF0EA5E9),
    tertiary = Color(0xFFEC5E00), // laranja do logo NuDuck, usado em destaques pontuais
    background = Color(0xFF0F172A),
    surface = Color(0xFF0B0B0B), // preto do topo/menus, igual aos mockups
    onPrimary = Color.Black,
    onBackground = Color.White,
    onSurface = Color.White,
)

// Paleta clara equivalente, para quem prefere (ou o sistema está em modo
// claro e a preferência é "Automático"). Mantém os mesmos tons de destaque
// (azul/laranja do logo) só invertendo fundo/superfície/texto.
private val LightColors = lightColorScheme(
    primary = Color(0xFF0284C7),
    secondary = Color(0xFF0EA5E9),
    tertiary = Color(0xFFEC5E00),
    background = Color(0xFFF8FAFC),
    surface = Color(0xFFFFFFFF),
    onPrimary = Color.White,
    onBackground = Color(0xFF0F172A),
    onSurface = Color(0xFF0F172A),
)

@Composable
fun DroidMonitorTheme(theme: AppTheme = AppTheme.SYSTEM, content: @Composable () -> Unit) {
    val useDark = when (theme) {
        AppTheme.DARK -> true
        AppTheme.LIGHT -> false
        AppTheme.SYSTEM -> isSystemInDarkTheme()
    }
    MaterialTheme(
        colorScheme = if (useDark) DarkColors else LightColors,
        content = content,
    )
}
