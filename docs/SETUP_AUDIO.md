# Setup de audio (F0)

macOS no deja capturar el audio del sistema sin ayuda. La solución de F0 es un driver
virtual: **BlackHole 2ch**. El sonido sale a la vez a los parlantes reales y a BlackHole,
y nosotros grabamos desde BlackHole como si fuera un micrófono.

En F4 esto se reemplaza por captura nativa. Ver [PLAN.md](PLAN.md).

## 1. Instalar BlackHole 2ch

```bash
brew install --cask blackhole-2ch
```

Pide contraseña de administrador: instala una extensión de audio a nivel sistema.

Después:

```bash
sudo killall coreaudiod
```

Eso reinicia el servidor de audio de macOS para que vea el driver nuevo. Si aun así no
aparece, reiniciá la Mac — el instalador lo pide explícitamente.

Verificar:

```bash
ls /Library/Audio/Plug-Ins/HAL/
```

Tiene que aparecer `BlackHole2ch.driver`.

### Si `brew list --cask` lo muestra pero el driver no está

Pasa cuando el `sudo` del instalador falla: brew registra el cask, baja el `.pkg` y
nunca corre el instalador. Reintentar `brew install` no sirve — brew lo da por instalado
y no hace nada. Corré el `.pkg` a mano:

```bash
sudo installer -pkg /opt/homebrew/Caskroom/blackhole-2ch/*/BlackHole2ch-*.pkg -target /
```

Confirmar que quedó bien:

```bash
pkgutil --pkgs | grep -i blackhole
```

## 2. Crear el Multi-Output Device

### Opción rápida: el helper

`scripts/make_multioutput.swift` crea exactamente el mismo objeto de Core Audio que la
GUI —un dispositivo agregado apilado— y lo deja como salida por defecto. No pide
contraseña: crear un agregado no requiere privilegios.

```bash
swiftc -O scripts/make_multioutput.swift -o /tmp/make_multioutput && /tmp/make_multioutput
```

Acepta subcadenas para elegir los dispositivos, por si tu salida real no son las bocinas
integradas:

```bash
/tmp/make_multioutput "AirPods" "BlackHole"
```

Es idempotente: si ya existe un agregado con el UID `com.granola-local.multioutput`, lo
destruye y lo vuelve a crear.

Para volver atrás, elegí tu salida normal en el menú de volumen. Para borrar el
dispositivo, sacalo desde Configuración de Audio MIDI con el `-`.

### Opción manual: la GUI

Sin esto vas a oír silencio: BlackHole se traga el audio y no lo devuelve a los parlantes.

1. Abrí **Configuración de Audio MIDI** (`/Applications/Utilities/Audio MIDI Setup.app`).
2. Botón `+` abajo a la izquierda → **Crear dispositivo de salida múltiple**.
3. Tildá **ambos**: tu salida real (`Bocinas de MacBook Pro`) y `BlackHole 2ch`.
4. En la columna **Corrección de deriva**, tildá `BlackHole 2ch` — **no** el dispositivo
   maestro. El maestro manda el reloj; el esclavo se corrige contra él.
5. Poné tu salida real primero en la lista: el primer dispositivo es el maestro de reloj.

## 3. Activar el Multi-Output

Menú de volumen (o Configuración del Sistema → Sonido → Salida) → elegí el
**Dispositivo de salida múltiple**.

> **Esto se desactiva solo.** Conectar auriculares, un monitor por HDMI o AirPods cambia
> la salida por defecto y la pista `sys` queda muda sin avisar. El spike detecta esto y
> te advierte al arrancar, pero conviene revisarlo a ojo antes de una reunión.

## 4. Control de volumen

Con el Multi-Output activo, las teclas de volumen **no funcionan**: macOS no sabe qué
dispositivo del agregado ajustar. Ajustá el volumen desde Configuración de Audio MIDI,
o desde la app que reproduce.

Esto no afecta el nivel de grabación de la pista `sys`: BlackHole recibe la señal antes
del control de volumen del hardware.

## 5. Permiso de micrófono

La primera vez, macOS pide permiso de micrófono para el proceso que corre Python
(Terminal, iTerm o tu IDE). Si lo negaste:

Configuración del Sistema → Privacidad y seguridad → Micrófono → activá tu terminal.

BlackHole cuenta como micrófono para macOS, así que **sin este permiso las dos pistas
quedan mudas**, no solo la del mic.

## 6. Verificar

```bash
source .venv/bin/activate
python -c "import sounddevice as sd; print(sd.query_devices())"
```

Tenés que ver `BlackHole 2ch` con canales de entrada > 0.

## Nombres de dispositivo en este equipo

macOS en español nombra los dispositivos integrados en español:

- Micrófono: `Micrófono de MacBook Pro`
- Salida real: `Bocinas de MacBook Pro`
- Audio del sistema: `BlackHole 2ch`

`find_device()` busca por subcadena y sin distinguir mayúsculas ni acentos, así que
`--mic-device "macbook"` alcanza.

## Captura nativa (F4) — estado y qué falta

`SwiftTapSource` usa Core Audio Process Taps: no necesita BlackHole ni Multi-Output. El
helper se compila solo la primera vez, en `build/granola-tap.app`.

```bash
python -m src.spikes.f0_capture --seconds 12 --sys-device tap
```

**Hoy la pista `sys` sale en silencio absoluto.** No es un bug de la cañería: el tap se
crea, el dispositivo agregado expone su canal de entrada, el formato es float32 mono a
48 kHz y los callbacks llegan cada 512 frames. Lo que llega es cero.

Es el comportamiento del sistema cuando falta el permiso de grabación de audio: no
devuelve un error, devuelve silencio. Y el permiso no se puede pedir desde acá, porque
`tccutil` no reconoce el bundle:

```
tccutil: No such bundle identifier "com.granola-local.tap"
```

El sistema sólo le da permiso a una app que conozca. Un binario firmado ad-hoc dentro de
`build/` no lo es.

**Para probarlo vos:**

1. Corré el spike desde tu propia terminal (no desde un agente): si aparece un diálogo de
   permiso, aceptalo.
2. Mirá **Configuración del Sistema > Privacidad y seguridad > Grabación de pantalla y
   audio del sistema**. Si aparece `granola-local tap`, activalo y volvé a correr.
3. Si no aparece, copiá la app a `/Applications` y abrila una vez para que el sistema la
   registre:

```bash
cp -R build/granola-tap.app /Applications/
open -a /Applications/granola-tap.app
```

Si después de eso sigue en silencio, la salida es empaquetar todo en la app de F5 (Tauri
ya produce un bundle firmado) y pedir el permiso desde ahí. El código del tap no cambia.

Mientras tanto, el camino de BlackHole sigue siendo el que funciona.
