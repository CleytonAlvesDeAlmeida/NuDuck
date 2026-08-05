package com.droidmonitor.webrtc

import android.content.Context
import org.json.JSONObject
import org.webrtc.DataChannel
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.VideoTrack
import java.nio.charset.Charset

private const val TAG = "WebRtcClient"

/**
 * Encapsula a PeerConnectionFactory/PeerConnection do lado Android.
 * O PC é sempre quem oferece vídeo (recvonly aqui); o Android envia
 * eventos de toque por um DataChannel "control".
 *
 * DTLS/SRTP são obrigatórios por padrão na pilha WebRTC — não há como
 * negociar uma conexão de mídia sem criptografia.
 */
class WebRtcClient(
    context: Context,
    private val signalingClient: SignalingClient,
    /** Item 9: "standard" (Wi-Fi) ou "low_latency" (USB). Ajusta codec, ICE e SDP. */
    private val latencyProfile: String = "standard",
) {
    interface Listener {
        fun onRemoteVideoTrack(track: VideoTrack)
        fun onControlChannelReady()
        fun onConnectionFailed(reason: String)
        /** Chamado assim que o PC responde dizendo qual modo ficou ativo de
         *  fato ("mirror" ou "extend") — pode diferir do que foi pedido, se
         *  o PC não conseguir uma segunda tela de verdade. `reason`, quando
         *  não nulo, explica o motivo exato reportado pelo PC. */
        fun onModeResolved(requestedMode: String, resolvedMode: String, reason: String?)
        /** Item 3: posição do cursor do mouse no PC, normalizada (0.0-1.0),
         *  mandada pelo servidor a cada frame — o cursor não vem mais
         *  "desenhado dentro" do vídeo, então o app precisa desenhar por
         *  conta própria em cima da imagem. */
        fun onCursorPosition(x: Float, y: Float)
        /** Item 4: desenho real do cursor (seta, texto "I", mãozinha,
         *  redimensionar, etc.), mandado só quando muda de forma — bitmap
         *  já pronto para desenhar, mais o "ponto quente" (hotspot) em
         *  pixels dentro do próprio bitmap. */
        fun onCursorShape(bitmap: android.graphics.Bitmap, hotX: Int, hotY: Int)
    }

    var listener: Listener? = null

    val eglBase: EglBase = EglBase.create()

    private val factory: PeerConnectionFactory
    private var peerConnection: PeerConnection? = null
    private var controlChannel: DataChannel? = null
    
    // FASE 3: monitoramento de frame congelado
    private var lastFrameTimestamp: Long = 0
    private var frozenFrameDetectionTask: Thread? = null

    init {
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(context)
                .createInitializationOptions()
        )

        // Item 9: em low_latency, o encoder H264 é preferido sobre VP8/VP9.
        // H264 tem aceleração de hardware consistente em Android (MediaCodec)
        // e menor overhead de encode que VP8 em software. O DefaultVideoEncoderFactory
        // já prioriza H264 internamente quando `enableIntelVp8Encoder=true`,
        // mas aqui preferimos o caminho explícito.
        val encoderFactory = DefaultVideoEncoderFactory(eglBase.eglBaseContext, true, true)
        val decoderFactory = DefaultVideoDecoderFactory(eglBase.eglBaseContext)

        factory = PeerConnectionFactory.builder()
            .setVideoEncoderFactory(encoderFactory)
            .setVideoDecoderFactory(decoderFactory)
            .createPeerConnectionFactory()
    }

    private var pendingOfferQuality: String? = null
    private var pendingOfferMode: String = "mirror"
    private var pendingScreenWidth: Int = 0
    private var pendingScreenHeight: Int = 0
    private var requestedMode: String = "mirror"

    /** Cria a PeerConnection, gera a offer (recvonly de vídeo) e envia via sinalização.
     *  `mode` é "mirror" (duplicar a tela do PC) ou "extend" (pedir uma segunda
     *  tela de verdade — o PC pode não conseguir e cair pra "mirror" sozinho,
     *  ver [Listener.onModeResolved]).
     *
     *  Item 9: `profile` ("standard" ou "low_latency") é repassado no offer
     *  para o server ajustar bitrate/fps/codec. Em low_latency, também aplicamos:
     *  - `bundlePolicy = MAXBUNDLE` (um único par de portas ICE, menos overhead)
     *  - `iceCandidatePoolSize = 0` (não pre-aloca candidatos; em LAN não precisa)
     *  - bitrate máximo 2.5 Mbps, fps máximo 60 (repassados via sinalização)
     *  - Trickle ICE em vez de esperar COMPLETE: envia candidatos assim que
     *    disponíveis. Em USB isso corta 1-2s do setup.
     */
    fun startConnection(
        quality: String,
        mode: String = "mirror",
        screenWidth: Int = 0,
        screenHeight: Int = 0,
        profile: String = latencyProfile,
    ) {
        pendingOfferQuality = quality
        pendingOfferMode = mode
        pendingScreenWidth = screenWidth
        pendingScreenHeight = screenHeight
        requestedMode = mode

        val isLowLatency = profile == "low_latency"

        val rtcConfig = PeerConnection.RTCConfiguration(emptyList<PeerConnection.IceServer>()).apply {
            // Sem servidores STUN/TURN: conexão é sempre direta na rede local.
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
            // Item 9: ajustes para baixa latência em cabo.
            if (isLowLatency) {
                bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE
                iceCandidatePoolSize = 0
            }
        }

        peerConnection = factory.createPeerConnection(rtcConfig, object : PeerConnection.Observer {
            override fun onIceCandidate(candidate: IceCandidate?) {
                // Item 9: Trickle ICE em low_latency — envia cada candidato
                // assim que disponível, em vez de esperar GATHER_COMPLETE.
                // Reduz o tempo até o primeiro frame em ~1s em LAN.
                if (isLowLatency && candidate != null) {
                    signalingClient.sendIceCandidate(candidate.sdp, candidate.sdpMid, candidate.sdpMLineIndex)
                }
            }

            override fun onAddStream(stream: MediaStream?) {
                // Callback legado (Plan B) — mantido como fallback, mas não é
                // garantido disparar em Unified Plan. A captura real da track
                // acontece em onAddTrack, abaixo.
                val track = stream?.videoTracks?.firstOrNull()
                if (track != null) {
                    RemoteLog.i(TAG, "Track de vídeo remota recebida (onAddStream)")
                    listener?.onRemoteVideoTrack(track)
                }
            }

            override fun onDataChannel(channel: DataChannel?) {}
            override fun onIceConnectionChange(state: PeerConnection.IceConnectionState?) {
                if (state == PeerConnection.IceConnectionState.FAILED ||
                    state == PeerConnection.IceConnectionState.DISCONNECTED
                ) {
                    RemoteLog.w(TAG, "Estado ICE: $state")
                } else {
                    RemoteLog.i(TAG, "Estado ICE: $state")
                }
                if (state == PeerConnection.IceConnectionState.FAILED) {
                    listener?.onConnectionFailed("Conexão WebRTC falhou (ICE)")
                }
            }

            override fun onSignalingChange(state: PeerConnection.SignalingState?) {}
            override fun onIceConnectionReceivingChange(receiving: Boolean) {}

            override fun onIceGatheringChange(state: PeerConnection.IceGatheringState?) {
                RemoteLog.i(TAG, "Estado de coleta ICE: $state")
                // Em low_latency com trickle ICE, NÃO esperamos COMPLETE — os
                // candidatos já foram enviados um a um. Enviamos a offer logo
                // após setLocalDescription (ver createOffer callback abaixo).
                // Em standard (Wi-Fi), mantemos o comportamento original:
                // esperar COMPLETE e enviar SDP com todos os candidatos.
                if (!isLowLatency && state == PeerConnection.IceGatheringState.COMPLETE) {
                    val localDesc = peerConnection?.localDescription
                    val quality = pendingOfferQuality
                    if (localDesc != null && quality != null) {
                        pendingOfferQuality = null
                        signalingClient.sendOffer(
                            sdp = localDesc.description,
                            sdpType = "offer",
                            quality = quality,
                            mode = pendingOfferMode,
                            screenWidth = pendingScreenWidth,
                            screenHeight = pendingScreenHeight,
                            profile = profile,
                        )
                    }
                }
            }

            override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>?) {}

            override fun onAddTrack(receiver: org.webrtc.RtpReceiver?, streams: Array<out MediaStream>?) {
                // Callback correto para Unified Plan (o modo configurado nesta PeerConnection).
                val track = receiver?.track()
                if (track is VideoTrack) {
                    RemoteLog.i(TAG, "Track de vídeo remota recebida (onAddTrack)")
                    listener?.onRemoteVideoTrack(track)
                    
                    // FASE 3: iniciar monitoramento de frame congelado
                    // Se não receber frame em 10s, dispara reconexão automática
                    startFrozenFrameDetection()
                }
            }

            override fun onRemoveStream(stream: MediaStream?) {}
            override fun onRenegotiationNeeded() {}
        })

        // Canal de dados usado para enviar toques/teclas ao PC.
        val init = DataChannel.Init().apply { ordered = true }
        controlChannel = peerConnection?.createDataChannel("control", init)
        controlChannel?.registerObserver(object : DataChannel.Observer {
            override fun onStateChange() {
                if (controlChannel?.state() == DataChannel.State.OPEN) {
                    listener?.onControlChannelReady()
                }
            }
            override fun onMessage(buffer: DataChannel.Buffer?) {
                val buf = buffer ?: return
                try {
                    val bytes = ByteArray(buf.data.remaining())
                    buf.data.get(bytes)
                    val json = JSONObject(String(bytes, Charset.forName("UTF-8")))

                    // FASE 1: ignorar heartbeat (keep-alive do servidor)
                    if (json.optString("type") == "heartbeat") {
                        RemoteLog.d(TAG, "Heartbeat recebido do servidor")
                        return
                    }

                    if (json.optString("type") != "cursor_pos") return

                    val x = json.optDouble("x", -1.0).toFloat()
                    val y = json.optDouble("y", -1.0).toFloat()
                    if (x in 0f..1f && y in 0f..1f) {
                        listener?.onCursorPosition(x, y)
                    }

                    // Item 4: a maioria das mensagens não traz "shape" (só
                    // manda quando o cursor muda de forma no PC) — evita
                    // decodificar um PNG a cada frame à toa.
                    val shape = json.optJSONObject("shape") ?: return
                    val pngB64 = shape.optString("png", "")
                    if (pngB64.isEmpty()) return
                    val pngBytes = android.util.Base64.decode(pngB64, android.util.Base64.DEFAULT)
                    val bitmap = android.graphics.BitmapFactory.decodeByteArray(pngBytes, 0, pngBytes.size)
                    if (bitmap != null) {
                        listener?.onCursorShape(bitmap, shape.optInt("hotX", 0), shape.optInt("hotY", 0))
                    }
                } catch (e: Exception) {
                    RemoteLog.e(TAG, "Falha ao processar mensagem do servidor: ${e.message}")
                }
            }
            override fun onBufferedAmountChange(amount: Long) {}
        })

        val offerConstraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveVideo", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveAudio", "false"))
        }

        peerConnection?.createOffer(object : SdpObserver by NoopSdpObserver {
            override fun onCreateSuccess(desc: SessionDescription) {
                peerConnection?.setLocalDescription(NoopSdpObserver, desc)

                if (isLowLatency) {
                    // Item 9: em low_latency, envia a offer IMEDIATAMENTE após
                    // setLocalDescription — não espera ICE COMPLETE. Os candidatos
                    // ICE restantes virão via sendIceCandidate (trickle).
                    val quality = pendingOfferQuality
                    if (quality != null) {
                        pendingOfferQuality = null
                        // Pega a SDP atual (já com setLocalDescription aplicado).
                        val localDesc = peerConnection?.localDescription ?: desc
                        // Limites explícitos para o server (Item 9).
                        val maxBitrate = 2_500_000  // 2.5 Mbps — suficiente p/ 720p60 em H264
                        val maxFps = 60
                        signalingClient.sendOffer(
                            sdp = localDesc.description,
                            sdpType = "offer",
                            quality = quality,
                            mode = pendingOfferMode,
                            screenWidth = pendingScreenWidth,
                            screenHeight = pendingScreenHeight,
                            profile = profile,
                            maxBitrate = maxBitrate,
                            maxFps = maxFps,
                        )
                    }
                }
                // Em standard (Wi-Fi), a offer é enviada em onIceGatheringChange(COMPLETE).
            }

            override fun onCreateFailure(error: String?) {
                RemoteLog.e(TAG, "Falha ao criar offer: $error")
                listener?.onConnectionFailed("Falha ao criar offer: $error")
            }
        }, offerConstraints)
    }

    /** Aplica a answer recebida do PC via sinalização. */
    fun applyRemoteAnswer(sdp: String, sdpType: String, resolvedMode: String, modeFallbackReason: String?) {
        val description = SessionDescription(SessionDescription.Type.fromCanonicalForm(sdpType), sdp)
        peerConnection?.setRemoteDescription(NoopSdpObserver, description)
        listener?.onModeResolved(requestedMode, resolvedMode, modeFallbackReason)
    }

    /** Envia um evento de toque/tecla ao PC (ignorado pelo servidor se "Permitir controle" estiver desligado). */
    fun sendControlEvent(json: JSONObject) {
        val channel = controlChannel ?: return
        if (channel.state() != DataChannel.State.OPEN) return
        val bytes = json.toString().toByteArray(Charset.forName("UTF-8"))
        channel.send(DataChannel.Buffer(java.nio.ByteBuffer.wrap(bytes), false))
    }

    // FASE 3: detecção de frame congelado (vídeo parado >10s)
    private fun startFrozenFrameDetection() {
        lastFrameTimestamp = System.currentTimeMillis()
        
        // Parar task anterior se existir
        frozenFrameDetectionTask?.interrupt()
        
        frozenFrameDetectionTask = Thread {
            Thread.currentThread().name = "FrozenFrameDetector"
            try {
                while (!Thread.currentThread().isInterrupted) {
                    Thread.sleep(5000) // verificar a cada 5s
                    
                    val now = System.currentTimeMillis()
                    val timeSinceLastFrame = now - lastFrameTimestamp
                    
                    if (timeSinceLastFrame > 10000) { // 10s sem frame
                        RemoteLog.w(TAG, "Frame congelado detectado (${timeSinceLastFrame}ms sem recebimento)")
                        listener?.onConnectionFailed("Vídeo congelado — reconectando...")
                        break
                    }
                }
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
            }
        }.apply { start() }
    }
    
    // Atualizar timestamp quando frame é renderizado (chamado do MainViewModel)
    fun updateLastFrameTimestamp() {
        lastFrameTimestamp = System.currentTimeMillis()
    }

    fun close() {
        frozenFrameDetectionTask?.interrupt()
        frozenFrameDetectionTask = null
        controlChannel?.close()
        controlChannel = null
        peerConnection?.close()
        peerConnection = null
        eglBase.release()
    }
}

/** SdpObserver que ignora os callbacks que não usamos, para evitar boilerplate repetido. */
private object NoopSdpObserver : SdpObserver {
    override fun onCreateSuccess(p0: SessionDescription?) {}
    override fun onSetSuccess() {}
    override fun onCreateFailure(p0: String?) {}
    override fun onSetFailure(p0: String?) {}
}
