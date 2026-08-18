"""Captura vía sounddevice: micrófono real y audio del sistema a través de BlackHole 2ch.

Las dos pistas usan la misma clase; lo único que cambia es el dispositivo. Cada instancia
tiene su propio InputStream, y PortAudio le da a cada uno su propio hilo de audio de alta
prioridad. No se usa un Aggregate Device: ver ADR-003.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import unicodedata
from typing import Any, Iterator

import numpy as np
import sounddevice as sd
import soxr

from src.audio.base import TARGET_SR, AudioChunk, AudioSource, Track

log = logging.getLogger(__name__)

BLOCK_MS = 100

# Tope de la cola en bloques. Con BLOCK_MS=100 son 60 s de audio por pista. Si un
# consumidor lento la llena, se descartan bloques y se cuentan, en vez de crecer sin
# límite hasta quedarse sin memoria.
_QUEUE_MAX_BLOCKS = 600

# Cómo se llama un Multi-Output Device según el idioma del sistema. Si la salida activa
# no matchea ninguno, la pista `sys` probablemente esté muda.
_MULTI_OUTPUT_HINTS = (
    "multi-output",
    "multi output",
    "multiple output",
    "salida multiple",
    "multi-salida",
    "aggregate",
    "agregado",
)


def _fold(text: str) -> str:
    """Minúsculas y sin acentos, para comparar nombres de dispositivo.

    macOS en español los nombra `Micrófono de MacBook Pro`. Sin esto habría que escribir
    la tilde a mano en la línea de comandos.
    """
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def list_input_devices() -> list[dict[str, Any]]:
    return [d for d in sd.query_devices() if d["max_input_channels"] > 0]


def find_device(substr: str) -> int:
    """Índice del dispositivo de **entrada** cuyo nombre contiene ``substr``.

    Levanta RuntimeError con un mensaje accionable si no hay match o si hay varios.
    """
    needle = _fold(substr)
    devices = list_input_devices()
    matches = [d for d in devices if needle in _fold(d["name"])]

    if len(matches) == 1:
        return int(matches[0]["index"])

    available = "\n".join(
        f"    [{d['index']}] {d['name']}  ({d['max_input_channels']} ch)" for d in devices
    )

    if not matches:
        hint = ""
        if "blackhole" in needle:
            hint = (
                "\n  BlackHole no aparece como entrada. Revisá:\n"
                "    brew install --cask blackhole-2ch\n"
                "    sudo killall coreaudiod      # o reiniciá la Mac\n"
                "  Si tampoco así, puede faltar el permiso de micrófono para tu terminal:\n"
                "  Configuración del Sistema > Privacidad y seguridad > Micrófono."
            )
        raise RuntimeError(
            f"No hay ningún dispositivo de entrada que contenga {substr!r}.{hint}\n"
            f"  Entradas disponibles:\n{available}"
        )

    names = ", ".join(repr(d["name"]) for d in matches)
    raise RuntimeError(
        f"{substr!r} matchea varios dispositivos de entrada: {names}.\n"
        f"  Usá una subcadena más específica.\n"
        f"  Entradas disponibles:\n{available}"
    )


def check_default_output() -> str | None:
    """Advierte si la salida activa no parece un Multi-Output Device.

    Conectar auriculares o un monitor por HDMI cambia la salida por defecto y BlackHole
    deja de recibir audio: la pista `sys` queda muda sin ningún error. Devuelve el texto
    de la advertencia, o None si la salida parece correcta.
    """
    try:
        name = str(sd.query_devices(kind="output")["name"])
    except Exception as exc:  # noqa: BLE001 - no vale la pena romper la captura por esto
        return f"No pude leer el dispositivo de salida por defecto: {exc}"

    folded = _fold(name)
    if any(hint in folded for hint in _MULTI_OUTPUT_HINTS):
        return None

    return (
        f"La salida por defecto es {name!r}, que no parece un Multi-Output Device. "
        "La pista `sys` va a quedar muda. Elegí el dispositivo de salida múltiple en "
        "Configuración del Sistema > Sonido > Salida. Ver docs/SETUP_AUDIO.md."
    )


class BlackHoleSource(AudioSource):
    def __init__(self, track: Track, device: int | str, samplerate: int | None = None) -> None:
        self._track: Track = track
        self._device = find_device(device) if isinstance(device, str) else int(device)

        info = sd.query_devices(self._device)
        self._name = str(info["name"])
        self._channels = int(info["max_input_channels"])
        if self._channels < 1:
            raise RuntimeError(f"{self._name!r} no tiene canales de entrada.")

        self._device_sr = int(samplerate or info["default_samplerate"])
        self._blocksize = int(self._device_sr * BLOCK_MS / 1000)

        self._queue: queue.Queue[tuple[float, np.ndarray]] = queue.Queue(_QUEUE_MAX_BLOCKS)
        self._stream: sd.InputStream | None = None
        self._resampler: soxr.ResampleStream | None = None
        self._lock = threading.Lock()
        self._running = False

        # Escritos solo desde el hilo de audio, leídos desde el hilo principal. Enteros,
        # así que el GIL alcanza: no hacen falta locks y un lock ahí adentro sería un
        # riesgo de xrun.
        self.overflow_count = 0
        self.dropped_blocks = 0
        self.callback_count = 0

    @property
    def track(self) -> Track:
        return self._track

    @property
    def device_name(self) -> str:
        return self._name

    @property
    def device_samplerate(self) -> int:
        return self._device_sr

    @property
    def is_running(self) -> bool:
        return self._running

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        # Corre en el hilo de audio de PortAudio, con prioridad de tiempo real. Nada de
        # I/O, locks ni allocations grandes acá adentro: solo mezclar, remuestrear y
        # encolar. Los `status` se cuentan y se loguean después, desde stop().
        if status:
            self.overflow_count += 1

        self.callback_count += 1

        mono = indata.mean(axis=1, dtype=np.float32) if indata.ndim > 1 else indata

        resampler = self._resampler
        if resampler is None:
            return

        out = resampler.resample_chunk(mono)
        if out.size == 0:
            return

        try:
            self._queue.put_nowait((time.monotonic(), out))
        except queue.Full:
            self.dropped_blocks += 1

    def open(self) -> None:
        """Reserva el dispositivo y arma el remuestreador, sin empezar a capturar.

        Abrir un InputStream cuesta decenas de milisegundos. Si eso pasara dentro de
        start(), las dos pistas arrancarían escalonadas y la diferencia de duración
        entre ellas sería ese escalón. Separar open() de start() deja el escalón en
        microsegundos. Ver el criterio de skew < 2 % en docs/PLAN.md.
        """
        with self._lock:
            if self._stream is not None:
                return

            self._resampler = soxr.ResampleStream(
                self._device_sr, TARGET_SR, 1, dtype="float32", quality="HQ"
            )
            self._stream = sd.InputStream(
                device=self._device,
                channels=self._channels,
                samplerate=self._device_sr,
                blocksize=self._blocksize,
                dtype="float32",
                callback=self._callback,
            )

        log.info(
            "pista %s abierta: %s @ %d Hz, %d ch, bloques de %d muestras",
            self._track,
            self._name,
            self._device_sr,
            self._channels,
            self._blocksize,
        )

    def start(self) -> None:
        self.open()
        with self._lock:
            if self._running:
                return
            assert self._stream is not None
            self._stream.start()
            self._running = True

        log.debug("pista %s: capturando", self._track)

    def stop(self) -> None:
        with self._lock:
            # Un stream abierto pero nunca arrancado igual hay que cerrarlo: si no,
            # el dispositivo queda reservado hasta que muera el proceso.
            if not self._running and self._stream is None:
                return
            self._running = False

            stream, self._stream = self._stream, None
            resampler, self._resampler = self._resampler, None

        if stream is not None:
            stream.stop()
            stream.close()

        # Vaciar la cola interna del remuestreador: sin esto se pierden los últimos
        # milisegundos de cada pista.
        if resampler is not None:
            tail = resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
            if tail.size:
                try:
                    self._queue.put_nowait((time.monotonic(), tail))
                except queue.Full:
                    self.dropped_blocks += 1

        if self.overflow_count:
            log.warning(
                "pista %s: %d overflows de PortAudio en %d bloques",
                self._track,
                self.overflow_count,
                self.callback_count,
            )
        if self.dropped_blocks:
            log.warning(
                "pista %s: %d bloques descartados por cola llena (consumidor lento)",
                self._track,
                self.dropped_blocks,
            )

    def read(self) -> Iterator[AudioChunk]:
        while True:
            try:
                ts, pcm = self._queue.get(timeout=0.1)
            except queue.Empty:
                if not self._running:
                    return
                continue
            yield AudioChunk(track=self._track, ts_monotonic=ts, pcm=pcm)
