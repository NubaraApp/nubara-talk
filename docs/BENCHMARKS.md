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

## F3 — traducción local

MacBook Pro M-series, NLLB-200 distilled 600M cuantizado a int8, CTranslate2 en CPU con
4 hilos, `beam_size=1`. Corrida con `HF_HUB_OFFLINE=1`: nada sale a la red.

| Medida | Valor |
|---|---|
| Modelo en disco | 621 MB |
| Carga del modelo | 0.65 s |
| Primera inferencia | 0.149 s |
| Latencia mediana por segmento | 0.199 s |
| Latencia p95 | 0.244 s |
| Latencia máxima | 0.246 s |
| Lote de 16 vs 16 llamadas sueltas | 3.36x más rápido |

Sobre los 16 segmentos confirmados de una sesión real de F1, traducidos de inglés a
español. El presupuesto de F3 era 300 ms por segmento.

**En vivo, con Whisper corriendo al mismo tiempo**, la latencia de traducción sube a
250–300 ms: el traductor está en CPU y compite con el resto del proceso. Sigue dentro del
presupuesto, pero el margen es mucho más chico que en la corrida en frío. Si en F5 hiciera
falta más aire, la palanca es bajar `intra_threads` o traducir sólo los finales.

**Verificación del tokenizer.** `transformers` 5.x avisa que el `tokenizer.json` copiado
tiene "an incorrect regex pattern". El aviso es espurio para NLLB: comparando el tokenizer
copiado contra el original de `facebook/nllb-200-distilled-600M` (`use_fast=False`), los
ids coinciden exactamente en frases con puntuación, porcentajes y abreviaturas. Se dejó
constancia acá porque el mensaje asusta y va a volver a aparecer.

**Calidad, sin maquillaje.** La traducción es buena cuando la transcripción es buena. Los
errores de Whisper se propagan y se amplifican: "Revenue came in a head of Planteys
Quarter" (mic, con el audio filtrado por el aire) sale como "Los ingresos vinieron de un
jefe del barrio de Planteys". NLLB no tiene forma de saber que eso está mal. La pista
`sys`, que toma el audio limpio por BlackHole, no tiene ese problema.

## F4 — captura nativa

Corrida de 12 s con `--sys-device tap`, salida por defecto en las bocinas, hablando por
`say` mientras grababa.

| Medida | mic (BlackHoleSource) | sys (SwiftTapSource) |
|---|---|---|
| Duración | 11.866 s | 11.157 s |
| RMS | 0.01284 | **0.00000** |
| Muestras | 189 862 | 178 517 |
| Overflows | 0 | 0 |
| Bloques descartados | 0 | 0 |

Desfase de duración 5,98 %, contra un criterio de 2 %. El costo está en arrancar y parar
un subproceso con su pipe, no en la captura: 0,848 s de audio que no llegan contra 0,139 s
en la pista de PortAudio. Afinarlo tiene sentido cuando el tap entregue audio de verdad.

**El RMS en cero es un permiso, no un bug.** Lo verificado antes de decir eso: el tap se
crea sin error, `kAudioTapPropertyUID` coincide con el UID que se le pasa al agregado, el
agregado expone 1 canal de entrada, el formato es float32 mono 48 kHz y los callbacks
llegan cada 512 frames durante toda la corrida. Se probaron además las cuatro
combinaciones de `mono`/`stereo` global y tap privado/público: las cuatro en silencio.
`tccutil` no reconoce el bundle, así que el permiso no se puede pedir desde acá.

**Efecto colateral útil.** La primera corrida con el tap devolvió cero muestras en *las
dos* pistas. No fue el tap: compilar el helper con `swiftc` dejó la máquina ocupada lo
suficiente como para que `start()` tardara más que el timeout de la cola, y `read()`
terminaba antes de leer nada. Es una carrera que ya existía en `BlackHoleSource` y que
ninguna corrida anterior había destapado. Arreglada en las dos fuentes (ADR-017).

## F5 — UI

Corrida de punta a punta contra el sidecar, sin ventana: un cliente WebSocket arranca la
captura, reproduce tres frases por `say`, para, busca y pide una respuesta al LLM.

