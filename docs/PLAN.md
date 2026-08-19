# Plan de fases

Cada fase tiene un criterio de salida verificable. No se avanza sin cumplirlo.

## F0 — Captura dual

Dos pistas de audio independientes y simultáneas: `mic` y `sys`.

**Entregable:** `src/audio/base.py`, `src/audio/blackhole.py`, `src/spikes/f0_capture.py`.

**Criterios de salida — cumplidos el 2026-08-18:**

- [x] `mic.wav` y `sys.wav` existen y su duración difiere en menos del 2 % — 0.00 %
- [x] `rms > 0.001` en ambas pistas — mic 0.0100, sys 0.1492
- [x] Cero overflows, o menos de 3 en 20 s — 0
- [x] Los timestamps de ambas pistas son comparables entre sí (mismo reloj
      `time.monotonic()`) — desfase máximo 11.3 ms
- [x] `python -m src.spikes.f0_capture` corre limpio desde el repo raíz

Se valida con música sonando y hablando al micrófono a la vez. La correlación entre las
dos pistas dio 0.0011: son fuentes independientes, no la misma señal duplicada.

## F1 — STT en vivo

VAD con Silero para cortar por turnos de habla. `mlx-whisper` en Apple Silicon,
fallback a `faster-whisper` int8 en Intel. **Una sola instancia del modelo**, con lock
y cola de trabajos: las dos pistas comparten el modelo, no cargan uno cada una.

Escritura a SQLite: segmentos `provisional` al vuelo, promovidos a `confirmed` cuando
el turno cierra.

**Criterios de salida — cumplidos el 2026-08-18:**

- [x] Latencia mediana desde fin de turno hasta texto en pantalla < 2 s — **0.50 s**,
      p95 0.59 s
- [x] Las dos pistas transcriben en paralelo sin bloquearse mutuamente — 8 turnos cada
      una, latencias medianas de 0.43 s y 0.52 s
- [x] Los segmentos de ambas pistas se intercalan correctamente por `t_start`
- [x] Números medidos en `BENCHMARKS.md`

Pendiente de validar con dos hablantes reales: en la prueba, el micrófono captó los
parlantes, así que ambas pistas transcribieron el mismo discurso.

## F2 — Puente LLM por CLI

Seleccionar un fragmento de la transcripción, elegir un prompt de `config/prompts.yaml`,
invocar la CLI del LLM por `subprocess` y mostrar la respuesta.

**Sin claves de API en el repo.** Se usa la CLI ya autenticada del usuario.

**Criterios de salida — cumplidos el 2026-08-18:**

- [x] Plantillas cargadas desde `config/prompts.yaml`, no hardcodeadas — 4 plantillas, y
      los marcadores se validan al cargar el archivo
- [x] `subprocess` con timeout y manejo de error si la CLI no está autenticada —
      verificado contra una CLI inexistente, un proceso colgado y `claude` sin login
- [x] Cero secretos en el repo — escaneo de patrones en cada corrida del spike

## F3 — Traducción local

NLLB-200 distilled 600M sobre CTranslate2. Subtitulado en vivo, local.

La CLI del LLM se reserva para "traducí esto bien" bajo demanda; **no** se usa para el
subtitulado en vivo (demasiado lento y caro en latencia).

**Criterios de salida:**

- [x] Traducción de un segmento típico < 300 ms — mediana 199 ms, p95 244 ms sobre los
      16 segmentos de una sesión real
- [x] `text_trad` poblado en SQLite y buscable vía FTS5 — el trigger de UPDATE reindexa,
      verificado buscando una palabra que sólo existe en la traducción
- [x] Traducción en vivo durante la captura — `f1_transcribe --traducir es`
- [x] Sin red: el spike corre con `HF_HUB_OFFLINE=1`

## F4 — Captura nativa

`SwiftTapSource` reemplaza a `BlackHoleSource` detrás de la misma interfaz
`AudioSource`. Core Audio Process Taps (macOS 14.4+) o ScreenCaptureKit: sin driver
de terceros, sin Multi-Output, sin que el usuario pueda romper el ruteo por accidente.

**Criterios de salida:**

- [x] Captura del audio del sistema sin BlackHole — funciona, con una condición: hay
      que lanzar la app desde `/Applications`. El tap transcribió 3 de 3 turnos. El mismo
      helper corrido desde la terminal sigue en silencio: es el permiso, no el código.
      Ver ADR-021.
