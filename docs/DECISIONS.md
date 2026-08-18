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

---

## ADR-010 — Una instancia de Silero VAD por pista

**Decisión:** cada `TurnDetector` carga su propio modelo de VAD. `TurnDetector` lo carga
solo si no se lo pasan, para que compartirlo requiera hacerlo a propósito.

**Motivo:** el modelo es un LSTM con estado. Compartirlo entre las dos pistas mezcla el
contexto de una con el de la otra, y las decisiones de habla/silencio salen mal. Peor:
usarlo desde dos hilos a la vez corrompe el heap de torch. El proceso muere con SIGTRAP y
`BUG IN CLIENT OF LIBMALLOC: memory corruption of free block` dentro de
`slow_conv2d_forward_cpu`. Un test aislado de 600 iteraciones concurrentes no lo disparó;
la captura real sí, en menos de diez segundos.

**Costo aceptado:** ninguno medible. El modelo pesa ~2 MB y su costo por frame es de
0.069 ms.

**No confundir con ADR-004:** el modelo de STT sí es uno solo compartido. Ahí la
compartición es deliberada y está protegida por un lock y una cola, porque el modelo pesa
gigabytes y compite por la GPU. El VAD es lo contrario: barato y con estado por pista.

---

## ADR-011 — Turnos con corte por duración máxima y parciales

**Decisión:** el detector cierra un turno por silencio, pero también a los 15 s aunque el
habla siga. Mientras el turno está abierto emite parciales cada 2.5 s, que se guardan como
`provisional` y se pisan cuando el turno cierra como `confirmed`.

**Motivo:** `VADIterator`, el helper que trae silero-vad, solo cierra por silencio. Con
habla continua nunca emite el fin del turno: alguien que habla dos minutos seguidos no
produciría ni una línea hasta callarse. El corte por duración garantiza un piso de
frecuencia de actualización, y los parciales hacen que el texto aparezca mientras la
persona todavía está hablando.

**Costo aceptado:** un corte forzado puede caer en mitad de una palabra. Es el mismo
problema que ADR-005 quería evitar, pero acotado: pasa una vez cada 15 s de habla continua
en lugar de en cada ventana.

---

## ADR-012 — La CLI local va primero en la lista de candidatos

**Decisión:** `config/prompts.yaml` lista varias CLI y se usa la primera instalada. El
orden por defecto pone `ollama` antes que `claude`.

**Motivo:** el prompt lleva un fragmento literal de la reunión. Con una CLI local el texto
no sale de la máquina; con una remota viaja al proveedor. Para transcripciones de
reuniones eso es una diferencia real, y el default tiene que ser el conservador.

**Costo aceptado:** un modelo local da respuestas peores que uno grande. Se cambia con
`--cli claude`, que es una decisión consciente por invocación, no un default silencioso.
El spike imprime siempre si la CLI elegida es local o remota antes de mandar nada.

---

## ADR-013 — El prompt va por stdin, nunca por argv

**Decisión:** `subprocess.run(comando, input=prompt)`, sin `shell=True`.

**Motivo:** un tramo de reunión largo excede el límite de tamaño de los argumentos del
sistema. Y pasarlo por argv obliga a preocuparse por el quoting de un texto que viene de
una transcripción automática, que puede contener cualquier cosa. Por stdin no hay nada
que escapar.

---

## ADR-014 — NLLB-200 distilled 600M en CTranslate2, no la CLI del LLM

**Decisión:** el subtitulado en vivo lo hace NLLB-200 distilled 600M cuantizado a int8
sobre CTranslate2, en CPU. La CLI de F2 queda para "traducí esto bien" bajo demanda.

**Motivo:** presupuesto de latencia. Medido en este equipo, NLLB traduce un segmento de
reunión en 199 ms de mediana; el mismo texto por `ollama run gemma4` tarda del orden de
segundos y hay que pagarlo en cada turno de las dos pistas. Además NLLB es un modelo de
traducción: no reformula ni comenta, que es exactamente lo que se quiere en un subtítulo.

**Costo aceptado:** 621 MB de modelo en disco y un dependencia más (`transformers`, sólo
por el tokenizer). NLLB traduce peor los nombres propios y las frases a medias que salen
de un parcial de STT.

---

## ADR-015 — La traducción vive en su propio hilo, con descarte de parciales

**Decisión:** `TranslationWorker` tiene cola e hilo propios. El motor de STT le entrega
resultados y sigue. Los finales tienen prioridad y un parcial se descarta si ya entró uno
más nuevo de la misma pista.

**Motivo:** el motor de STT llama a sus callbacks desde el hilo de inferencia. Traducir
ahí sumaría 200 ms al turno siguiente de las dos pistas. El descarte es el mismo criterio
de ADR-009: el texto de un parcial viejo ya está obsoleto antes de salir de la cola.

