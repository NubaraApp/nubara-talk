# Benchmarks

Latencias medidas en este equipo: Apple M5 Pro, macOS 26.6, Python 3.12, BlackHole 0.7.1.

## F0 — captura dual (validación real, 20 s)

`mic` = micrófono integrado. `sys` = BlackHole 2ch alimentado por un Multi-Output Device.

| Qué se mide | Valor | Fecha |
|-------------|-------|-------|
| Escalón de duración entre pistas | 0.00 % | 2026-08-18 |
| Desfase de `ts_monotonic` entre pistas | 11.3 ms | 2026-08-18 |
| Overflows de PortAudio en 20 s | 0 | 2026-08-18 |
| Bloques descartados por cola llena | 0 | 2026-08-18 |
| Latencia de arranque + cola del remuestreador | 125 ms | 2026-08-18 |
| RMS pista `mic` (sala, sin hablar) | 0.0100 | 2026-08-18 |
| RMS pista `sys` (voz reproducida) | 0.1492 | 2026-08-18 |
| Correlación entre pistas | 0.0011 | 2026-08-18 |

La correlación cercana a cero dice que las dos pistas **no son la misma forma de onda**:
`sys` es la señal digital antes del hardware, sin el retardo acústico ni la reverberación
de la sala. No dice que el micrófono no oiga los parlantes — sí los oye. En F1 el mic
transcribió el mismo discurso que salía por los parlantes, con más errores.

## Notas

**El escalón entre pistas no es gratis.** Abrir un `InputStream` cuesta ~40 ms y cerrarlo
~180 ms. Haciéndolo en secuencia, las pistas quedaban desfasadas 2.27 % en una captura de
8 s — por encima del límite. Se resolvió abriendo las dos antes de arrancar ninguna y
cerrándolas en hilos paralelos. Bajó a 0.00 %.

**Las muestras que "faltan vs pared"** son un bloque de latencia de arranque más la cola
del remuestreador. No es audio perdido. Preocupa cuando crece mucho por encima de un
bloque, o cuando difiere entre las dos pistas.

**Condición de la validación:** la pista `sys` recibió voz sintetizada con `say`
reproducida por el Multi-Output. La pista `mic` capturó ruido de sala, no habla directa.
Ambas superan el umbral de RMS con margen, pero el número de `mic` con alguien hablándole
de cerca va a ser bastante más alto.


## F1 — STT en vivo (validación real, 38 s)

Modelo: `mlx-community/whisper-large-v3-turbo` sobre MLX/Metal. VAD: Silero, una
instancia por pista.

| Qué se mide | Valor | Fecha |
|-------------|-------|-------|
| Latencia mediana, fin de turno a texto | **0.50 s** | 2026-08-18 |
| Latencia p95 | 0.59 s | 2026-08-18 |
| Latencia mediana pista `sys` | 0.43 s | 2026-08-18 |
| Latencia mediana pista `mic` | 0.52 s | 2026-08-18 |
| Inferencia mediana por turno | 0.181 s | 2026-08-18 |
| RTF (53.7 s de habla) | 0.054 | 2026-08-18 |
| Carga del modelo (ya descargado) | 1.5 s | 2026-08-18 |
| Descarga inicial del modelo | ~30 s | 2026-08-18 |
| Costo del VAD por frame de 32 ms | 0.069 ms | 2026-08-18 |
| Turnos transcritos / detectados | 16 / 16 | 2026-08-18 |
| Parciales descartados por obsoletos | 0 | 2026-08-18 |

Latencia por turno según duración del audio, medida aparte:

| Audio del turno | Inferencia | RTF |
|---|---|---|
| 3 s | 0.18 s | 0.060 |
| 5 s | 0.20 s | 0.039 |
| 8 s | 0.22 s | 0.027 |
| 20 s | 0.32 s | 0.016 |

El costo es casi todo fijo: turnos más largos casi no cuestan más. El presupuesto de 2 s
sobra por un factor de cuatro.

### Condición de la validación

La pista `sys` recibió voz sintetizada con `say`. **El micrófono captó los parlantes**,
así que las dos pistas transcribieron el mismo discurso: sirve para probar que ambas
corren en paralelo sin bloquearse, pero no es un escenario de dos hablantes reales. La
transcripción del mic salió peor —"Revenue came in a head of Planteys Quarter"— que es
lo esperable de audio acústico con reverberación.


## F2 — puente LLM por CLI

| Qué se mide | Valor | Fecha |
|-------------|-------|-------|
| Respuesta completa, `ollama run gemma4` local | 8.1 s | 2026-08-18 |
| Prompt de 2 segmentos + 4 de contexto | ~60 palabras | 2026-08-18 |

El costo de arranque de proceso más la carga del modelo domina: por eso la CLI se reserva
para pedidos bajo demanda y **no** se usa para el subtitulado en vivo. Ver ADR-006.

`claude -p` no se pudo medir: la CLI está instalada pero sin login en este equipo. El
puente lo detecta y lo reporta como tal, en lugar de tratarlo como un fallo genérico.
