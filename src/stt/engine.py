"""Motor de transcripción: una sola instancia del modelo, compartida por las dos pistas.

Cargar un modelo por pista duplica la memoria y hace que las dos compitan por la misma
GPU, empeorando la latencia de ambas. Acá hay un solo modelo detrás de un lock, y las
dos pistas le mandan trabajo por una cola con prioridad. Ver ADR-004.
"""

from __future__ import annotations

import logging
import platform
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import numpy as np

from src.audio.base import Track
from src.stt.vad import SpeechTurn

log = logging.getLogger(__name__)

DEFAULT_MLX_REPO = "mlx-community/whisper-large-v3-turbo"
DEFAULT_FASTER_MODEL = "large-v3-turbo"

# Los finales se atienden antes que los parciales: el parcial es una comodidad visual,
# el final es el que queda en la base.
_PRIORITY = {"final": 0, "partial": 1}


@dataclass
class Result:
    turn: SpeechTurn
    text: str
    lang: str | None
    latency: float  # desde que terminó el audio del turno hasta que hubo texto
    compute: float  # solo la inferencia


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    turn: SpeechTurn = field(compare=False)
    audio_end_wall: float = field(compare=False)


class Backend(Protocol):
    name: str

    def load(self) -> None: ...

    def transcribe(self, pcm: np.ndarray, language: str | None) -> tuple[str, str | None]: ...


class MlxBackend:
    """Whisper sobre MLX. Usa la GPU por Metal."""

    def __init__(self, repo: str = DEFAULT_MLX_REPO) -> None:
        self.name = f"mlx-whisper:{repo}"
        self._repo = repo

    def load(self) -> None:
        import mlx_whisper

        # mlx_whisper cachea el modelo por repo. Una transcripción de silencio fuerza la
        # descarga y la carga acá, y no en el primer turno real de la reunión.
        mlx_whisper.transcribe(
            np.zeros(16_000, dtype=np.float32), path_or_hf_repo=self._repo, language="en"
        )

    def transcribe(self, pcm: np.ndarray, language: str | None) -> tuple[str, str | None]:
        import mlx_whisper

        result = mlx_whisper.transcribe(pcm, path_or_hf_repo=self._repo, language=language)
        return str(result.get("text", "")).strip(), result.get("language")


class FasterWhisperBackend:
    """Fallback para Intel: CTranslate2 int8 en CPU. No hay CUDA en Mac."""

    def __init__(self, model: str = DEFAULT_FASTER_MODEL) -> None:
        self.name = f"faster-whisper:{model}"
        self._model_name = model
        self._model = None

    def load(self) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper no está instalado. Es el fallback para Intel:\n"
                "  pip install faster-whisper"
            ) from exc

        self._model = WhisperModel(self._model_name, device="cpu", compute_type="int8")

    def transcribe(self, pcm: np.ndarray, language: str | None) -> tuple[str, str | None]:
        assert self._model is not None
        segments, info = self._model.transcribe(pcm, language=language)
        return "".join(s.text for s in segments).strip(), info.language


def default_backend() -> Backend:
    if platform.machine() == "arm64":
        return MlxBackend()
    log.warning("no es Apple Silicon: usando faster-whisper en CPU, va a ser más lento")
    return FasterWhisperBackend()


class TranscriptionEngine:
    def __init__(
        self,
        on_result: Callable[[Result], None],
        backend: Backend | None = None,
        language_by_track: dict[Track, str | None] | None = None,
    ) -> None:
        self._backend = backend or default_backend()
        self._on_result = on_result
        self._lang = language_by_track or {}

        self._queue: queue.PriorityQueue[_Job | None] = queue.PriorityQueue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._seq = 0
        self._latest_partial: dict[Track, int] = {}

        self.dropped_partials = 0
        self.done = 0

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def warmup(self) -> float:
        t0 = time.monotonic()
        self._backend.load()
        return time.monotonic() - t0

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="stt", daemon=True)
        self._worker.start()

    def stop(self, drain: bool = True) -> None:
        if self._worker is None:
            return
        if not drain:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
        self._queue.put(None)
        self._worker.join(timeout=120.0)
        self._worker = None

    def submit(self, turn: SpeechTurn, audio_end_wall: float) -> None:
        self._seq += 1
        if turn.kind == "partial":
            self._latest_partial[turn.track] = self._seq
        self._queue.put(_Job(_PRIORITY[turn.kind], self._seq, turn, audio_end_wall))

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return

            # Si mientras este parcial esperaba llegó otro más nuevo de la misma pista,
            # este ya no sirve: mostrar texto viejo es peor que no mostrar nada.
            if job.turn.kind == "partial" and self._latest_partial.get(job.turn.track, 0) > job.seq:
                self.dropped_partials += 1
                continue

            try:
                t0 = time.monotonic()
                with self._lock:
                    text, lang = self._backend.transcribe(
                        job.turn.pcm, self._lang.get(job.turn.track)
                    )
                compute = time.monotonic() - t0
            except Exception:
                log.exception("falló la transcripción de %s #%d", job.turn.track, job.turn.index)
                continue

            self.done += 1
            if not text:
                continue

            self._on_result(
                Result(
                    turn=job.turn,
                    text=text,
                    lang=lang,
                    latency=time.monotonic() - job.audio_end_wall,
                    compute=compute,
                )
            )
