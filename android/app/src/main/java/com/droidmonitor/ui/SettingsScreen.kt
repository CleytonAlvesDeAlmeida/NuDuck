package com.droidmonitor.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.Divider
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.droidmonitor.R
import com.droidmonitor.settings.AppLanguage
import com.droidmonitor.settings.AppSettings
import com.droidmonitor.settings.AppTheme

private val QUALITY_VALUES = listOf("144p", "240p", "360p", "480p", "720p", "1080p", "auto")

/**
 * Tela de Configurações do NuDuck.
 *
 * Usada de dois jeitos:
 * - Tela cheia, a partir da Descoberta (antes de conectar em um PC), pelo ícone de engrenagem.
 * - Dentro de um Dialog flutuante, aberta pelo menu durante o espelhamento/extensão
 *   (sem interromper a transmissão, já que o vídeo continua rodando atrás).
 */
@Composable
fun SettingsScreen(
    settings: AppSettings,
    onSettingsChange: (AppSettings) -> Unit,
    onClose: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .verticalScroll(rememberScrollState()),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 8.dp, vertical = 8.dp),
        ) {
            IconButton(onClick = onClose) {
                Icon(
                    imageVector = Icons.Filled.ArrowBack,
                    contentDescription = stringResource(R.string.settings_back_cd),
                    tint = MaterialTheme.colorScheme.onBackground,
                )
            }
            Text(
                text = stringResource(R.string.settings_title),
                style = MaterialTheme.typography.titleLarge,
                color = MaterialTheme.colorScheme.onBackground,
            )
        }

        Spacer(modifier = Modifier.height(8.dp))

        SettingsSection(title = stringResource(R.string.settings_section_video)) {
            QualityPicker(
                selected = settings.defaultQuality,
                onSelect = { onSettingsChange(settings.copy(defaultQuality = it)) },
            )
        }

        Divider(color = MaterialTheme.colorScheme.surfaceVariant)

        SettingsSection(title = stringResource(R.string.settings_section_screen_mode)) {
            ScreenModePicker(
                selected = settings.defaultScreenMode,
                onSelect = { onSettingsChange(settings.copy(defaultScreenMode = it)) },
            )
        }

        Divider(color = MaterialTheme.colorScheme.surfaceVariant)

        SettingsSection(title = stringResource(R.string.settings_section_control)) {
            SwitchRow(
                title = stringResource(R.string.settings_control_label),
                subtitle = stringResource(R.string.settings_control_hint),
                checked = settings.sendControlEvents,
                onCheckedChange = { onSettingsChange(settings.copy(sendControlEvents = it)) },
            )
            SwitchRow(
                title = stringResource(R.string.settings_remember_pin_label),
                subtitle = stringResource(R.string.settings_remember_pin_hint),
                checked = settings.rememberPins,
                onCheckedChange = { onSettingsChange(settings.copy(rememberPins = it)) },
            )
        }

        Divider(color = MaterialTheme.colorScheme.surfaceVariant)

        SettingsSection(title = stringResource(R.string.settings_section_appearance)) {
            ThemePicker(
                selected = settings.theme,
                onSelect = { onSettingsChange(settings.copy(theme = it)) },
            )
            LanguagePicker(
                selected = settings.language,
                onSelect = { onSettingsChange(settings.copy(language = it)) },
            )
        }

        Divider(color = MaterialTheme.colorScheme.surfaceVariant)

        SettingsSection(title = stringResource(R.string.settings_section_startup)) {
            SwitchRow(
                title = stringResource(R.string.settings_launch_on_boot_label),
                subtitle = stringResource(R.string.settings_launch_on_boot_hint),
                checked = settings.launchOnBoot,
                onCheckedChange = { onSettingsChange(settings.copy(launchOnBoot = it)) },
            )
        }

        Divider(color = MaterialTheme.colorScheme.surfaceVariant)

        Text(
            text = stringResource(R.string.settings_about_text),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp),
        )

        Spacer(modifier = Modifier.height(24.dp))
    }
}

@Composable
private fun SettingsSection(title: String, content: @Composable () -> Unit) {
    Column(modifier = Modifier.widthIn(max = 640.dp)) {
        Text(
            text = title,
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.primary,
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
        )
        content()
    }
}

