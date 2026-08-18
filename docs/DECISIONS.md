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
El cierre normal lo resuelve el manejador de `RunEvent::Exit`. Ese manejador **no** corre
si la app se cae o la matan con una señal, así que el sidecar además vigila a su padre y
se apaga solo cuando `getppid()` cambia. Verificado matando la app con `pkill`.

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

---

## ADR-022 — La respuesta va en el idioma del fragmento, no en el mío

**Decisión:** `answer_language()` mira el campo `lang` de los segmentos elegidos y le pide
al LLM que conteste en ese idioma. La UI puede forzarlo, pero el default es automático.

**Motivo:** la respuesta es para leérsela a quien preguntó. Si en la reunión te preguntan
en inglés, una respuesta en español no sirve para nada: habría que traducirla en la
cabeza, en voz alta y con la reunión esperando. El caso de uso del proyecto es
justamente ese — reuniones en inglés.

**Corrección posterior.** La primera versión aplicaba esta regla a todas las plantillas
y rompía la de traducir: pedía traducir un texto en inglés *al inglés*, y devolvía el
original sin tocar. El idioma depende de la acción, no sólo del fragmento. Ahora cada
plantilla lo declara en `config/prompts.yaml`:

- `idioma: fragmento` — contestar en el idioma en que hablaron. Es el de `responder`.
- `idioma: propio` — en el idioma en que yo leo. Es el de traducir, explicar y resumir.

El valor se valida al cargar el archivo: un `idioma` desconocido falla ahí y no en medio
de una reunión.

**Costo aceptado:** si Whisper se equivoca de idioma, la respuesta sale en el idioma
equivocado. Por eso el selector de la UI tiene español e inglés fijos además de
automático.

---

## ADR-023 — Las acciones viven encima de cada frase, no en un panel

**Decisión:** al pasar el mouse por una frase aparece una barra flotante con las
plantillas de `config/prompts.yaml`. Actúa sobre esa frase sola, o sobre toda la
selección si la frase ya estaba elegida.

**Motivo:** en una reunión, cada segundo que el usuario pasa buscando un botón es un
segundo que dejó de escuchar. Las acciones tienen que estar donde ya está mirando, que es
el texto. El panel lateral sigue existiendo para trabajar sobre varias frases juntas.

**Costo aceptado:** la barra tapa un poco de la burbuja de arriba. Se posiciona con
`bottom: 100%` para no cubrir la etiqueta de quién habló, que es lo que se lee primero.

---

## ADR-024 — Las sesiones se guardan con reloj de pared

**Decisión:** `sessions.started_at` y `ended_at` usan `time.time()`, no
`time.monotonic()`. Los tiempos de cada segmento siguen siendo relativos al arranque
de la sesión, medidos con `monotonic`.

**Motivo:** `monotonic` cuenta desde que arrancó la máquina. Ordena bien, pero no se
puede mostrar como fecha, y la lista de sesiones quedaba con identificadores hexadecimales
en vez de "Hoy 15:42".

**Costo aceptado:** las sesiones viejas quedaron con valores monotónicos. La UI las
reconoce porque son números chicos y les muestra el id, como antes.

---

## ADR-025 — La barra flotante necesita un puente invisible

**Decisión:** cada burbuja tiene un `::before` transparente de 14 px que ocupa el hueco
de arriba, entre ella y la burbuja anterior.

**Motivo:** sin eso, al mover el mouse hacia la barra de acciones se salía de la caja de
la burbuja por ese hueco, `:hover` se apagaba y la barra desaparecía antes de que se
pudiera hacer clic. Es el clásico problema de los menús que se abren hacia arriba. El
puente es hijo de la burbuja, así que estando encima de él la burbuja sigue en `:hover`.

**Costo aceptado:** esa franja de 14 px pertenece a la burbuja de abajo, así que la de
arriba pierde el `:hover` un poco antes de lo que el ojo espera. Es el hueco entre las
dos, donde no hay nada que tocar.

---

## ADR-026 — El modelo contesta sin razonar, salvo que se le pida

**Decisión:** el candidato `ollama` por defecto lleva `--think=false`. Queda un segundo
candidato, `ollama-razonando`, con el razonamiento prendido, elegible desde la ventana.

**Motivo:** medido con la misma pregunta y el modelo ya cargado, **4.10 s razonando
contra 0.46 s sin razonar**. Casi todo el tiempo de espera era razonamiento que
`--hidethinking` escondía: se generaba igual, y había que esperarlo igual. En una reunión,
cuatro segundos con la ventana diciendo "pensando…" es una eternidad.

Ojo con la forma del flag: `--think false` **no** lo apaga — ollama lo interpreta como
otra cosa y termina imprimiendo el razonamiento dentro de la respuesta. Hay que escribirlo
`--think=false`.

**Costo aceptado:** sin razonar las respuestas son más cortas y más planas. Para
`responder` en medio de una reunión es lo que se quiere; para resumir un tramo largo,
puede no serlo. Por eso el otro candidato sigue ahí, a un clic.

---

## ADR-027 — La respuesta se escribe a medida que sale

**Decisión:** `LlmCli.run()` acepta un `on_chunk` y el sidecar reenvía cada trozo por el
WebSocket. La ventana pinta el texto mientras el modelo escribe.

**Motivo:** con respuestas largas o con una CLI remota, esperar en silencio a que termine
se siente roto. Ver aparecer las primeras palabras dice que algo está pasando.

**Costo aceptado:** poco, pero conviene ser honesto con cuánto sirve. Con
`--hidethinking`, ollama no suelta una sola letra hasta terminar de razonar: medido, el
primer texto llegaba a los 5.4 s de una respuesta de 5.5 s. El streaming no arregla eso
— lo arregla ADR-026. Donde sí se nota es en respuestas largas y en `claude`.