- [ ] Desfase de duración < 2 % — medido antes de tener audio daba 5,98 %: arrancar y
      parar un subproceso cuesta más que abrir un `InputStream`. Sin volver a medirlo
      con audio real, queda abierto.
- [x] Ni un cambio fuera de `src/audio/` — los spikes llaman a `make_source()` y no
      nombran ninguna clase concreta. Cambiar de backend es un argumento, no una edición.

**Cómo usarlo:** tildar *captura nativa* en la ventana. BlackHole deja de hacer falta.

## F5 — UI

Tauri con sidecar Python. Se comunican por WebSocket local.

**Criterios de salida:**

- [x] Transcripción en vivo de ambas pistas, visualmente distinguibles — `Ellos` a la
      izquierda en azul, `Yo` a la derecha en verde; los parciales van en itálica y
      apagados hasta que el turno cierra
- [x] Selección de fragmento y disparo de prompt desde la UI — clic para elegir, las
      plantillas de `config/prompts.yaml` salen como botones, y la respuesta dice si la
      CLI fue local o remota
- [x] Búsqueda FTS5 sobre sesiones pasadas — con el error de sintaxis de FTS5 devuelto
      al usuario en vez de tragado
- [x] Traducción en vivo debajo de cada burbuja
- [x] Acciones al pasar el mouse por una frase, sin ir a ningún panel
- [x] La respuesta sale en el idioma del fragmento
- [x] Ancho del panel ajustable, y se recuerda
- [x] La respuesta se escribe a medida que sale
- [x] Respuesta corta inmediata y ampliación a pedido
- [x] Las respuestas se guardan y se releen; abrir una sesión vieja las trae
- [x] Cuadro de instrucciones: quién sos y cómo querés que te contesten
- [x] Ventana nativa en Tauri que lanza y mata el sidecar
- [x] Las respuestas se agrupan por pregunta; sólo queda abierto el último grupo
- [x] *Responder al último* elige el turno más nuevo de la transcripción, no el de una
      pista fija — ver ADR-033

## F6 — Resumen de la reunión

Un botón que manda la conversación entera al LLM y devuelve un resumen detallado, para
el final de la reunión. Ver ADR-034.

**Criterios de salida:**

- [x] El resumen sale con las secciones pedidas — 6 de 6 en `ee0f5ea16438` y en
      `4031177656b3`
- [x] Se saca el eco entre pistas antes de mandar — 6 turnos a 3 en `4031177656b3`
- [x] Si no entra en el contexto, se resume por tramos — 160 turnos y 7745 caracteres
      en `ee0f5ea16438` salieron en 2 tramos
- [x] Queda guardado y vuelve al reabrir la sesión — es una fila de `answers`
- [x] Tarda menos de 300 s — 31,4 s con 160 turnos, 14,1 s con 3
- [x] La sesión queda nombrada por lo que dice el resumen, y el panel lo muestra en vez
      de la hora — 0,46 s la llamada del título; ver ADR-035

**Sin verificar:** todo lo medido salió de reuniones de prueba de pocos minutos. No se
probó con una reunión de una hora, que es donde el resumen por tramos empieza a importar
de verdad.

## F7 — La app abre en las reuniones

La app entra por la lista de reuniones, no por una transcripción vacía. Ver ADR-036.

**Criterios de salida:**

- [x] Al abrir se ven las reuniones, agrupadas por día y con el título del resumen —
      24 filas bajo `Hoy` y `sin fecha`
- [x] Un clic abre la reunión con su transcripción y sus respuestas; `‹ Reuniones`
      vuelve al inicio
- [x] Volver al inicio con el micrófono abierto avisa cuál es la reunión viva y la
      devuelve sin recargar la transcripción — `viewing` queda en `null`
- [x] Buscar por título filtra la lista mientras se escribe, y dice cuando no hay nada
- [x] Una sesión recién creada, todavía fuera del listado, igual sale como grabando

**Sin verificar:** el estado de grabación se probó inyectando el mensaje `state` en la
ventana, no con audio real — no hay dispositivos de audio en el entorno donde se
desarrolló. Falta correrlo con una reunión de verdad.
