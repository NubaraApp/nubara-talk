"""Captura nativa del audio del sistema — F4.

Reemplaza a `BlackHoleSource` sin driver de terceros ni Multi-Output Device: un helper
en Swift crea un Core Audio Process Tap (macOS 14.4+) y escribe el audio por stdout.
El helper se compila solo la primera vez. Ver ADR-002 y ADR-016.

**Estado: bloqueado por permisos.** El tap se crea, el agregado tiene su canal de
entrada y los callbacks llegan con el tamaño correcto — pero el audio viene en cero
mientras el sistema no le dé permiso de grabación de audio al binario. Ver
`docs/SETUP_AUDIO.md`.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator

import numpy as np
import soxr

from src.audio.base import TARGET_SR, AudioChunk, AudioSource, Track

log = logging.getLogger(__name__)

BLOCK_MS = 100
_QUEUE_MAX_BLOCKS = 600

_SRC = Path(__file__).with_name("tap_helper.swift")
_BUILD = Path(__file__).resolve().parents[2] / "build"
_APP = _BUILD / "nubara-tap.app"
_BIN = _APP / "Contents" / "MacOS" / "nubara-tap"

# El tap es una API con permiso: sin identidad de bundle, el sistema no tiene a quién
# pedirle autorización y devuelve silencio en vez de un error. Por eso el helper se
# empaqueta en un .app mínimo y se firma, aunque sea un programa de línea de comandos.
_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.nubara-talk.tap</string>
  <key>CFBundleName</key><string>nubara-talk tap</string>
  <key>CFBundleExecutable</key><string>nubara-tap</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSBackgroundOnly</key><true/>
  <key>NSAudioCaptureUsageDescription</key>
  <string>nubara-talk transcribe el audio de la reunion en tu propia maquina.</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>nubara-talk captura tu microfono para transcribirlo en tu propia maquina.</string>
</dict>
</plist>
"""


def build_helper(force: bool = False) -> Path:
    """Compila y firma el helper si hace falta. Devuelve la ruta del ejecutable."""
    if not force and _BIN.exists() and _BIN.stat().st_mtime >= _SRC.stat().st_mtime:
        return _BIN

    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    if not Path(swiftc).exists():
        raise RuntimeError(
            "No hay swiftc. Instalá las Command Line Tools:  xcode-select --install"
        )

    _BIN.parent.mkdir(parents=True, exist_ok=True)
    plist = _BUILD / "nubara-tap.plist"
    plist.write_text(_PLIST)
    (_APP / "Contents" / "Info.plist").write_text(_PLIST)

    subprocess.run(
        [
            swiftc, "-O", str(_SRC), "-o", str(_BIN),
            # El Info.plist va también dentro del Mach-O: TCC mira ahí cuando el
            # ejecutable se lanza directo, sin pasar por LaunchServices.
            "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
            "-Xlinker", "__info_plist", "-Xlinker", str(plist),
        ],
        check=True,
    )
    subprocess.run(
        ["codesign", "--force", "--sign", "-", "--identifier", "com.nubara-talk.tap", str(_APP)],
        check=True,
        capture_output=True,
    )
    log.info("helper compilado en %s", _BIN)
    return _BIN


class SwiftTapSource(AudioSource):
    """Una pista alimentada por el process tap del sistema.

    Misma interfaz que `BlackHoleSource`: quien la usa no sabe de dónde sale el audio.
    """

    def __init__(self, track: Track = "sys", exclude_pids: list[int] | None = None) -> None:
        self._track: Track = track
        self._exclude = list(exclude_pids or [])
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._resampler: soxr.ResampleStream | None = None
        self._queue: queue.Queue[tuple[float, np.ndarray]] = queue.Queue(_QUEUE_MAX_BLOCKS)
        self._lock = threading.Lock()
        self._running = False
        # Ver el comentario en BlackHoleSource: read() no puede terminar antes de
        # que la captura haya arrancado.
        self._ever_started = False
        self._device_sr = 0

        self.dropped_blocks = 0
        self.callback_count = 0
        self.overflow_count = 0
        # Suma de |x| de todo lo capturado: sirve para distinguir "no había sonido" de
        # "el sistema nos dio silencio porque falta el permiso".
        self.total_abs = 0.0

    @property
    def track(self) -> Track:
        return self._track

    @property
    def device_name(self) -> str:
        return "Core Audio process tap"

    @property
    def device_samplerate(self) -> int:
        return self._device_sr

    @property
    def is_running(self) -> bool:
        return self._running

    def open(self) -> None:
        """Arranca el helper y lee su cabecera, sin empezar a consumir audio."""
        with self._lock:
            if self._proc is not None:
                return

            binary = build_helper()
            cmd = [str(binary)]
            for pid in self._exclude:
                cmd += ["--exclude-pid", str(pid)]

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert proc.stdout is not None

            header = proc.stdout.readline()
            if not header:
                err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")
                proc.wait(timeout=5)
                raise RuntimeError(f"el helper del tap murió al arrancar:\n{err.strip()}")

            info = json.loads(header)
            self._device_sr = int(info["sample_rate"])
            self._resampler = soxr.ResampleStream(
                self._device_sr, TARGET_SR, 1, dtype="float32", quality="HQ"
            )
            self._proc = proc

        log.info("pista %s abierta: process tap @ %d Hz", self._track, self._device_sr)

    def start(self) -> None:
        self.open()
        with self._lock:
            if self._running:
                return
            self._running = True
            self._ever_started = True
            self._thread = threading.Thread(
                target=self._pump, name=f"tap-{self._track}", daemon=True
            )
            self._thread.start()

    def _pump(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        nbytes = int(self._device_sr * BLOCK_MS / 1000) * 4

        while self._running:
            block = proc.stdout.read(nbytes)
            if not block:
                break
            self.callback_count += 1
            mono = np.frombuffer(block, dtype="<f4")
            self.total_abs += float(np.abs(mono).sum())

            resampler = self._resampler
            if resampler is None:
                break
            out = resampler.resample_chunk(mono)
            if out.size == 0:
                continue
            try:
                self._queue.put_nowait((time.monotonic(), out))
            except queue.Full:
                self.dropped_blocks += 1

    def stop(self) -> None:
        with self._lock:
            if self._proc is None:
                return
            self._running = False
            proc, self._proc = self._proc, None

        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

        resampler, self._resampler = self._resampler, None
        if resampler is not None:
            tail = resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
            if tail.size:
                try:
                    self._queue.put_nowait((time.monotonic(), tail))
                except queue.Full:
                    self.dropped_blocks += 1

        err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace").strip()
        if err:
            log.warning("helper del tap: %s", err)
        if self.callback_count and self.total_abs == 0.0:
            log.warning(
                "pista %s: el tap entregó %d bloques, todos en silencio absoluto. "
                "Suele ser el permiso de grabación de audio del sistema, no un bug. "
                "Ver docs/SETUP_AUDIO.md.",
                self._track,
                self.callback_count,
            )

    def read(self) -> Iterator[AudioChunk]:
        while True:
            try:
                ts, pcm = self._queue.get(timeout=0.1)
            except queue.Empty:
                if not self._running and self._ever_started:
                    return
                continue
            yield AudioChunk(track=self._track, ts_monotonic=ts, pcm=pcm)
