"""Une captura, VAD, motor y base: una pista entra, segmentos salen."""

from __future__ import annotations

import logging
import sqlite3
import threading

from src.audio.base import AudioSource
from src.stt.engine import Result, TranscriptionEngine
from src.stt.vad import TurnDetector

log = logging.getLogger(__name__)


class TrackPipeline:
    """Drena una pista, la corta en turnos y encola cada turno para transcribir.

    Corre en su propio hilo. Las dos pistas tienen un pipeline cada una y comparten el
    motor: el paralelismo está en el VAD y la captura, no en la inferencia.
    """

    def __init__(
        self,
        source: AudioSource,
        detector: TurnDetector,
        engine: TranscriptionEngine,
        session_start: float,
    ) -> None:
        self._source = source
        self._detector = detector
        self._engine = engine
        self._session_start = session_start
        self._thread = threading.Thread(target=self._run, name=f"vad-{detector.track}", daemon=True)
        self.turns_final = 0
        self.turns_partial = 0

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _submit(self, turns) -> None:
        for turn in turns:
            if turn.kind == "final":
                self.turns_final += 1
            else:
                self.turns_partial += 1
            self._engine.submit(turn, self._session_start + turn.t_end)

    def _run(self) -> None:
        for chunk in self._source.read():
            self._submit(self._detector.push(chunk))
        self._submit(self._detector.flush())


class SessionWriter:
    """Persiste los resultados. Es el callback del motor, así que corre en su hilo."""

    def __init__(self, conn: sqlite3.Connection, session_id: str) -> None:
        self._conn = conn
        self._session_id = session_id
        self._lock = threading.Lock()
        self.written = 0

    def __call__(self, result: Result) -> None:
        from src.store.db import upsert_segment

        turn = result.turn
        with self._lock:
            upsert_segment(
                self._conn,
                session_id=self._session_id,
                track=turn.track,
                turn_index=turn.index,
                t_start=turn.t_start,
                t_end=turn.t_end,
                text=result.text,
                lang=result.lang,
                status="confirmed" if turn.kind == "final" else "provisional",
            )
            self.written += 1
