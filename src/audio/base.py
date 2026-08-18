"""Interfaz de captura de audio.

Existe para que F4 pueda cambiar BlackHole por captura nativa sin tocar nada fuera de
``src/audio/``. Por eso no filtra nada de sounddevice: ni índices de dispositivo, ni
callbacks, ni tipos de PortAudio.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, Literal

import numpy as np

Track = Literal["mic", "sys"]

TARGET_SR = 16_000


@dataclass(frozen=True, slots=True)
class AudioChunk:
    track: Track

    # Momento en que el bloque terminó de capturarse, en el reloj de time.monotonic().
    # Es el mismo reloj para las dos pistas: por eso son comparables entre sí aunque
    # cada stream corra contra un reloj de hardware distinto. Ver ADR-003.
    ts_monotonic: float

    # Mono, float32, TARGET_SR Hz, rango [-1, 1].
    pcm: np.ndarray

    @property
    def duration(self) -> float:
        return len(self.pcm) / TARGET_SR


class AudioSource(ABC):
    """Una fuente de audio de una sola pista, arrancable y parable."""

    @property
    @abstractmethod
    def track(self) -> Track: ...

    @property
    @abstractmethod
    def is_running(self) -> bool: ...

    @abstractmethod
    def start(self) -> None:
        """Arranca la captura. Volver a llamarlo estando corriendo no hace nada."""

    @abstractmethod
    def stop(self) -> None:
        """Para la captura. Los bloques ya capturados siguen disponibles en read()."""

    @abstractmethod
    def read(self) -> Iterator[AudioChunk]:
        """Entrega bloques a medida que llegan.

        Termina cuando la fuente se paró y no queda nada pendiente. Consumir esto desde
        más de un hilo a la vez reparte los bloques entre ellos: no los duplica.
        """

    def __enter__(self) -> AudioSource:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
