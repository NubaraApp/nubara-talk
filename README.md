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

## Estado

**F0 — spike de captura dual.** Nada de STT, traducción ni UI todavía.
Ver [docs/PLAN.md](docs/PLAN.md).

## Requisitos

- macOS 14+ en Apple Silicon (probado en M5 Pro / macOS 26.6)
- Python 3.12
- [BlackHole 2ch](https://existential.audio/blackhole/) + un Multi-Output Device

## Setup

```bash
./scripts/setup.sh
```

Luego seguí [docs/SETUP_AUDIO.md](docs/SETUP_AUDIO.md) para configurar el ruteo de audio.

## Correr el spike F0

```bash
source .venv/bin/activate
python -m src.spikes.f0_capture --seconds 20
```

Poné música sonando **y** hablá al micrófono mientras corre. El script imprime un
veredicto PASA/FALLA y escribe `data/f0/mic.wav` y `data/f0/sys.wav`.

## Licencia

MIT
