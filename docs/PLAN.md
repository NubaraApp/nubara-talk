# Plan de fases

Cada fase tiene un criterio de salida verificable. No se avanza sin cumplirlo.

## F0 — Captura dual

Dos pistas de audio independientes y simultáneas: `mic` y `sys`.

**Entregable:** `src/audio/base.py`, `src/audio/blackhole.py`, `src/spikes/f0_capture.py`.

**Criterios de salida:**

- [ ] `mic.wav` y `sys.wav` existen y su duración difiere en menos del 2 %
- [ ] `rms > 0.001` en ambas pistas
- [ ] Cero overflows, o menos de 3 en 20 s
- [ ] Los timestamps de ambas pistas son comparables entre sí (mismo reloj `time.monotonic()`)
- [ ] `python -m src.spikes.f0_capture` corre limpio desde el repo raíz

Se valida con música sonando y hablando al micrófono a la vez.

## F1 — STT en vivo

VAD con Silero para cortar por turnos de habla. `mlx-whisper` en Apple Silicon,
fallback a `faster-whisper` int8 en Intel. **Una sola instancia del modelo**, con lock
y cola de trabajos: las dos pistas comparten el modelo, no cargan uno cada una.

Escritura a SQLite: segmentos `provisional` al vuelo, promovidos a `confirmed` cuando
el turno cierra.

**Criterios de salida:**

- [ ] Latencia mediana desde fin de turno hasta texto en pantalla < 2 s
- [ ] Las dos pistas transcriben en paralelo sin bloquearse mutuamente
- [ ] Los segmentos de ambas pistas se intercalan correctamente por `t_start`
- [ ] Números medidos en `BENCHMARKS.md`

## F2 — Puente LLM por CLI

Seleccionar un fragmento de la transcripción, elegir un prompt de `config/prompts.yaml`,
invocar la CLI del LLM por `subprocess` y mostrar la respuesta.

**Sin claves de API en el repo.** Se usa la CLI ya autenticada del usuario.

**Criterios de salida:**

- [ ] Plantillas cargadas desde `config/prompts.yaml`, no hardcodeadas
- [ ] `subprocess` con timeout y manejo de error si la CLI no está autenticada
- [ ] Cero secretos en el repo

## F3 — Traducción local

NLLB-200 distilled 600M sobre CTranslate2. Subtitulado en vivo, local.

La CLI del LLM se reserva para "traducí esto bien" bajo demanda; **no** se usa para el
subtitulado en vivo (demasiado lento y caro en latencia).

**Criterios de salida:**

- [ ] Traducción de un segmento típico < 300 ms
- [ ] `text_trad` poblado en SQLite y buscable vía FTS5

## F4 — Captura nativa

`SwiftTapSource` reemplaza a `BlackHoleSource` detrás de la misma interfaz
`AudioSource`. Core Audio Process Taps (macOS 14.4+) o ScreenCaptureKit: sin driver
de terceros, sin Multi-Output, sin que el usuario pueda romper el ruteo por accidente.

**Criterios de salida:**

- [ ] Los mismos criterios de F0, pero sin BlackHole instalado
- [ ] Ni un cambio fuera de `src/audio/`

## F5 — UI

Tauri con sidecar Python. Se comunican por WebSocket local.

**Criterios de salida:**

- [ ] Transcripción en vivo de ambas pistas, visualmente distinguibles
- [ ] Selección de fragmento y disparo de prompt desde la UI
- [ ] Búsqueda FTS5 sobre sesiones pasadas
