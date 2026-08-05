package com.droidmonitor.ui

import android.app.Activity
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat

/**
 * Liga/desliga o modo imersivo "sticky" na Activity dona da janela atual.
 *
 * - `enterImmersiveMode`: esconde barra de status + barra de navegação,
 *   desenha o conteúdo por trás delas (edge-to-edge) e configura o
 *   comportamento BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE — basta deslizar
 *   a partir da borda para as barras aparecerem temporariamente, e elas
 *   somem sozinhas de novo. É o comportamento recomendado para players de
 *   vídeo e jogos, exatamente o caso da transmissão do NuDuck.
 *
 * - `exitImmersiveMode`: reverte tudo. Usado ao sair de ConnectedScreen
 *   (volta pra Discovery) ou ao abrir o dialog de configurações.
 *
 * A Activity não é recriada em rotação (configChanges no Manifest), então
 * o estado imersivo persiste naturalmente entre mudanças de orientação —
 * não precisa restaurar manualmente.
 */
@Composable
fun ImmersiveModeEffect(active: Boolean) {
    androidx.compose.ui.platform.LocalView.current.let { view ->
        DisposableEffect(active, view) {
            val activity = view.context as? Activity
            if (active && activity != null) {
                val window = activity.window
                // Edge-to-edge: o conteúdo desenha atrás das system bars.
                WindowCompat.setDecorFitsSystemWindows(window, false)
                val controller = WindowInsetsControllerCompat(window, view)
                controller.hide(WindowInsetsCompat.Type.systemBars())
                controller.systemBarsBehavior =
                    WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }

            onDispose {
                val act = view.context as? Activity
                if (act != null) {
                    val window = act.window
                    WindowCompat.setDecorFitsSystemWindows(window, true)
                    val controller = WindowInsetsControllerCompat(window, view)
                    controller.show(WindowInsetsCompat.Type.systemBars())
                }
            }
        }
    }
}