**Detalle de implementación:** la limpieza de ANSI se hace sobre el texto acumulado y se
manda el delta, no trozo por trozo. Una secuencia de escape puede quedar partida entre
dos lecturas del pipe, y limpiando cada trozo por separado quedaría media secuencia
metida en la respuesta.

---

## ADR-028 — Las respuestas se guardan en la base, no en la pantalla

**Decisión:** tabla `answers` con la respuesta, la CLI que la dio, el idioma, el
fragmento al que contesta, los turnos elegidos y, si es una ampliación, a qué respuesta
amplía. La ventana las apila, la más nueva arriba, y al abrir una sesión vieja vienen con
la transcripción.

**Motivo:** en una reunión uno pide varias seguidas. Que pedir la siguiente borrara la
anterior hacía perder el hilo justo cuando más se necesita compararlas. Y guardarlas sólo
en memoria de la ventana significaba perderlas al cerrarla.

**Costo aceptado:** una tabla más y una decisión de retención que todavía no está tomada
— hoy no se borra nada. Cuando el archivo pese, habrá que decidir.

---

## ADR-029 — Respuesta corta primero, ampliación a pedido

**Decisión:** dos plantillas. `responder` contesta en menos de un segundo con la CLI
rápida. `ampliar` toma esa respuesta y la desarrolla — versión completa, dos alternativas
de tono y qué tener en cuenta — usando la CLI que razona.

**Motivo:** son dos necesidades distintas en momentos distintos. Mientras el otro habla
hace falta algo en pantalla ya, aunque sea corto, para ir pensando. Cuando hay un hueco,
conviene algo más trabajado. Medido: 0.73 s la corta, 14.83 s la ampliada.

**Implementación:** la plantilla declara `cli: ollama-razonando` y el servidor la usa si
está instalada; si no, cae a la de siempre en vez de fallar. Y como usa `{respuesta}`,
la ventana sabe que no puede ofrecerla suelta: aparece como botón sobre cada respuesta
ya escrita, no en la barra de la frase.

**La ampliación se pide sola.** `responder` declara `ampliar_con: ampliar` y la ventana
la dispara apenas llega la respuesta corta. No se lanzan las dos a la vez, aunque
"al mismo tiempo" sea lo que uno querría: ollama atiende una generación por vez, así que
pedir la larga en paralelo retrasaría justo la que se quiere leer ya — y además la larga
necesita la corta como entrada. Encadenadas, la corta está en pantalla a los 0.7 s y la
larga se escribe debajo mientras se lee. Se apaga desde Ajustes.

---

## ADR-030 — La ventana pinta el poco markdown que los modelos usan

**Decisión:** negrita, código y viñetas. Nada más.

**Motivo:** la respuesta ampliada viene con `**Full Version**` y listas, y en texto plano
se lee como ruido. Un renderizador de markdown completo sería una dependencia grande para
tres marcas.

**Seguridad:** el texto pasa por `escapeHtml` antes de cualquier reemplazo. Lo que
devuelve el modelo nunca llega al DOM como etiquetas — y ese texto viene de una
transcripción automática de lo que se dijo en una reunión, que no es una fuente confiable.

---

## ADR-031 — Las respuestas se agrupan: lo nuevo arriba, la ampliación pegada abajo

**Decisión:** cada pregunta forma un grupo. Los grupos se ordenan del más nuevo al más
viejo, arriba del todo el último. Dentro de un grupo, la respuesta corta va primero y su
ampliación pegada debajo.

**Motivo:** son dos necesidades que parecen contradecirse y no lo son. Lo último que
pediste tiene que estar arriba, porque en una reunión no hay tiempo de buscarlo. Pero la
ampliación no puede aparecer *encima* de la respuesta que amplía: mientras la larga se
escribe, empujaría hacia abajo justo el texto que se está leyendo.

Las dos primeras versiones eligieron una de las dos y perdieron la otra. Agrupar las
respeta a las dos.

**Costo aceptado:** insertar una tarjeta ya no es "arriba de todo" sino "después del
padre y de sus hermanas". Son cinco líneas de recorrido y evitan tener que elegir.

---

## ADR-032 — Un cuadro de instrucciones que viaja en cada prompt

**Decisión:** un `textarea` en la ventana con quién es el usuario, en qué situación está y
cómo quiere que le contesten. Va como `{instrucciones}` en `responder`, `ampliar`,
`aclarar` y `resumir`. Se guarda en la máquina, no en la base.

**Motivo:** el modelo sabe qué se dijo, pero no quién sos ni qué te estás jugando. La
misma frase pide respuestas muy distintas según si el que pregunta es un cliente enojado
o un compañero de equipo. Medido con `We should also discuss the migration timeline`:

- sin instrucciones: *"We don't have any immediate concerns regarding the migration timeline."*
- con "cliente que ya se quejó dos veces, no prometas fechas, tono firme pero
  conciliador": *"We understand your concern... We are actively managing this process with
  a focus on stability and thorough testing."*

**Decisiones de detalle.** El bloque se arma en el servidor, no en la plantilla: sin
instrucciones, la plantilla dejaría un encabezado colgando de una sección vacía. Se
recorta a 2000 caracteres, porque un prompt gigante hace más lento al modelo sin mejorar
la respuesta. Y no va en `traducir_bien`: una traducción no debería cambiar según quién
la pide.

**Costo aceptado:** se guarda en `localStorage`, así que es una sola instrucción para
todo, no una por reunión. Si en algún momento hace falta por sesión, va a la base.
