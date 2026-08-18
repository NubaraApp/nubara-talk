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

La correlación cercana a cero confirma que son dos fuentes independientes: la pista `sys`
no es el micrófono captando los parlantes, es la señal digital antes de salir al hardware.

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
