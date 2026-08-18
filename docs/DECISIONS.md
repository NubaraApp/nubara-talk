# Decisiones de arquitectura

ADRs cortos. Cada uno registra qué se decidió y qué se descartó, para no reabrir la
discusión en tres meses.

---

## ADR-001 — Sin diarización de hablantes

**Decisión:** no se identifican hablantes por voz.

**Motivo:** dos pistas independientes ya responden quién habla. `mic` es el usuario,
`sys` son los demás. pyannote es pesado, frágil y agrega latencia para resolver un
problema que la topología de captura ya resolvió gratis.

**Costo aceptado:** si hay dos personas físicamente en la misma sala compartiendo el
micrófono, se mezclan en la pista `mic`. Es un caso que no nos interesa: el uso real es
videollamada.

---

## ADR-002 — Captura del sistema vía BlackHole, no ScreenCaptureKit

**Decisión:** F0 usa BlackHole 2ch + Multi-Output Device.

**Motivo:** funciona hoy, sin escribir Swift ni empaquetar un helper firmado. Permite
llegar a un spike verificable en horas, no en días.

**Descartado por ahora:** ScreenCaptureKit y Core Audio Process Taps (macOS 14.4+).
Son mejores —sin driver de terceros, sin que el usuario rompa el ruteo— pero requieren
un binario Swift y entitlements. Van en F4, detrás de la interfaz `AudioSource`.

**Costo aceptado:** el usuario tiene que instalar un driver y mantener activo el
Multi-Output. Es frágil: conectar auriculares lo desactiva. Por eso el spike lo detecta
y advierte.

---

## ADR-003 — Dos InputStream independientes, no un Aggregate Device

**Decisión:** un `sounddevice.InputStream` por pista, cada uno en su propio hilo.

**Motivo:** un Aggregate Device fuerza a los dos dispositivos a un reloj común y
introduce un punto de fallo compartido: si BlackHole se cae, se cae también el mic.
Con streams separados, cada pista falla sola.

**Sobre la deriva de reloj:** no importa. Las pistas nunca se mezclan muestra a muestra.
Cada una se transcribe por separado y los segmentos resultantes se alinean en la línea
de tiempo por `time.monotonic()`, que es el mismo reloj para ambas. Un desfase de
milisegundos entre relojes de hardware es irrelevante frente a turnos de habla de
segundos.

---

## ADR-004 — mlx-whisper en Apple Silicon

**Decisión:** `mlx-whisper` como motor de STT primario. `faster-whisper` int8 como
fallback en Intel.

**Motivo:** `mlx-whisper` usa la GPU por Metal. `faster-whisper` en Mac corre en CPU
—no hay CUDA— y rinde peor.

**Decisión asociada:** **una sola instancia del modelo**, compartida por las dos pistas,
con un lock y una cola de trabajos. Cargar dos instancias duplica la memoria y hace
competir a las dos pistas por la misma GPU, empeorando la latencia de ambas.

---

## ADR-005 — VAD con Silero, cortando por turnos

**Decisión:** Silero VAD. Los cortes se hacen en los límites de los turnos de habla,
nunca por ventana de tiempo fija.

**Motivo:** cortar cada N segundos parte palabras y frases al medio. Whisper depende del
contexto de la frase completa; un corte en mitad de una palabra degrada la transcripción
de los dos fragmentos resultantes.

---

## ADR-006 — Traducción con NLLB-200 en CTranslate2, no por CLI

**Decisión:** NLLB-200 distilled 600M sobre CTranslate2, local, para el subtitulado en vivo.

**Motivo:** el subtitulado en vivo necesita latencia de decenas de milisegundos y corre
sobre cada segmento. Un `subprocess` a la CLI de un LLM cuesta cientos de milisegundos
solo de arranque de proceso.

**La CLI se reserva** para traducción bajo demanda —"traducí esto bien"— donde la calidad
importa más que la latencia y el usuario está esperando de todos modos.

---

## ADR-007 — SQLite con FTS5

**Decisión:** SQLite, tabla `segments` + tabla virtual FTS5 sincronizada por triggers.

**Motivo:** un archivo, cero servidor, búsqueda de texto completo incluida. Las sesiones
son de miles de segmentos, no de millones: no hace falta nada más grande.

---

## ADR-008 — El LLM se invoca por CLI, no por API HTTP

**Decisión:** `subprocess` contra la CLI ya autenticada del usuario.

**Motivo:** no consume créditos de API y no requiere que el repo maneje ninguna clave.
**Nunca se escriben claves ni tokens en el repo, ni siquiera de ejemplo.**

---

## ADR-009 — UI en Tauri con sidecar Python

**Decisión:** Tauri para la UI, el proceso Python como sidecar, comunicados por
WebSocket local.

**Motivo:** el núcleo de audio y ML es Python y no se va a reescribir. Tauri da una app
nativa liviana sin empaquetar Chromium. El WebSocket local es la frontera más simple
entre los dos procesos.
