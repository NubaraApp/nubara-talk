"""Traducción fuera del camino crítico del STT.

El motor de transcripción llama a sus callbacks desde su propio hilo de inferencia: si
la traducción se hiciera ahí, cada segmento traducido retrasaría al siguiente turno en
transcribirse. Por eso la traducción vive en su propio hilo con su propia cola.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from src.stt.engine import Result
from src.translate.nllb import Translator, flores

log = logging.getLogger(__name__)

_STOP = object()


@dataclass(slots=True)
class Translated:
    result: Result
    text: str
    latency: float = 0.0
    dropped: bool = False


@dataclass(order=True, slots=True)
class _Job:
    priority: int
    seq: int
    result: Result = field(compare=False)


class TranslationWorker:
    """Cola de traducción con descarte de parciales viejos.

    Misma política que el motor de STT: los finales van primero y un parcial se tira si
    ya entró uno más nuevo de la misma pista, porque su texto ya está obsoleto antes de
    salir de la cola.
    """

    def __init__(self, translator: Translator, tgt: str, on_result=None) -> None:
        self._tr = translator
        self._tgt = tgt
        self._on_result = on_result
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = 0
        self._latest_partial: dict[str, int] = {}
        self._thread = threading.Thread(target=self._run, name="translate", daemon=True)
        self._running = False
        self.translated = 0
        self.dropped_partials = 0
        self.skipped_same_lang = 0
        self.latencies: list[float] = []

    def start(self) -> None:
        self._running = True
        self._thread.start()

    def submit(self, result: Result) -> None:
        if not result.text.strip():
            return
        self._seq += 1
        kind = result.turn.kind
        if kind == "partial":
            self._latest_partial[result.turn.track] = self._seq
        self._queue.put(_Job(0 if kind == "final" else 1, self._seq, result))

    def stop(self, timeout: float = 30.0) -> None:
        if not self._running:
            return
        self._queue.put(_Job(2, 1 << 30, None))  # type: ignore[arg-type]
        self._thread.join(timeout)
        self._running = False

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job.result is None:
                return
            turn = job.result.turn
            if turn.kind == "partial" and self._latest_partial.get(turn.track, 0) > job.seq:
                self.dropped_partials += 1
                continue
            t0 = time.monotonic()
            src = job.result.lang or "en"
            # Mi propia pista suele estar ya en el idioma destino. Traducirla gasta CPU
            # para devolver casi lo mismo, así que pasa de largo.
            try:
                if flores(src) == flores(self._tgt):
                    self.skipped_same_lang += 1
                    continue
            except ValueError:
                log.warning("idioma '%s' sin mapeo FLORES; no se traduce el turno", src)
                continue
            try:
                text = self._tr.translate(job.result.text, src, self._tgt)
            except Exception:
                log.exception("falló la traducción del turno %s/%s", turn.track, turn.index)
                continue
            latency = time.monotonic() - t0
            self.latencies.append(latency)
            self.translated += 1
            if self._on_result is not None:
                self._on_result(Translated(job.result, text, latency))