| Paso | Resultado |
|---|---|
| Segmentos recibidos en vivo | 12 (6 parciales + 6 finales, las dos pistas) |
| Traducciones recibidas | 12 |
| Búsqueda FTS5 (`trimestre`) | 1 fila |
| Transcripción de la sesión | 6 filas |
| Respuesta del LLM | `ollama`, local, 14.9 s |
| Consulta FTS5 inválida (`"`) | error devuelto al cliente, sidecar sigue vivo |

La respuesta a los dos últimos turnos de la pista `sys`:

> Excelente noticia sobre los ingresos. Por supuesto, le haré llegar el Cronograma de
> Migración hoy mismo.

Los 14,9 s contra los 8,1 s de F2 son del modelo, no del transporte: el prompt de la UI
lleva más contexto.

**Lo que no está medido.** No hay ninguna medición de la UI con una reunión de verdad, ni
del comportamiento de la lista después de cientos de turnos: el DOM crece sin recortar.
Con reuniones largas eso hay que mirarlo.

### F4, segunda vuelta: con la app instalada

Lanzando `granola-local.app` desde `/Applications` y tildando *captura nativa*, el tap
entrega audio. Tres frases por `say`, tres turnos transcritos en la pista `sys`:

```
* sys  Good morning everyone, thanks for joining.
* sys  The enterprise segment dropped most of that grout.
* sys  Can you send me the Migration Timeline today?
```

Sin BlackHole en el camino. La prueba de control: el **mismo** binario del helper, con el
mismo código, corrido desde la terminal en la misma sesión da RMS 0.00000. La única
variable es quién lo lanza.

Lo que no volví a medir: el desfase de duración entre pistas con el tap. La última medición
daba 5,98 % y fue sobre audio en silencio. Queda abierto.

## Idioma de la respuesta

Misma plantilla (`responder`), mismo `idioma: auto`, dos idiomas de entrada:

| Oído | Respuesta |
|---|---|
| `Can you send me the migration timeline before Friday?` | *(en inglés)* Yes, I can send you the migration timeline before Friday. |
| *(en español)* | *(en español)* Disculpe, ¿podrían por favor repetir o aclarar…? |

El ruteo de idioma funciona. Lo que no funcionó bien fue la transcripción del español:
`say -v Eddy` por los parlantes y de vuelta por el micrófono salió como "Me pudez monder
el chronograma democracia en anties delvines". Es una limitación de la prueba, no del
ruteo — el audio sintético reproducido y recapturado por el aire es el peor caso posible.

## Latencia de la respuesta del LLM

Misma pregunta, `ollama run gemma4`, modelo ya cargado en GPU. Mejor de dos corridas.

| Configuración | Tiempo |
|---|---|
| `--hidethinking` (lo que había) | 4.10 s |
| `--think=false` | **0.46 s** |
| `--think false` (sin el `=`) | 4.7 s, y el razonamiento sale dentro de la respuesta |

Y el costo de tener el modelo descargado de memoria, con una pregunta corta:

| | Tiempo |
|---|---|
| Primera llamada tras descargarlo | 4.15 s |
| Siguiente, ya cargado | 0.50 s |

De ahí salen las dos medidas: `--think=false` por defecto y `--keepalive 30m`.

De punta a punta desde la UI, contra el sidecar, con la misma pregunta:

| | Antes | Ahora |
|---|---|---|
| Modelo descargado | 9.73 s | — |
| Modelo cargado | 5.55 s | **0.42 s** |

El streaming de la respuesta se agregó igual, pero conviene ser honesto: con
`--hidethinking` el primer texto llegaba a los 5.42 s de una respuesta de 5.55 s, porque
ollama no suelta nada hasta terminar de razonar. Lo que bajó la espera fue apagar el
razonamiento, no el streaming.

## Respuesta corta contra ampliada

Misma frase (`Revenue came in ahead of plan this quarter.`), de punta a punta contra el
sidecar:

| | CLI | Tiempo |
|---|---|---|
| `responder` | `ollama` (`--think=false`) | **0.73 s** |
| `ampliar` | `ollama-razonando` | 14.83 s |

La ampliada devuelve versión completa para decir en voz alta, dos alternativas de tono y
qué tener en cuenta antes de contestar. Las dos quedan guardadas en `answers`, la segunda
con `parent_id` apuntando a la primera.
