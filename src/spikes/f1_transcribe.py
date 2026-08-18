"""Spike F1: transcripción en vivo de las dos pistas.

    python -m src.spikes.f1_transcribe --seconds 30

Imprime cada segmento a medida que sale y, al final, un veredicto PASA/FALLA contra los
criterios de F1 en docs/PLAN.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import sounddevice as sd

from src.audio.base import Track
from src.audio.blackhole import BlackHoleSource, check_default_output, find_device
from src.stt.engine import MlxBackend, Result, TranscriptionEngine, default_backend
from src.stt.pipeline import SessionWriter, TrackPipeline
from src.stt.vad import TurnDetector
from src.store.db import DEFAULT_DB_PATH, connect, create_session, end_session, transcript

log = logging.getLogger("f1")

LATENCY_BUDGET = 2.0

_print_lock = threading.Lock()


def _show(result: Result) -> None:
    turn = result.turn
    mark = "*" if turn.kind == "final" else " "
    with _print_lock:
        print(
            f"  {mark} [{turn.track}] {turn.t_start:6.2f}-{turn.t_end:6.2f}"
            f"  ({result.latency:.2f}s)  {result.text}",
            flush=True,
        )


def report(
    results: list[Result],
    pipelines: dict[Track, TrackPipeline],
    engine: TranscriptionEngine,
    rows: list,
    warmup: float,
) -> bool:
    finals = [r for r in results if r.turn.kind == "final"]
    lat = np.array([r.latency for r in finals]) if finals else np.zeros(0)
    comp = np.array([r.compute for r in finals]) if finals else np.zeros(0)
    audio = sum(r.turn.duration for r in finals)

    print("\n" + "=" * 72)
    print("POR PISTA")
    print("=" * 72)
    for track, pipe in pipelines.items():
        track_finals = [r for r in finals if r.turn.track == track]
        track_lat = [r.latency for r in track_finals]
        print(f"\n  [{track}]")
        print(f"    turnos finales    {pipe.turns_final:6d}   transcritos {len(track_finals)}")
        print(f"    parciales         {pipe.turns_partial:6d}")
        if track_lat:
            print(f"    latencia mediana  {np.median(track_lat):6.2f} s")
        else:
            print("    latencia mediana       —   (sin habla detectada)")

    print("\n" + "=" * 72)
    print("MOTOR")
    print("=" * 72)
    print(f"  backend                {engine.backend_name}")
    print(f"  carga del modelo       {warmup:6.2f} s")
    print(f"  trabajos completados   {engine.done:6d}")
    print(f"  parciales descartados  {engine.dropped_partials:6d}   (llegó uno más nuevo)")
    if len(comp):
        print(f"  inferencia mediana     {np.median(comp):6.3f} s")
        print(f"  RTF                    {comp.sum() / audio:6.3f}   ({audio:.1f} s de habla)")
    print(f"  filas escritas         {len(rows):6d}")

    tracks_with_text = {r.turn.track for r in finals}
    ordered = all(a["t_start"] <= b["t_start"] for a, b in zip(rows, rows[1:]))
    confirmed = [r for r in rows if r["status"] == "confirmed"]

    checks: list[tuple[str, bool, str]] = [
        (
            "Hubo turnos finales transcritos",
            len(finals) > 0,
            f"{len(finals)} turnos",
        ),
        (
            f"Latencia mediana < {LATENCY_BUDGET} s",
            len(lat) > 0 and float(np.median(lat)) < LATENCY_BUDGET,
            f"mediana {np.median(lat):.2f} s, p95 {np.percentile(lat, 95):.2f} s"
            if len(lat)
            else "sin datos",
        ),
        (
            "Ambas pistas produjeron texto",
            tracks_with_text == {"mic", "sys"},
            f"con texto: {sorted(tracks_with_text) or 'ninguna'}",
        ),
        (
            "Segmentos ordenados por t_start",
            ordered,
            f"{len(rows)} filas",
        ),
        (
            "Todo turno final quedó confirmado",
            len(confirmed) == len({(r.turn.track, r.turn.index) for r in finals}),
            f"{len(confirmed)} confirmados",
        ),
    ]

    print("\n" + "=" * 72)
    print("CRITERIOS F1")
    print("=" * 72)
    for name, ok, detail in checks:
        print(f"  [{'ok' if ok else 'NO'}] {name:36s} {detail}")

    passed = all(ok for _, ok, _ in checks)
    print("\n" + "=" * 72)
    print("  PASA" if passed else "  FALLA")
    print("=" * 72)
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.spikes.f1_transcribe",
        description="Spike F1: transcripción en vivo de micrófono y audio del sistema.",
    )
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--mic-device", default=None)
    parser.add_argument("--sys-device", default="BlackHole")
    parser.add_argument("--model", default=None, help="repo HF del modelo MLX")
    parser.add_argument("--lang-mic", default="en", help="idioma del mic, o 'auto'")
    parser.add_argument("--lang-sys", default="en", help="idioma del sistema, o 'auto'")
    parser.add_argument("--max-turn", type=float, default=15.0)
    parser.add_argument("--partial-every", type=float, default=2.5)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    print("=" * 72)
    print("SPIKE F1 — transcripción en vivo")
    print("=" * 72)

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

    def resolve(value: int | str) -> int:
        text = str(value)
        return int(text) if text.lstrip("-").isdigit() else find_device(text)

    try:
        mic_id = resolve(mic_device)
        sys_id = resolve(args.sys_device)
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2

    lang = {
        "mic": None if args.lang_mic == "auto" else args.lang_mic,
        "sys": None if args.lang_sys == "auto" else args.lang_sys,
    }
    results: list[Result] = []
    results_lock = threading.Lock()

    session_id = uuid.uuid4().hex[:12]
    conn = connect(args.db)
    writer = SessionWriter(conn, session_id)

    def on_result(result: Result) -> None:
        with results_lock:
            results.append(result)
        _show(result)
        writer(result)

    engine = TranscriptionEngine(
        on_result,
        backend=MlxBackend(args.model) if args.model else default_backend(),
        language_by_track=lang,
    )
    print(f"\n  backend: {engine.backend_name}")
    print("  cargando el modelo (la primera vez lo descarga)...", flush=True)
    warmup = engine.warmup()
    print(f"  listo en {warmup:.1f} s")

    sources = [BlackHoleSource("mic", mic_id), BlackHoleSource("sys", sys_id)]
    for src in sources:
        src.open()

    session_start = time.monotonic()
    create_session(conn, session_id, session_start)

    engine.start()
    pipelines = {
        src.track: TrackPipeline(
            src,
            TurnDetector(
                src.track,
                session_start,
                max_turn_s=args.max_turn,
                partial_every_s=args.partial_every,
            ),
            engine,
            session_start,
        )
        for src in sources
    }

    print(f"\nEscuchando {args.seconds:.0f} s. '*' marca turno cerrado.\n")
    for src in sources:
        src.start()
    for pipe in pipelines.values():
        pipe.start()

    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        print("\nInterrumpido.")

    stoppers = [threading.Thread(target=src.stop) for src in sources]
    for t in stoppers:
        t.start()
    for t in stoppers:
        t.join(timeout=5.0)
    for pipe in pipelines.values():
        pipe.join(timeout=5.0)

    print("\n  vaciando la cola de transcripción...", flush=True)
    engine.stop(drain=True)
    end_session(conn, session_id, time.monotonic())

    rows = transcript(conn, session_id)
    ok = report(results, pipelines, engine, rows, warmup)
    print(f"\n  sesión {session_id} en {args.db}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
