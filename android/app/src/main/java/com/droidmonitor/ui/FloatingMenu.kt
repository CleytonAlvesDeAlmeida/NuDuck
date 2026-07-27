package com.droidmonitor.ui

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.HighQuality
import androidx.compose.material.icons.filled.LinkOff
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import com.droidmonitor.R
import kotlinx.coroutines.delay
import kotlin.math.roundToInt

/** Estados possíveis do menu flutuante do NuDuck durante a transmissão. */
private enum class MenuStage { COLLAPSED, EXPANDED, QUALITY }

private val MenuBlack = Color(0xFF0B0B0B)
private const val IDLE_TIMEOUT_MS = 3500L
private const val IDLE_ALPHA = 0.35f

/**
 * Menu flutuante exibido apenas durante a transmissão (dentro de `ConnectedScreen`).
 *
 * - Segurar e mover: arrasta o menu para qualquer ponto da tela.
 * - Tocar (sem arrastar): abre o menu quando fechado.
 * - Tocar fora do menu: fecha o menu (via scrim invisível, sem afetar o
 *   controle remoto do PC por trás do vídeo).
 * - Sem interação por [IDLE_TIMEOUT_MS]: o botão fechado esmaece até
 *   receber um novo toque ou arraste.
 */
@Composable
fun FloatingMenuHost(
    quality: String,
    qualityLabels: Map<String, String>,
    onQualityChange: (String) -> Unit,
    onOpenSettings: () -> Unit,
    onDisconnect: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var stage by remember { mutableStateOf(MenuStage.COLLAPSED) }
    var dragOffset by remember { mutableStateOf<Offset?>(null) } // null = posição inicial (topo-direita)
    var interactionTick by remember { mutableIntStateOf(0) }
    val density = LocalDensity.current

    fun markInteraction() { interactionTick++ }
    fun collapse() { stage = MenuStage.COLLAPSED }

    val alpha = remember { Animatable(1f) }
    LaunchedEffect(interactionTick, stage) {
        alpha.snapTo(1f)
        if (stage == MenuStage.COLLAPSED) {
            delay(IDLE_TIMEOUT_MS)
            alpha.animateTo(IDLE_ALPHA, animationSpec = tween(600))
        }
    }

    Box(modifier = modifier.fillMaxSize()) {
        // Scrim invisível: só existe quando o menu está aberto, captura o toque
        // "fora do menu" para fechá-lo sem repassar esse toque ao vídeo remoto.
        if (stage != MenuStage.COLLAPSED) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .pointerInput(Unit) {
                        detectTapGestures(onTap = {
                            markInteraction()
                            collapse()
                        })
                    },
            )
        }

        val initialX = with(density) { 24.dp.toPx() }
        val initialY = with(density) { 24.dp.toPx() }
        val current = dragOffset ?: Offset(initialX, initialY)

        Box(
            modifier = Modifier
                .align(Alignment.TopEnd)
                .offset { IntOffset(-current.x.roundToInt(), current.y.roundToInt()) }
                .pointerInput(Unit) {
                    detectDragGestures(
                        onDragStart = { markInteraction() },
                    ) { change, dragAmount ->
                        change.consume()
                        val base = dragOffset ?: Offset(initialX, initialY)
                        // x cresce para a esquerda pois o menu é ancorado no canto superior direito
                        dragOffset = Offset(
                            x = (base.x - dragAmount.x).coerceAtLeast(0f),
                            y = (base.y + dragAmount.y).coerceAtLeast(0f),
                        )
                        markInteraction()
                    }
                }
                .pointerInput(Unit) {
                    detectTapGestures(onTap = {
                        markInteraction()
                        if (stage == MenuStage.COLLAPSED) stage = MenuStage.EXPANDED
                    })
                },
        ) {
            when (stage) {
                MenuStage.COLLAPSED -> CollapsedButton(alpha = alpha.value)
                MenuStage.EXPANDED -> ExpandedPanel(
                    onQuality = { markInteraction(); stage = MenuStage.QUALITY },
                    onSettings = { markInteraction(); onOpenSettings() },
                    onDisconnect = { markInteraction(); onDisconnect() },
                )
                MenuStage.QUALITY -> QualityPanel(
                    quality = quality,
                    qualityLabels = qualityLabels,
                    onBack = { markInteraction(); stage = MenuStage.EXPANDED },
                    onSelect = { value -> markInteraction(); onQualityChange(value) },
                )
            }
        }
    }
}

