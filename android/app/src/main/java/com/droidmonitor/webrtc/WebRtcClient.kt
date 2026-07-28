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
    }

    var listener: Listener? = null

    val eglBase: EglBase = EglBase.create()

    private val factory: PeerConnectionFactory
    private var peerConnection: PeerConnection? = null
    private var controlChannel: DataChannel? = null

    init {
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(context)
                .createInitializationOptions()
        )

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
     *  ver [Listener.onModeResolved]). */
    fun startConnection(quality: String, mode: String = "mirror", screenWidth: Int = 0, screenHeight: Int = 0) {
        pendingOfferQuality = quality
        pendingOfferMode = mode
        pendingScreenWidth = screenWidth
        pendingScreenHeight = screenHeight
        requestedMode = mode

        val rtcConfig = PeerConnection.RTCConfiguration(emptyList<PeerConnection.IceServer>()).apply {
            // Sem servidores STUN/TURN: conexão é sempre direta na rede local.
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
        }

        peerConnection = factory.createPeerConnection(rtcConfig, object : PeerConnection.Observer {
            override fun onIceCandidate(candidate: IceCandidate?) {
                // Não usamos trickle ICE (candidatos avulsos via sinalização).
                // Em vez disso, esperamos onIceGatheringChange == COMPLETE e
                // mandamos a SDP já com todos os candidatos embutidos.
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
                if (state == PeerConnection.IceGatheringState.COMPLETE) {
                    // Só agora a SDP local tem todos os candidatos ICE embutidos.
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
            override fun onMessage(buffer: DataChannel.Buffer?) {}
            override fun onBufferedAmountChange(amount: Long) {}
        })

        val offerConstraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveVideo", "true"))
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveAudio", "false"))
        }

        peerConnection?.createOffer(object : SdpObserver by NoopSdpObserver {
            override fun onCreateSuccess(desc: SessionDescription) {
                // Não envia aqui: essa SDP ainda não tem candidatos ICE.
                // O envio real acontece em onIceGatheringChange(COMPLETE).
                peerConnection?.setLocalDescription(NoopSdpObserver, desc)
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

    fun close() {
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
