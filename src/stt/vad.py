"""Detección de turnos de habla con Silero VAD.

Corta por turnos, nunca por ventana fija: un corte en mitad de una palabra degrada la
transcripción de los dos fragmentos resultantes. Ver ADR-005.

`VADIterator`, el helper que viene con silero-vad, no alcanza: con habla continua nunca
emite el fin del turno, así que un orador que habla dos minutos seguidos no produciría
ni una línea de transcripción. Acá hay una máquina de estados propia que además corta por
duración máxima y emite parciales mientras el turno sigue abierto.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import numpy as np
import torch

from src.audio.base import TARGET_SR, AudioChunk, Track

log = logging.getLogger(__name__)

# El VAD corre en el hilo de cada pista y cuesta 0.07 ms por frame: no necesita el pool
# de hilos de torch, y dejarlo suelto lo hace competir con los hilos de audio.
torch.set_num_threads(1)

# Silero exige exactamente 512 muestras por frame a 16 kHz. No es configurable.
FRAME = 512
FRAME_MS = FRAME / TARGET_SR * 1000

TurnKind = Literal["partial", "final"]


def _load_model() -> torch.nn.Module:
    from silero_vad import load_silero_vad

    return load_silero_vad()


@dataclass(frozen=True, slots=True)
class SpeechTurn:
    track: Track
    index: int  # contador por pista; identifica al turno entre parciales y final
    kind: TurnKind
    t_start: float  # segundos desde el inicio de la sesión
    t_end: float
    pcm: np.ndarray
    forced: bool = False  # cortado por duración máxima, no por silencio

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start


class TurnDetector:
    """Convierte un flujo de AudioChunk en turnos de habla.

    Los tiempos salen de contar muestras a partir de un único anclaje al reloj compartido,
    no de la marca de cada bloque: contar muestras no acumula el jitter de la entrega de
    bloques, y las dos pistas quedan igual sobre la misma línea de tiempo.
    """

    def __init__(
        self,
        track: Track,
        session_start: float,
        model: torch.nn.Module | None = None,
        threshold: float = 0.5,
        min_silence_ms: float = 400.0,
        min_speech_ms: float = 200.0,
        pad_ms: float = 200.0,
        max_turn_s: float = 15.0,
        partial_every_s: float = 2.5,
    ) -> None:
        self.track = track
        # Una instancia por pista, siempre. El modelo es un LSTM con estado: compartirlo
        # mezcla el contexto de las dos pistas y, peor, corrompe el heap de torch —
        # `BUG IN CLIENT OF LIBMALLOC` dentro de slow_conv2d_forward_cpu, proceso muerto
        # con SIGTRAP. Por eso se carga acá adentro y no se recibe por parámetro salvo
        # en tests. Pesa ~2 MB: cargarlo dos veces no cuesta nada.
        self._model = model if model is not None else _load_model()
        self._session_start = session_start
        self._threshold = threshold
        self._min_silence_frames = max(1, int(min_silence_ms / FRAME_MS))
        self._min_speech_frames = max(1, int(min_speech_ms / FRAME_MS))
        self._pad_frames = max(0, int(pad_ms / FRAME_MS))
        self._max_turn_frames = max(1, int(max_turn_s * 1000 / FRAME_MS))
        self._partial_frames = max(1, int(partial_every_s * 1000 / FRAME_MS))

        self._anchor: float | None = None  # t de la sesión para la muestra 0 de esta pista
        self._buffer = np.zeros(0, dtype=np.float32)
        self._frames_seen = 0

        self._pre: list[np.ndarray] = []  # pre-roll para no cortar el ataque de la palabra
        self._speech: list[np.ndarray] = []
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self._turn_start_frame = 0
        self._frames_at_last_partial = 0
        self._index = 0

    def _t(self, frame_number: int) -> float:
        assert self._anchor is not None
        return self._anchor + frame_number * FRAME / TARGET_SR

    def _emit(self, kind: TurnKind, frames: list[np.ndarray], end_frame: int, forced: bool):
        if not frames:
            return None
        pcm = np.concatenate(frames)
        return SpeechTurn(
            track=self.track,
            index=self._index,
            kind=kind,
            t_start=self._t(self._turn_start_frame),
            t_end=self._t(end_frame),
            pcm=pcm,
            forced=forced,
        )

    def push(self, chunk: AudioChunk) -> list[SpeechTurn]:
        if self._anchor is None:
            # El bloque llega ya completo: su marca es el final, así que la muestra 0
            # de este bloque ocurrió una duración de bloque antes.
            self._anchor = chunk.ts_monotonic - chunk.duration - self._session_start

        self._buffer = np.concatenate([self._buffer, chunk.pcm])
        out: list[SpeechTurn] = []

        while len(self._buffer) >= FRAME:
            frame, self._buffer = self._buffer[:FRAME], self._buffer[FRAME:]
            out.extend(self._step(frame))

        return out

    def _step(self, frame: np.ndarray) -> list[SpeechTurn]:
        with torch.no_grad():
            prob = float(self._model(torch.from_numpy(frame.copy()), TARGET_SR))

        self._frames_seen += 1
        voiced = prob >= self._threshold
        out: list[SpeechTurn] = []

        if not self._in_speech:
            self._pre.append(frame)
            if len(self._pre) > self._pad_frames + self._min_speech_frames:
                self._pre.pop(0)

            self._speech_run = self._speech_run + 1 if voiced else 0
            if self._speech_run >= self._min_speech_frames:
                self._in_speech = True
                self._silence_run = 0
                self._speech = list(self._pre)
                self._pre = []
                self._turn_start_frame = self._frames_seen - len(self._speech)
                self._frames_at_last_partial = self._frames_seen
            return out

        self._speech.append(frame)
        self._silence_run = self._silence_run + 1 if not voiced else 0
        open_frames = self._frames_seen - self._turn_start_frame

        if self._silence_run >= self._min_silence_frames:
            # Recortar el silencio de más, dejando el pad: el silencio largo al final
            # confunde a Whisper y lo hace alucinar texto.
            keep = max(1, len(self._speech) - max(0, self._silence_run - self._pad_frames))
            turn = self._emit("final", self._speech[:keep], self._turn_start_frame + keep, False)
            if turn is not None:
                out.append(turn)
            self._reset_turn()
            return out

        if open_frames >= self._max_turn_frames:
            turn = self._emit("final", self._speech, self._frames_seen, True)
            if turn is not None:
                out.append(turn)
            # El habla sigue: se abre un turno nuevo sin pasar por silencio.
            self._index += 1
            self._speech = []
            self._turn_start_frame = self._frames_seen
            self._frames_at_last_partial = self._frames_seen
            return out

        if self._frames_seen - self._frames_at_last_partial >= self._partial_frames:
            self._frames_at_last_partial = self._frames_seen
            turn = self._emit("partial", self._speech, self._frames_seen, False)
            if turn is not None:
                out.append(turn)

        return out

    def _reset_turn(self) -> None:
        self._index += 1
        self._in_speech = False
        self._speech = []
        self._pre = []
        self._speech_run = 0
        self._silence_run = 0

    def flush(self) -> list[SpeechTurn]:
        """Cierra el turno abierto al terminar la captura."""
        if not self._in_speech:
            return []
        turn = self._emit("final", self._speech, self._frames_seen, True)
        self._reset_turn()
        return [turn] if turn is not None else []
