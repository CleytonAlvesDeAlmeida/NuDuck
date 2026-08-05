package com.droidmonitor.webrtc

import android.opengl.GLES11Ext
import android.opengl.GLES20
import org.webrtc.RendererCommon
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer

/**
 * Upscaling no lado do cliente (GPU do celular/tablet).
 *
 * Contexto: o servidor transmite a tela do PC numa resolução BAIXA de
 * propósito (a qualidade escolhida no app), pra economizar CPU do PC e
 * rede. É o celular quem amplia essa imagem pequena até preencher a
 * tela — isso já acontece de qualquer forma no WebRTC (ele sempre
 * desenha o vídeo esticado na tela usando a GPU/OpenGL). Esta classe
 * troca o "desenhista" (GlDrawer) padrão por um que, além de ampliar,
 * também aplica um filtro de nitidez em tempo real (GPU, sem custo
 * perceptível de CPU/bateria) pra compensar o borrão da ampliação.
 *
 * Técnica usada: **AMD CAS (Contrast Adaptive Sharpening)**, do
 * FidelityFX da AMD (código de referência público/MIT, documentado em
 * gpuopen.com). A diferença pro "unsharp mask" simples (versão
 * anterior desta classe) é que o CAS mede o CONTRASTE local ao redor
 * de cada pixel antes de decidir o quanto realçar — em áreas já
 * detalhadas/ruidosas ele realça pouco (evita amplificar ruído e criar
 * "auréolas" nas bordas), e em áreas mais lisas/borradas pela
 * ampliação ele realça mais. Resultado: uma imagem ampliada mais
 * nítida com efeitos colaterais visuais menores que o unsharp mask.
 *
 * Não é upscale "por IA" tipo super-resolução (isso pesaria demais pra
 * rodar em tempo real num celular comum); é um realce de nitidez
 * adaptativo ao contraste, sobre a ampliação normal.
 *
 * IMPORTANTE (histórico): a primeira versão desta classe estendia
 * `org.webrtc.GlGenericDrawer`, mas essa classe é interna da
 * biblioteca WebRTC (`package-private`) e não pode ser usada fora dela
 * — isso quebrou a build no Codemagic. Esta versão implementa a
 * interface pública `RendererCommon.GlDrawer` diretamente, usando só
 * APIs padrão do Android (`android.opengl.GLES20`/`GLES11Ext`), sem
 * depender de nada interno do WebRTC.
 */
class SharpUpscaleDrawer : RendererCommon.GlDrawer {

    // Força do realce de nitidez do CAS (0.0 = sem efeito, 1.0 = máximo).
    // Valores muito altos podem realçar ruído de compressão do vídeo —
    // 0.4-0.6 é uma faixa segura e visivelmente nítida.
    private val sharpness = 0.5f

    // Quadrado (2 triângulos via TRIANGLE_STRIP) cobrindo a tela toda.
    private val positionBuffer = floatBufferOf(-1f, -1f, 1f, -1f, -1f, 1f, 1f, 1f)
    private val texCoordBuffer = floatBufferOf(0f, 0f, 1f, 0f, 0f, 1f, 1f, 1f)

    private var oesProgram = 0
    private var rgbProgram = 0
    private var yuvProgram = 0

    override fun drawOes(
        oesTextureId: Int,
        texMatrix: FloatArray,
        frameWidth: Int,
        frameHeight: Int,
        viewportX: Int,
        viewportY: Int,
        viewportWidth: Int,
        viewportHeight: Int,
    ) {
        if (oesProgram == 0) {
            oesProgram = buildProgram(VERTEX_SHADER, sharpenFragmentShader(oes = true))
        }
        drawSingleTexture(
            oesProgram, GLES11Ext.GL_TEXTURE_EXTERNAL_OES, oesTextureId, texMatrix,
            frameWidth, frameHeight, viewportX, viewportY, viewportWidth, viewportHeight,
        )
    }