@Composable
private fun CollapsedButton(alpha: Float) {
    Box(
        modifier = Modifier
            .size(48.dp)
            .background(MenuBlack.copy(alpha = alpha), RoundedCornerShape(10.dp)),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            painter = painterResource(id = R.drawable.ic_nuduck_logo),
            contentDescription = stringResource(R.string.menu_open_cd),
            tint = Color.White.copy(alpha = alpha),
            modifier = Modifier.size(width = 24.dp, height = 27.dp),
        )
    }
}

@Composable
private fun ExpandedPanel(
    onQuality: () -> Unit,
    onSettings: () -> Unit,
    onDisconnect: () -> Unit,
) {
    Column(
        modifier = Modifier
            .width(200.dp)
            .background(MenuBlack, RoundedCornerShape(14.dp))
            .padding(vertical = 8.dp),
    ) {
        MenuRow(icon = Icons.Filled.HighQuality, label = stringResource(R.string.menu_quality), onClick = onQuality)
        MenuRow(icon = Icons.Filled.Settings, label = stringResource(R.string.menu_settings), onClick = onSettings)
        MenuRow(icon = Icons.Filled.LinkOff, label = stringResource(R.string.menu_disconnect), onClick = onDisconnect, danger = true)
    }
}

@Composable
private fun MenuRow(icon: ImageVector, label: String, onClick: () -> Unit, danger: Boolean = false) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .tapClick(onClick)
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        Icon(
            imageVector = icon,
            contentDescription = null,
            tint = if (danger) Color(0xFFFF6B6B) else Color(0xFFE6E6E6),
            modifier = Modifier.size(20.dp),
        )
        Spacer(modifier = Modifier.size(12.dp))
        Text(text = label, color = if (danger) Color(0xFFFF6B6B) else Color.White)
    }
}

@Composable
private fun QualityPanel(
    quality: String,
    qualityLabels: Map<String, String>,
    onBack: () -> Unit,
    onSelect: (String) -> Unit,
) {
    Column(
        modifier = Modifier
            .width(220.dp)
            .background(MenuBlack, RoundedCornerShape(14.dp))
            .padding(vertical = 8.dp),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                .fillMaxWidth()
                .tapClick(onBack)
                .padding(horizontal = 16.dp, vertical = 10.dp),
        ) {
            Icon(
                imageVector = Icons.Filled.ArrowBack,
                contentDescription = stringResource(R.string.menu_back),
                tint = Color(0xFF9CA3AF),
                modifier = Modifier.size(18.dp),
            )
            Spacer(modifier = Modifier.size(8.dp))
            Text(text = stringResource(R.string.menu_back), color = Color(0xFF9CA3AF))
        }

        Column(
            modifier = Modifier
                .heightIn(max = 280.dp)
                .verticalScroll(rememberScrollState()),
        ) {
            qualityLabels.forEach { (value, label) ->
                QualityRow(label = label, selected = value == quality, onClick = { onSelect(value) })
            }
        }
    }
}

@Composable
private fun QualityRow(label: String, selected: Boolean, onClick: () -> Unit) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
        modifier = Modifier
            .fillMaxWidth()
            .tapClick(onClick)
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        Text(text = label, color = Color.White)
        if (selected) {
            // Reaproveita o ícone de "voltar" rotacionado como seta indicadora,
            // evitando adicionar mais um ícone só para este destaque.
            Icon(
                imageVector = Icons.Filled.ArrowBack,
                contentDescription = stringResource(R.string.menu_quality_selected_cd),
                tint = Color(0xFF38BDF8),
                modifier = Modifier
                    .size(16.dp)
                    .rotate(180f),
            )
        }
    }
}

/** Toque simples (tap) reutilizável para linhas de menu clicáveis. */
private fun Modifier.tapClick(onClick: () -> Unit): Modifier = this.pointerInput(Unit) {
    detectTapGestures(onTap = { onClick() })
}
