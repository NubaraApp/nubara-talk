#!/usr/bin/env bash
# Crea el venv, instala dependencias y chequea el ruteo de audio.
# Idempotente: se puede correr las veces que haga falta.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY_BIN="${PY_BIN:-}"
if [[ -z "$PY_BIN" ]]; then
  for candidate in python3.12 /opt/homebrew/bin/python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PY_BIN="$(command -v "$candidate")"
      break
    fi
  done
fi

if [[ -z "$PY_BIN" ]]; then
  echo "ERROR: no encontré ningún Python. Instalá: brew install python@3.12" >&2
  exit 1
fi

PY_VER="$("$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [[ "$("$PY_BIN" -c 'import sys; print(sys.version_info >= (3, 11))')" != "True" ]]; then
  echo "ERROR: $PY_BIN es Python $PY_VER. Hace falta 3.11+." >&2
  echo "       brew install python@3.12 && PY_BIN=\$(brew --prefix)/bin/python3.12 $0" >&2
  exit 1
fi

echo "==> Python: $PY_BIN ($PY_VER)"

if [[ ! -d .venv ]]; then
  echo "==> Creando .venv"
  "$PY_BIN" -m venv .venv
else
  echo "==> .venv ya existe"
fi

echo "==> Instalando dependencias"
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt

echo
echo "==> Chequeo de audio"

HAL_DIR="/Library/Audio/Plug-Ins/HAL"
if [[ -d "$HAL_DIR/BlackHole2ch.driver" ]]; then
  echo "  [ok]   BlackHole 2ch instalado"
else
  echo "  [FALTA] BlackHole 2ch no está en $HAL_DIR"

  # brew puede registrar el cask y bajar el .pkg sin que el instalador haya corrido
  # nunca: pasa si el sudo falló. Reintentar `brew install` no arregla nada, porque
  # brew lo da por instalado y no hace nada.
  PKG="$(ls -1 /opt/homebrew/Caskroom/blackhole-2ch/*/BlackHole2ch-*.pkg 2>/dev/null | head -1 || true)"
  if [[ -n "$PKG" ]]; then
    echo "          El cask está registrado pero el instalador nunca corrió."
    echo "          Corré esto en una terminal interactiva (pide contraseña):"
    echo "            sudo installer -pkg $PKG -target /"
  else
    echo "          brew install --cask blackhole-2ch"
  fi
  echo "          sudo killall coreaudiod    # o reiniciá la Mac"
fi

# El driver puede estar instalado pero no visible para Core Audio si coreaudiod
# no se reinició. Por eso se chequean las dos cosas por separado.
./.venv/bin/python - <<'PYEOF'
import sys

try:
    import sounddevice as sd
except Exception as exc:  # noqa: BLE001 - el mensaje al usuario importa más que el tipo
    print(f"  [FALLO] no pude importar sounddevice: {exc}")
    sys.exit(0)

inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
names = [d["name"] for d in inputs]

if any("blackhole" in n.lower() for n in names):
    print("  [ok]   Core Audio ve BlackHole como entrada")
else:
    print("  [FALTA] Core Audio no ve BlackHole. Corré: sudo killall coreaudiod")

try:
    out = sd.query_devices(kind="output")
    print(f"  [info] salida por defecto: {out['name']}")
except Exception as exc:  # noqa: BLE001
    print(f"  [info] no pude leer la salida por defecto: {exc}")

print("  [info] entradas disponibles:")
for d in inputs:
    print(f"           {d['name']}  ({d['max_input_channels']} ch)")
PYEOF

echo
echo "Listo. Siguiente paso:"
echo "  1. Configurá el Multi-Output Device — docs/SETUP_AUDIO.md"
echo "  2. source .venv/bin/activate"
echo "  3. python -m src.spikes.f0_capture --seconds 20"
