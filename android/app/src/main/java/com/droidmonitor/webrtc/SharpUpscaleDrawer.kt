package com.droidmonitor.webrtc

import android.opengl.GLES20
import org.webrtc.GlGenericDrawer
import org.webrtc.GlShader

/**
 * Upscaling no lado do cliente (GPU do celular/tablet).
 *
 * Contexto: o servidor agora transmite a tela do PC numa resolução
 * BAIXA de propósito (a qualidade escolhida no app — 144p a 1080p),
 * porque codificar/enviar em alta resolução é o que mais pesa a CPU do
 * PC e a rede (ver server.py: ele não amplia mais o frame antes de
 * codificar). Isso, por si só, já faz o SurfaceViewRenderer do WebRTC
 * ampliar a imagem pequena até preencher a tela do celular usando a
 * GPU (OpenGL) — é assim que o WebRTC sempre desenhou vídeo, só que
 * antes a imagem já chegava do PC no tamanho da tela, então essa
 * ampliação nunca era exercitada de verdade.
 *
 * Esta classe substitui o desenhista (GlDrawer) padrão do WebRTC por
 * um que, além de ampliar (essa parte a GPU já faz sozinha ao desenhar
 * um retângulo maior), também aplica um filtro de nitidez em tempo
 * real (GPU shader) para compensar o borrão da ampliação — uma técnica
 * clássica de "unsharp mask" (realce de bordas), rodando a cada frame,
 * a 100% na GPU (custo praticamente zero na CPU/bateria).
 *
 * Importante ser honesto sobre o que isso é e não é: não é um upscale
 * "por IA" tipo super-resolução (isso exigiria um modelo de rede
 * neural rodando por frame, pesado demais para tempo real num
 * celular comum); é um realce de nitidez leve aplicado sobre a
 * ampliação normal, que reduz a sensação de borrão/desfoque sem
 * consumir GPU/bateria de forma perceptível.
 */
class SharpUpscaleDrawer : GlGenericDrawer(FRAGMENT_SHADER, ShaderCallbacksImpl()) {

    private class ShaderCallbacksImpl : ShaderCallbacks {
        private var texelWidthLoc = -1
        private var texelHeightLoc = -1
        private var amountLoc = -1

        override fun onNewShader(shader: GlShader) {
            texelWidthLoc = shader.getUniformLocation("texelW")
            texelHeightLoc = shader.getUniformLocation("texelH")
            amountLoc = shader.getUniformLocation("sharpenAmount")
        }

        override fun onPrepareShader(
            shader: GlShader,
            texMatrix: FloatArray,
            frameWidth: Int,
            frameHeight: Int,
            viewportWidth: Int,
            viewportHeight: Int,
        ) {
            // Tamanho de 1 pixel da imagem ORIGINAL (pequena, recebida do
            // PC) em coordenadas de textura (0.0-1.0) — é isso que o
            // shader usa para "olhar" para os pixels vizinhos.
            val texelW = if (frameWidth > 0) 1.0f / frameWidth else 0f
            val texelH = if (frameHeight > 0) 1.0f / frameHeight else 0f
            GLES20.glUniform1f(texelWidthLoc, texelW)
            GLES20.glUniform1f(texelHeightLoc, texelH)
            // Força do realce de nitidez. Valores maiores = mais nítido,
            // mas com risco de "auréolas" nas bordas se exagerar.
            // 0.20-0.30 é um meio-termo seguro para compensar upscale.
            GLES20.glUniform1f(amountLoc, SHARPEN_AMOUNT)
        }
    }

    companion object {
        private const val SHARPEN_AMOUNT = 0.25f

        // Roda depois da função sample(tc), que o GlGenericDrawer já
        // define pra gente (ela devolve a cor do pixel na coordenada
        // "tc", já convertida pra RGB não importa se a fonte original
        // era OES/YUV/RGB). "tc" (varying) também já vem pronta.
        private const val FRAGMENT_SHADER = """
            uniform float texelW;
            uniform float texelH;
            uniform float sharpenAmount;
            void main() {
                vec4 center = sample(tc);
                vec4 top    = sample(tc + vec2(0.0, -texelH));
                vec4 bottom = sample(tc + vec2(0.0,  texelH));
                vec4 left   = sample(tc + vec2(-texelW, 0.0));
                vec4 right  = sample(tc + vec2( texelW, 0.0));
                vec4 sharpened = center * (1.0 + 4.0 * sharpenAmount)
                    - (top + bottom + left + right) * sharpenAmount;
                gl_FragColor = clamp(sharpened, 0.0, 1.0);
            }
        """
    }
}
