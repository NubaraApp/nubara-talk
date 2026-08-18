"""Elección de la fuente de audio.

Existe para que cambiar de BlackHole a captura nativa sea un argumento, no una edición:
fuera de ``src/audio/`` nadie nombra una clase concreta.
"""

from __future__ import annotations

from src.audio.base import AudioSource, Track

TAP = "tap"


def make_source(track: Track, device: str | int) -> AudioSource:
    """Devuelve la fuente de la pista. `device="tap"` usa el process tap nativo."""
    if isinstance(device, str) and device.strip().lower() == TAP:
        from src.audio.swift_tap import SwiftTapSource

        return SwiftTapSource(track)

    from src.audio.blackhole import BlackHoleSource

    return BlackHoleSource(track, device)