    override fun drawRgb(
        textureId: Int,
        texMatrix: FloatArray,
        frameWidth: Int,
        frameHeight: Int,
        viewportX: Int,
        viewportY: Int,
        viewportWidth: Int,
        viewportHeight: Int,
    ) {
        if (rgbProgram == 0) {
            rgbProgram = buildProgram(VERTEX_SHADER, sharpenFragmentShader(oes = false))
        }
        drawSingleTexture(
            rgbProgram, GLES20.GL_TEXTURE_2D, textureId, texMatrix,
            frameWidth, frameHeight, viewportX, viewportY, viewportWidth, viewportHeight,
        )
    }

    override fun drawYuv(
        yuvTextures: IntArray,
        texMatrix: FloatArray,
        frameWidth: Int,
        frameHeight: Int,
        viewportX: Int,
        viewportY: Int,
        viewportWidth: Int,
        viewportHeight: Int,
    ) {
        // Caminho raro (decodificação via software, sem aceleração de
        // hardware) — aqui só converte YUV->RGB e amplia, sem o realce
        // de nitidez extra, pra manter o código simples e confiável
        // nesse caso pouco comum.
        if (yuvProgram == 0) {
            yuvProgram = buildProgram(VERTEX_SHADER, YUV_FRAGMENT_SHADER)
        }
        GLES20.glUseProgram(yuvProgram)
        GLES20.glViewport(viewportX, viewportY, viewportWidth, viewportHeight)

        val names = arrayOf("y_tex", "u_tex", "v_tex")
        val units = intArrayOf(GLES20.GL_TEXTURE0, GLES20.GL_TEXTURE1, GLES20.GL_TEXTURE2)
        for (i in 0..2) {
            GLES20.glActiveTexture(units[i])
            GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, yuvTextures[i])
            GLES20.glUniform1i(GLES20.glGetUniformLocation(yuvProgram, names[i]), i)
        }

        val texMatrixLoc = GLES20.glGetUniformLocation(yuvProgram, "texMatrix")
        GLES20.glUniformMatrix4fv(texMatrixLoc, 1, false, texMatrix, 0)