@Composable
private fun SwitchRow(
    title: String,
    subtitle: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onCheckedChange(!checked) }
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(title, color = MaterialTheme.colorScheme.onBackground, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
        }
        Spacer(modifier = Modifier.height(0.dp))
        Switch(
            checked = checked,
            onCheckedChange = onCheckedChange,
            colors = SwitchDefaults.colors(checkedTrackColor = MaterialTheme.colorScheme.primary),
        )
    }
}

/** Linha clicável com um valor à direita que abre um DropdownMenu — reaproveitada
 *  pelos seletores de qualidade, modo de tela, tema e idioma. */
@Composable
private fun PickerRow(
    title: String,
    subtitle: String?,
    valueLabel: String,
    menu: @Composable (expanded: Boolean, onDismiss: () -> Unit) -> Unit,
) {
    var expanded by remember { mutableStateOf(false) }

    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
        modifier = Modifier
            .fillMaxWidth()
            .clickable { expanded = true }
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(title, color = MaterialTheme.colorScheme.onBackground, style = MaterialTheme.typography.bodyLarge)
            subtitle?.let {
                Text(it, color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
            }
        }
        Box {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .background(MaterialTheme.colorScheme.surface, RoundedCornerShape(8.dp))
                    .padding(horizontal = 12.dp, vertical = 8.dp),
            ) {
                Text(valueLabel, color = MaterialTheme.colorScheme.primary)
            }
            menu(expanded) { expanded = false }
        }
    }
}

@Composable
private fun QualityPicker(selected: String, onSelect: (String) -> Unit) {
    val autoLabel = stringResource(R.string.settings_quality_auto)
    val label = if (selected == "auto") autoLabel else selected
    PickerRow(
        title = stringResource(R.string.settings_quality_label),
        subtitle = stringResource(R.string.settings_quality_hint),
        valueLabel = label,
    ) { expanded, dismiss ->
        DropdownMenu(expanded = expanded, onDismissRequest = dismiss) {
            QUALITY_VALUES.forEach { value ->
                DropdownMenuItem(
                    text = { Text(if (value == "auto") autoLabel else value) },
                    onClick = { dismiss(); onSelect(value) },
                )
            }
        }
    }
}

@Composable
private fun ScreenModePicker(selected: String, onSelect: (String) -> Unit) {
    val mirrorLabel = stringResource(R.string.pin_entry_mode_mirror)
    val extendLabel = stringResource(R.string.pin_entry_mode_extend)
    val label = if (selected == "extend") extendLabel else mirrorLabel
    PickerRow(
        title = stringResource(R.string.settings_mode_label),
        subtitle = stringResource(R.string.settings_mode_hint),
        valueLabel = label,
    ) { expanded, dismiss ->
        DropdownMenu(expanded = expanded, onDismissRequest = dismiss) {
            DropdownMenuItem(text = { Text(mirrorLabel) }, onClick = { dismiss(); onSelect("mirror") })
            DropdownMenuItem(text = { Text(extendLabel) }, onClick = { dismiss(); onSelect("extend") })
        }
    }
}

@Composable
private fun ThemePicker(selected: AppTheme, onSelect: (AppTheme) -> Unit) {
    val labels = mapOf(
        AppTheme.SYSTEM to stringResource(R.string.settings_theme_system),
        AppTheme.LIGHT to stringResource(R.string.settings_theme_light),
        AppTheme.DARK to stringResource(R.string.settings_theme_dark),
    )
    PickerRow(
        title = stringResource(R.string.settings_theme_label),
        subtitle = null,
        valueLabel = labels.getValue(selected),
    ) { expanded, dismiss ->
        DropdownMenu(expanded = expanded, onDismissRequest = dismiss) {
            labels.forEach { (value, label) ->
                DropdownMenuItem(text = { Text(label) }, onClick = { dismiss(); onSelect(value) })
            }
        }
    }
}

@Composable
private fun LanguagePicker(selected: AppLanguage, onSelect: (AppLanguage) -> Unit) {
    PickerRow(
        title = stringResource(R.string.settings_language_label),
        subtitle = null,
        valueLabel = selected.displayName,
    ) { expanded, dismiss ->
        DropdownMenu(expanded = expanded, onDismissRequest = dismiss) {
            AppLanguage.values().forEach { value ->
                DropdownMenuItem(text = { Text(value.displayName) }, onClick = { dismiss(); onSelect(value) })
            }
        }
    }
}
