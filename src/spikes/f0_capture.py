"""Spike F0: captura simultánea de micrófono y audio del sistema.

Correr con música sonando **y** hablando al micrófono a la vez:

    python -m src.spikes.f0_capture --seconds 20

Imprime un veredicto PASA/FALLA contra los criterios de F0 y escribe mic.wav y sys.wav.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd

from src.audio.base import TARGET_SR, AudioSource, Track
from src.audio.blackhole import BLOCK_MS, BlackHoleSource, check_default_output, find_device

log = logging.getLogger("f0")

RMS_MIN = 0.001
DURATION_SKEW_MAX = 0.02
OVERFLOWS_PER_20S = 3
TS_SKEW_MAX = 0.5


@dataclass
class TrackResult:
    track: Track
    device_name: str
    pcm: np.ndarray
    first_ts: float | None
    last_ts: float | None
    elapsed: float
    overflows: int
    dropped_blocks: int
    path: Path

    @property
    def duration(self) -> float:
        return len(self.pcm) / TARGET_SR

    @property
    def rms(self) -> float:
        if self.pcm.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(self.pcm, dtype=np.float64))))

    @property
    def lost_samples(self) -> int:
        """Muestras que deberían haber llegado y no llegaron."""
        return max(0, int(round(self.elapsed * TARGET_SR)) - len(self.pcm))


def _drain(source: AudioSource, sink: list[np.ndarray], stamps: list[float]) -> None:
    for chunk in source.read():
        sink.append(chunk.pcm)
        stamps.append(chunk.ts_monotonic)


def _write_wav(path: Path, pcm: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(TARGET_SR)
        wav.writeframes(pcm16.tobytes())


def capture(seconds: float, mic_device: int | str, sys_device: int | str, out_dir: Path) -> list[TrackResult]:
    sources = [
        BlackHoleSource("mic", mic_device),
        BlackHoleSource("sys", sys_device),
    ]

    buffers: dict[Track, list[np.ndarray]] = {"mic": [], "sys": []}
    stamps: dict[Track, list[float]] = {"mic": [], "sys": []}

    for src in sources:
        print(f"  {src.track}: {src.device_name} @ {src.device_samplerate} Hz")

    readers = [
        threading.Thread(
            target=_drain, args=(src, buffers[src.track], stamps[src.track]), daemon=True
        )
        for src in sources
    ]

    print(f"\nGrabando {seconds:.0f} s. Poné música Y hablá al micrófono.")
    started = time.monotonic()
    for src in sources:
        src.start()
    for thread in readers:
        thread.start()

    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        print("\nInterrumpido.")

    for src in sources:
        src.stop()
    elapsed = time.monotonic() - started
    for thread in readers:
        thread.join(timeout=2.0)

    results = []
    for src in sources:
        assert isinstance(src, BlackHoleSource)
        blocks = buffers[src.track]
        pcm = np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)
        ts = stamps[src.track]
        results.append(
            TrackResult(
                track=src.track,
                device_name=src.device_name,
                pcm=pcm,
                first_ts=ts[0] if ts else None,
                last_ts=ts[-1] if ts else None,
                elapsed=elapsed,
                overflows=src.overflow_count,
                dropped_blocks=src.dropped_blocks,
                path=out_dir / f"{src.track}.wav",
            )
        )

    for res in results:
        _write_wav(res.path, res.pcm)

    return results


def report(results: list[TrackResult], seconds: float) -> bool:
    mic, sysx = results[0], results[1]

    print("\n" + "=" * 68)
    print("POR PISTA")
    print("=" * 68)
    for res in results:
        print(f"\n  [{res.track}] {res.device_name}")
        print(f"    duración          {res.duration:8.3f} s   (pared: {res.elapsed:.3f} s)")
        print(f"    RMS               {res.rms:8.5f}")
        print(f"    muestras          {len(res.pcm):8d}")
        print(f"    muestras perdidas {res.lost_samples:8d}   ({res.lost_samples / TARGET_SR:.3f} s)")
        print(f"    overflows         {res.overflows:8d}")
        print(f"    bloques a la cola {res.dropped_blocks:8d}   (descartados por cola llena)")
        print(f"    archivo           {res.path}")

    dur_skew = abs(mic.duration - sysx.duration) / max(mic.duration, sysx.duration, 1e-9)
    overflow_budget = OVERFLOWS_PER_20S * seconds / 20.0

    if mic.first_ts is not None and sysx.first_ts is not None:
        ts_skew: float | None = max(
            abs(mic.first_ts - sysx.first_ts), abs(mic.last_ts - sysx.last_ts)  # type: ignore[arg-type]
        )
    else:
        ts_skew = None

    checks: list[tuple[str, bool, str]] = [
        (
            "Ambos WAV existen",
            mic.path.exists() and sysx.path.exists(),
            f"mic={mic.path.exists()} sys={sysx.path.exists()}",
        ),
        (
            "Duraciones difieren < 2 %",
            dur_skew < DURATION_SKEW_MAX,
            f"{dur_skew * 100:.2f} %",
        ),
        (
            f"RMS > {RMS_MIN} en ambas pistas",
            mic.rms > RMS_MIN and sysx.rms > RMS_MIN,
            f"mic={mic.rms:.5f} sys={sysx.rms:.5f}",
        ),
        (
            f"Overflows < {overflow_budget:.0f}",
            (mic.overflows + sysx.overflows) < max(overflow_budget, 1.0),
            f"mic={mic.overflows} sys={sysx.overflows}",
        ),
        (
            "Timestamps comparables entre pistas",
            ts_skew is not None and ts_skew < TS_SKEW_MAX,
            "sin datos" if ts_skew is None else f"desfase máx {ts_skew * 1000:.1f} ms",
        ),
        (
            "Cero bloques descartados",
            (mic.dropped_blocks + sysx.dropped_blocks) == 0,
            f"mic={mic.dropped_blocks} sys={sysx.dropped_blocks}",
        ),
    ]

    print("\n" + "=" * 68)
    print("CRITERIOS F0")
    print("=" * 68)
    for name, ok, detail in checks:
        print(f"  [{'ok' if ok else 'NO'}] {name:38s} {detail}")

    passed = all(ok for _, ok, _ in checks)

    print("\n" + "=" * 68)
    print("  PASA" if passed else "  FALLA")
    print("=" * 68)

    if not passed:
        if sysx.rms <= RMS_MIN:
            print(
                "\n  La pista `sys` está muda. Casi siempre es una de estas:\n"
                "    - la salida por defecto no es el Multi-Output Device\n"
                "    - no había nada sonando\n"
                "    - el Multi-Output no tiene tildado BlackHole 2ch\n"
                "  Ver docs/SETUP_AUDIO.md."
            )
        if mic.rms <= RMS_MIN:
            print(
                "\n  La pista `mic` está muda. Revisá el permiso de micrófono de tu\n"
                "  terminal en Configuración del Sistema > Privacidad y seguridad."
            )

    return passed


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.spikes.f0_capture",
        description="Spike F0: captura simultánea de micrófono y audio del sistema.",
    )
    parser.add_argument("--seconds", type=float, default=20.0, help="duración (default: 20)")
    parser.add_argument(
        "--mic-device",
        default=None,
        help="subcadena o índice del micrófono (default: entrada por defecto del sistema)",
    )
    parser.add_argument(
        "--sys-device",
        default="BlackHole",
        help="subcadena o índice del audio del sistema (default: BlackHole)",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("data/f0"))
    parser.add_argument("--list-devices", action="store_true", help="listar entradas y salir")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.list_devices:
        print(sd.query_devices())
        return 0

    print("=" * 68)
    print("SPIKE F0 — captura dual")
    print("=" * 68)

    warning = check_default_output()
    if warning:
        print(f"\n  AVISO: {warning}\n")

    mic_device = args.mic_device
    if mic_device is None:
        default_in = sd.default.device[0]
        if default_in is None or default_in < 0:
            print("ERROR: no hay entrada por defecto. Pasá --mic-device.", file=sys.stderr)
            return 2
        mic_device = int(default_in)

    try:
        mic_device = int(mic_device) if str(mic_device).lstrip("-").isdigit() else mic_device
        sys_device = args.sys_device
        sys_device = int(sys_device) if str(sys_device).lstrip("-").isdigit() else sys_device
        # Resolver los dos antes de abrir nada: si BlackHole falta, conviene enterarse
        # ahora y no después de haber grabado 20 s de una sola pista.
        if isinstance(mic_device, str):
            mic_device = find_device(mic_device)
        if isinstance(sys_device, str):
            sys_device = find_device(sys_device)
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2

    if mic_device == sys_device:
        print(
            "\nERROR: mic y sys resuelven al mismo dispositivo. Serían la misma pista.",
            file=sys.stderr,
        )
        return 2

    try:
        results = capture(args.seconds, mic_device, sys_device, args.out_dir)
    except sd.PortAudioError as exc:
        print(f"\nERROR de PortAudio: {exc}", file=sys.stderr)
        print("  Si dice algo de permisos: Configuración del Sistema > Privacidad y", file=sys.stderr)
        print("  seguridad > Micrófono, y activá tu terminal.", file=sys.stderr)
        return 2

    print(f"\n(bloques de {BLOCK_MS} ms, remuestreados a {TARGET_SR} Hz)")
    return 0 if report(results, args.seconds) else 1


if __name__ == "__main__":
    raise SystemExit(main())
