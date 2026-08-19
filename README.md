# granola-local

App de escritorio para macOS que captura, transcribe y traduce reuniones **en local**.
Sin API de pago, sin telemetría, sin red.

## Qué hace

- Captura **micrófono** y **audio del sistema** en dos pistas separadas y simultáneas.
- Transcribe ambas en vivo, localmente.
- Traduce los diálogos localmente.
- Permite seleccionar un fragmento y pedirle a un LLM —invocado **por CLI**, no por
  API HTTP— que redacte una respuesta usando prompts preconfigurados.

Caso de uso: reuniones en inglés donde necesitás entender y responder.

## Por qué dos pistas

No hay diarización de hablantes. No hace falta: `mic` sos vos, `sys` son los demás.
Ver [docs/DECISIONS.md](docs/DECISIONS.md).

Ver [docs/PLAN.md](docs/PLAN.md) para el estado de cada fase.

## Requisitos

- macOS 14+ en Apple Silicon (probado en M5 Pro / macOS 26.6)
- Python 3.12
- [BlackHole 2ch](https://existential.audio/blackhole/) + un Multi-Output Device

## Setup

```bash
./scripts/setup.sh
```

Para el ruteo de audio, el helper crea el Multi-Output Device sin pasar por la GUI:

```bash
swiftc -O scripts/make_multioutput.swift -o /tmp/make_multioutput && /tmp/make_multioutput
```

Detalles y pasos manuales en [docs/SETUP_AUDIO.md](docs/SETUP_AUDIO.md).

## Correr el spike F0

```bash
source .venv/bin/activate
python -m src.spikes.f0_capture --seconds 20
```

Poné música sonando **y** hablá al micrófono mientras corre. El script imprime un
veredicto PASA/FALLA y escribe `data/f0/mic.wav` y `data/f0/sys.wav`.

## Correr el spike F1

```bash
source .venv/bin/activate
python -m src.spikes.f1_transcribe --seconds 60
```

Transcribe las dos pistas en vivo y las guarda en `data/granola.sqlite`. `*` marca turno
cerrado; las líneas sin marca son parciales. La primera corrida descarga el modelo
(~1.6 GB) y tarda unos 30 s de más.

Idioma por pista con `--lang-mic` y `--lang-sys`, o `auto` para detectarlo:

```bash
python -m src.spikes.f1_transcribe --lang-mic es --lang-sys en
```

## Resumir una reunión (F6)

Al terminar, el botón **Resumen** de la ventana manda la conversación entera al LLM y
devuelve un resumen con temas, decisiones, tareas, preguntas abiertas y datos. Queda
guardado con las demás respuestas, así que vuelve solo al reabrir la sesión.

Sin abrir la ventana:

```bash
python -m src.spikes.f6_resumen                    # la sesión más larga de la base
python -m src.spikes.f6_resumen --sesion <id>
python -m src.spikes.f6_resumen --solo-material    # qué se le mandaría, sin llamar al LLM
```

## Abrir la app (F5)

```bash
npm install
npx tauri dev
```

La ventana lanza el sidecar de Python sola. Si preferís verla en el navegador, sin Tauri:

```bash
PYTHONPATH=. .venv/bin/python -m src.ui.server
open src/ui/web/index.html
```

El sidecar escucha sólo en `127.0.0.1:8765`. No es alcanzable desde otra máquina.

## Traducción local (F3)

La primera vez hay que convertir el modelo (baja ~2.5 GB y deja 621 MB en disco):

```bash
.venv/bin/ct2-transformers-converter --model facebook/nllb-200-distilled-600M \
  --output_dir models/nllb-200-distilled-600M-ct2 --quantization int8 \
  --copy_files tokenizer.json tokenizer_config.json special_tokens_map.json \
  sentencepiece.bpe.model
```

Traducir la última sesión de la base y medir la latencia:

```bash
python -m src.spikes.f3_translate
```

Traducir en vivo mientras se captura:

```bash
python -m src.spikes.f1_transcribe --seconds 60 --traducir es
```

## Pedirle algo al LLM (F2)

```bash
python -m src.spikes.f2_respond --list-prompts
```

```bash
python -m src.spikes.f2_respond --prompt responder --turns 3
```

Toma los últimos segmentos de la sesión, arma el prompt con una plantilla de
`config/prompts.yaml` y se lo manda a la CLI por `subprocess`. `--dry-run` muestra el
prompt sin invocar nada.

Por defecto usa la primera CLI instalada de la lista, que empieza por una local: el
fragmento de la reunión no sale de la máquina. `--cli claude` la manda a una remota, y el
spike avisa cuál eligió antes de enviar nada.

**No hay ninguna clave de API en el repo.** Se usa la CLI que ya tengas autenticada.

## Estado

**Las seis fases cerradas.** F4 (captura nativa, sin BlackHole) funciona lanzando la app
desde `/Applications`: ver `docs/SETUP_AUDIO.md`.

## Licencia

MIT