**Costo aceptado:** un segundo hilo compitiendo por CPU con Whisper. En Apple Silicon
Whisper corre en la GPU vía MLX, así que la pelea es menor de lo que parece.

---

## ADR-016 — El process tap va en un helper de Swift, no en Python

**Decisión:** `SwiftTapSource` lanza un binario de Swift que crea el Core Audio Process
Tap y escribe float32 mono por stdout. Python lee el pipe, remuestrea y entrega
`AudioChunk`, igual que `BlackHoleSource`.

**Motivo:** `AudioHardwareCreateProcessTap`, `CATapDescription` y el IOProc son API de
Objective-C y bloques de tiempo real. Manejar eso desde Python con ctypes es posible y es
una mala idea: el IOProc corre en un hilo de tiempo real y el GIL no tiene lugar ahí.
Además el tap necesita identidad de bundle firmada para que el sistema le pueda dar
permiso, y un intérprete de Python no la tiene.

**Costo aceptado:** hay que compilar en la primera corrida (`swiftc`, unos segundos) y
un proceso más vivo durante la captura. El helper se recompila solo si el `.swift` es más
nuevo que el binario.

---

## ADR-017 — `read()` no puede terminar antes de que la captura arranque

**Decisión:** las fuentes llevan `_ever_started`. El iterador de `read()` sólo termina si
la captura llegó a correr y ya paró.

**Motivo:** el consumidor arranca su hilo antes que la fuente. Antes, si `start()` se
demoraba más que el timeout de la cola, `read()` veía la cola vacía y `_running` en False
y terminaba sin haber leído nada. No era teórico: compilar el helper de F4 dejó la
máquina lo bastante ocupada como para que las dos pistas devolvieran cero muestras.

---

## ADR-018 — La UI es una ventana tonta y un sidecar de Python

**Decisión:** Tauri muestra `src/ui/web` y lanza `python -m src.ui.server`. Se hablan por
un WebSocket en 127.0.0.1. En Rust no hay nada de lógica: sólo abrir la ventana, arrancar
el sidecar y matarlo al cerrar.

**Motivo:** todo lo que hace la app ya está en Python — captura, VAD, Whisper, NLLB,
SQLite, el puente al LLM. Reescribir cualquier parte en Rust sería duplicarla. Y el modelo
de STT tarda segundos en cargar: si viviera en el proceso de la ventana, cada arranque de
la UI lo pagaría de nuevo.

**Costo aceptado:** hay dos procesos que pueden desincronizarse, y el sidecar hay que
matarlo explícitamente al cerrar la ventana o queda un Python huérfano capturando audio.
Está resuelto en el manejador de `RunEvent::Exit`.

---

## ADR-019 — El frontend es HTML plano, sin bundler ni framework

**Decisión:** `index.html`, `app.js` y `style.css`. Sin npm en el frontend, sin build.

**Motivo:** la UI son tres cosas: una lista que crece, una selección y un par de
formularios. Un framework agregaría un paso de build, un `node_modules` y una cadena de
dependencias más larga que todo el resto del proyecto junto. `npm` aparece una sola vez,
para bajar el binario del CLI de Tauri.

**Costo aceptado:** el estado se maneja a mano con un `Map` y `document.getElementById`.
Si la UI crece a varias pantallas, esta decisión hay que revisarla.

---

## ADR-020 — El WebSocket escucha sólo en 127.0.0.1

**Decisión:** `HOST = "127.0.0.1"`, nunca `0.0.0.0`.

**Motivo:** por ese socket pasa la transcripción completa de la reunión. En `0.0.0.0`
cualquiera en la misma red WiFi podría leerla. No hay autenticación porque no hace falta:
el socket no es alcanzable desde afuera de la máquina.

**Costo aceptado:** no se puede abrir la UI desde otro dispositivo. Es intencional.

---

## ADR-021 — La captura nativa sólo funciona lanzada desde la app

**Decisión:** el process tap de F4 se usa desde `granola-local.app` instalada en
`/Applications`. Desde una terminal no se usa: se deja `BlackHoleSource`.

**Motivo:** no es una preferencia, es cómo funciona el permiso. El mismo binario del
helper, con el mismo código, devuelve silencio absoluto lanzado desde la terminal y audio
real lanzado por la app. Lo que cambia es a quién le atribuye el sistema la captura: el
`.app` firmado tiene identidad y permiso; un binario suelto en `build/` no.

**Costo aceptado:** los spikes de línea de comandos no pueden probar el tap. Para eso está
la casilla *captura nativa* en la ventana. Y la app queda firmada ad-hoc, así que el
permiso se pierde si se recompila y cambia el hash.