        bindQuadAndDraw(yuvProgram)
    }

    override fun release() {
        listOf(oesProgram, rgbProgram, yuvProgram).forEach { program ->
            if (program != 0) GLES20.glDeleteProgram(program)
        }
        oesProgram = 0
        rgbProgram = 0
        yuvProgram = 0
    }

    // ---- Implementação interna (OpenGL ES 2.0 puro) ----

    private fun drawSingleTexture(
        program: Int,
        textureTarget: Int,
        textureId: Int,
        texMatrix: FloatArray,
        frameWidth: Int,
        frameHeight: Int,
        viewportX: Int,
        viewportY: Int,
        viewportWidth: Int,
        viewportHeight: Int,
    ) {
        GLES20.glUseProgram(program)
        GLES20.glViewport(viewportX, viewportY, viewportWidth, viewportHeight)

        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(textureTarget, textureId)
        GLES20.glUniform1i(GLES20.glGetUniformLocation(program, "tex"), 0)

        val texMatrixLoc = GLES20.glGetUniformLocation(program, "texMatrix")
        GLES20.glUniformMatrix4fv(texMatrixLoc, 1, false, texMatrix, 0)

        // Tamanho de 1 pixel da imagem ORIGINAL (pequena, recebida do
        // PC) em coordenadas de textura (0.0-1.0) — usado pelo shader
        // pra "olhar" para os pixels vizinhos e calcular o realce.
        val texelW = if (frameWidth > 0) 1f / frameWidth else 0f
        val texelH = if (frameHeight > 0) 1f / frameHeight else 0f
        GLES20.glUniform1f(GLES20.glGetUniformLocation(program, "texelW"), texelW)
        GLES20.glUniform1f(GLES20.glGetUniformLocation(program, "texelH"), texelH)
        GLES20.glUniform1f(GLES20.glGetUniformLocation(program, "sharpness"), sharpness)

        bindQuadAndDraw(program)

        GLES20.glBindTexture(textureTarget, 0)
    }

    private fun bindQuadAndDraw(program: Int) {
        val posLoc = GLES20.glGetAttribLocation(program, "inPosition")
        val tcLoc = GLES20.glGetAttribLocation(program, "inTexCoord")

        positionBuffer.position(0)
        GLES20.glVertexAttribPointer(posLoc, 2, GLES20.GL_FLOAT, false, 0, positionBuffer)
        GLES20.glEnableVertexAttribArray(posLoc)

        texCoordBuffer.position(0)
        GLES20.glVertexAttribPointer(tcLoc, 2, GLES20.GL_FLOAT, false, 0, texCoordBuffer)
        GLES20.glEnableVertexAttribArray(tcLoc)

        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)

        GLES20.glDisableVertexAttribArray(posLoc)
        GLES20.glDisableVertexAttribArray(tcLoc)
    }

    private fun buildProgram(vertexSource: String, fragmentSource: String): Int {
        val vertexShader = compileShader(GLES20.GL_VERTEX_SHADER, vertexSource)
        val fragmentShader = compileShader(GLES20.GL_FRAGMENT_SHADER, fragmentSource)

        val program = GLES20.glCreateProgram()
        GLES20.glAttachShader(program, vertexShader)
        GLES20.glAttachShader(program, fragmentShader)
        GLES20.glLinkProgram(program)

        val linkStatus = IntArray(1)
        GLES20.glGetProgramiv(program, GLES20.GL_LINK_STATUS, linkStatus, 0)
        if (linkStatus[0] == 0) {
            val log = GLES20.glGetProgramInfoLog(program)
            GLES20.glDeleteProgram(program)
            throw RuntimeException("NuDuck: falha ao linkar shader de upscale: $log")
        }

        GLES20.glDeleteShader(vertexShader)
        GLES20.glDeleteShader(fragmentShader)
        return program
    }

    private fun compileShader(type: Int, source: String): Int {
        val shader = GLES20.glCreateShader(type)
        GLES20.glShaderSource(shader, source)
        GLES20.glCompileShader(shader)

        val compileStatus = IntArray(1)
        GLES20.glGetShaderiv(shader, GLES20.GL_COMPILE_STATUS, compileStatus, 0)
        if (compileStatus[0] == 0) {
            val log = GLES20.glGetShaderInfoLog(shader)
            GLES20.glDeleteShader(shader)
            throw RuntimeException("NuDuck: falha ao compilar shader de upscale: $log")
        }
        return shader
    }

    private fun floatBufferOf(vararg values: Float): FloatBuffer {
        val buffer = ByteBuffer.allocateDirect(values.size * 4)
            .order(ByteOrder.nativeOrder())
            .asFloatBuffer()
        buffer.put(values)
        buffer.position(0)
        return buffer
    }

    companion object {
        private const val VERTEX_SHADER = """
            attribute vec4 inPosition;
            attribute vec2 inTexCoord;
            uniform mat4 texMatrix;
            varying vec2 vTexCoord;
            void main() {
                gl_Position = inPosition;
                vTexCoord = (texMatrix * vec4(inTexCoord, 0.0, 1.0)).xy;
            }
        """

        private fun sharpenFragmentShader(oes: Boolean): String {
            val header = if (oes) {
                "#extension GL_OES_EGL_image_external : require\n" +
                    "precision mediump float;\n" +
                    "uniform samplerExternalOES tex;\n"
            } else {
                "precision mediump float;\n" +
                    "uniform sampler2D tex;\n"
            }
            // Port do algoritmo de referência "CasFilter" do AMD FidelityFX
            // CAS (Contrast Adaptive Sharpening) — código original da AMD é
            // MIT/domínio público, documentado em gpuopen.com/fidelityfx-cas.
            // Adaptado aqui pra GLSL ES 2.0 (mobile), operando em RGB (o
            // conteúdo já vem convertido pra RGB antes de chegar neste
            // shader, seja de uma textura OES ou 2D comum).
            return header + """
                uniform float texelW;
                uniform float texelH;
                uniform float sharpness;
                varying vec2 vTexCoord;
                void main() {
                    // Vizinhança 3x3 ao redor do pixel central "e":
                    //   a b c
                    //   d e f
                    //   g h i
                    vec2 t = vec2(texelW, texelH);
                    vec3 a = texture2D(tex, vTexCoord + vec2(-t.x, -t.y)).rgb;
                    vec3 b = texture2D(tex, vTexCoord + vec2( 0.0, -t.y)).rgb;
                    vec3 c = texture2D(tex, vTexCoord + vec2( t.x, -t.y)).rgb;
                    vec3 d = texture2D(tex, vTexCoord + vec2(-t.x,  0.0)).rgb;
                    vec3 e = texture2D(tex, vTexCoord).rgb;
                    vec3 f = texture2D(tex, vTexCoord + vec2( t.x,  0.0)).rgb;
                    vec3 g = texture2D(tex, vTexCoord + vec2(-t.x,  t.y)).rgb;
                    vec3 h = texture2D(tex, vTexCoord + vec2( 0.0,  t.y)).rgb;
                    vec3 i = texture2D(tex, vTexCoord + vec2( t.x,  t.y)).rgb;

                    // Mínimo e máximo "suaves" (cruz + cantos, com peso
                    // maior pra cruz) — é isso que dá ao CAS a noção de
                    // CONTRASTE LOCAL, usada abaixo pra decidir o quanto
                    // realçar cada pixel sem exagerar em áreas ruidosas.
                    vec3 mnRGB = min(min(min(d, e), min(f, b)), h);
                    vec3 mnRGB2 = min(mnRGB, min(min(a, c), min(g, i)));
                    mnRGB += mnRGB2;

                    vec3 mxRGB = max(max(max(d, e), max(f, b)), h);
                    vec3 mxRGB2 = max(mxRGB, max(max(a, c), max(g, i)));
                    mxRGB += mxRGB2;

                    // Distância suave até o limite do sinal, dividida pelo
                    // máximo — quanto mais "achatado" (baixo contraste) o
                    // entorno, mais essa amplitude empurra o realce pra cima.
                    vec3 rcpMRGB = 1.0 / max(mxRGB, 0.0001);
                    vec3 ampRGB = clamp(min(mnRGB, 2.0 - mxRGB) * rcpMRGB, 0.0, 1.0);
                    ampRGB = inversesqrt(ampRGB + 0.0001);

                    float peak = 8.0 - 3.0 * clamp(sharpness, 0.0, 1.0);
                    vec3 wRGB = -(1.0 / (ampRGB * peak));
                    vec3 rcpWeightRGB = 1.0 / (4.0 * wRGB + 1.0);

                    vec3 window = (b + d) + (f + h);
                    vec3 sharpened = clamp((window * wRGB + e) * rcpWeightRGB, 0.0, 1.0);

                    gl_FragColor = vec4(sharpened, 1.0);
                }
            """
        }

        // Conversão YUV (BT.601) -> RGB, sem realce extra (ver drawYuv).
        private const val YUV_FRAGMENT_SHADER = """
            precision mediump float;
            uniform sampler2D y_tex;
            uniform sampler2D u_tex;
            uniform sampler2D v_tex;
            varying vec2 vTexCoord;
            void main() {
                float y = texture2D(y_tex, vTexCoord).r * 1.16438;
                float u = texture2D(u_tex, vTexCoord).r - 0.5;
                float v = texture2D(v_tex, vTexCoord).r - 0.5;
                gl_FragColor = vec4(
                    y + 1.59603 * v,
                    y - 0.39176 * u - 0.81297 * v,
                    y + 2.01723 * u,
                    1.0
                );
            }
        """
    }
}
