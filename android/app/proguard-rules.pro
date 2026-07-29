# Regras específicas do projeto.
#
# A partir de agora isMinifyEnabled = true no build de release (ver
# build.gradle.kts) — a maioria das bibliotecas (WebRTC, OkHttp, MLKit) já
# vem com suas próprias regras de proteção dentro do .aar, aplicadas
# automaticamente. O JmDNS (usado pra descoberta de PCs na rede) usa
# reflexão internamente e é conhecido por precisar de regras explícitas,
# então mantemos a classe inteira sem ofuscar por segurança.
-keep class javax.jmdns.** { *; }
-dontwarn javax.jmdns.**

# org.json (parte do protocolo de sinalização) já é classe do próprio
# Android SDK, não precisa de regra — mas deixamos aqui documentado.
-dontwarn org.json.**
