"""Captura nativa del audio del sistema — F4. Stub.

Reemplaza a BlackHoleSource sin driver de terceros ni Multi-Output Device, usando
Core Audio Process Taps (macOS 14.4+) o ScreenCaptureKit desde un helper en Swift.
Ver ADR-002.
"""

from __future__ import annotations

from typing import Iterator

from src.audio.base import AudioChunk, AudioSource, Track

_NOT_YET = "SwiftTapSource llega en F4. Usá BlackHoleSource. Ver docs/PLAN.md."


class SwiftTapSource(AudioSource):
    def __init__(self, track: Track = "sys") -> None:
        raise NotImplementedError(_NOT_YET)

    @property
    def track(self) -> Track:
        raise NotImplementedError(_NOT_YET)

    @property
    def is_running(self) -> bool:
        raise NotImplementedError(_NOT_YET)

    def start(self) -> None:
        raise NotImplementedError(_NOT_YET)

    def stop(self) -> None:
        raise NotImplementedError(_NOT_YET)

    def read(self) -> Iterator[AudioChunk]:
        raise NotImplementedError(_NOT_YET)
