# Benchmarks

Latencias medidas en este equipo: Apple M5 Pro, macOS 26.6, Python 3.12.

| Fase | Qué se mide | Valor | Fecha |
|------|-------------|-------|-------|
| F0 | Escalón de duración entre pistas (2 streams, mismo dispositivo) | 0.00 % | 2026-08-18 |
| F0 | Desfase de `ts_monotonic` entre pistas | 0.2 ms | 2026-08-18 |
| F0 | Latencia de arranque + cola del remuestreador | ~115 ms | 2026-08-18 |
| F0 | Overflows de PortAudio en 8 s | 0 | 2026-08-18 |

## Notas

**El ensayo de F0 usó el micrófono en las dos pistas**, porque BlackHole todavía no
estaba instalado. Eso valida la concurrencia de los dos streams, el remuestreo y el
alineamiento de timestamps, pero **no** valida el ruteo del audio del sistema, y el
0.00 % de escalón es optimista: las dos pistas compartían el mismo reloj de hardware.
Con BlackHole hay dos relojes distintos y va a aparecer deriva real. No importa para el
diseño (ADR-003), pero el número va a subir.

**El escalón entre pistas no es gratis.** Abrir un `InputStream` cuesta ~40 ms y
cerrarlo ~180 ms. Haciendo ambas cosas en secuencia, las pistas quedaban desfasadas
2.27 % en una captura de 8 s. Abrir las dos antes de arrancar ninguna, y cerrarlas en
hilos paralelos, lo bajó a 0.00 %.
