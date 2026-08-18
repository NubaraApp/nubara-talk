"""Spike F3: traducción local de una sesión ya transcrita.

Toma los segmentos de la última sesión en la base, los traduce con NLLB-200 sobre
CTranslate2, guarda `text_trad` y verifica que FTS5 los encuentre por el texto
traducido. Mide la latencia por segmento, que es el criterio de salida de F3.

Corre sin audio y sin red: el modelo ya está convertido en disco.

    python -m src.spikes.f3_translate
    python -m src.spikes.f3_translate --texto "Can you hear me?"
"""

from __future__ import annotations

import argparse
import os

# Antes de importar transformers: si algo intentara bajar el modelo, tiene que fallar,
# no salir a la red por lo bajo. El spike prueba que la traducción es local de verdad.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import logging  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from src.store.db import DEFAULT_DB_PATH, connect, search, update_translation  # noqa: E402
from src.translate.nllb import DEFAULT_MODEL_DIR, Translator  # noqa: E402

LATENCY_BUDGET = 0.300  # segundos por segmento

SPEAKER = {"mic": "Yo", "sys": "Ellos"}


def latest_session(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def segments(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM segments
            WHERE session_id = ? AND status = 'confirmed' AND TRIM(text) != ''
            ORDER BY t_start, track
            """,
            (session_id,),
        )
    )


def report(
    rows: list[sqlite3.Row],
    lat: list[float],
    written: int,
    hits: int,
    probe: str,
    load: float,
    warmup: float,
    batch_gain: float | None,
) -> int:
    lat_a = np.array(lat)
    print("\n" + "=" * 72)
    print("TRADUCCIÓN")
    print("=" * 72)
    print(f"  modelo                 {DEFAULT_MODEL_DIR}")
    print(f"  carga                  {load:6.2f} s")
    print(f"  primera inferencia     {warmup:6.3f} s   (la más lenta, siempre)")
    print(f"  segmentos              {len(rows):6d}")
    if len(lat_a):
        print(f"  latencia mediana       {np.median(lat_a):6.3f} s")
        print(f"  latencia p95           {np.percentile(lat_a, 95):6.3f} s")
        print(f"  latencia máxima        {lat_a.max():6.3f} s")
    if batch_gain is not None:
        print(f"  lote vs suelto         {batch_gain:6.2f}x   (mismo texto, una llamada)")

    checks: list[tuple[str, bool, str]] = [
        (
            "Hubo segmentos para traducir",
            len(rows) > 0,
            f"{len(rows)} segmentos confirmados",
        ),
        (
            f"Latencia mediana < {LATENCY_BUDGET * 1000:.0f} ms",
            len(lat_a) > 0 and float(np.median(lat_a)) < LATENCY_BUDGET,
            f"mediana {np.median(lat_a) * 1000:.0f} ms, "
            f"p95 {np.percentile(lat_a, 95) * 1000:.0f} ms"
            if len(lat_a)
            else "sin datos",
        ),
        (
            "`text_trad` poblado en SQLite",
            written == len(rows) and written > 0,
            f"{written} de {len(rows)} filas",
        ),
        (
            "FTS5 encuentra por el texto traducido",
            hits > 0,
            f"'{probe}' devuelve {hits} filas",
        ),
        (
            "Sin red: el modelo carga de disco",
            os.environ.get("HF_HUB_OFFLINE") == "1",
            "HF_HUB_OFFLINE=1 durante toda la corrida",
        ),
    ]

    print("\n" + "=" * 72)
    print("CRITERIOS DE SALIDA F3")
    print("=" * 72)
    ok = True
    for name, passed, detail in checks:
        ok &= passed
        print(f"  [{'x' if passed else ' '}] {name:<44} {detail}")
    print("\n  " + ("PASA" if ok else "FALLA"))
    return 0 if ok else 1


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Spike F3: traducción local")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--session", default=None, help="por defecto, la última")
    parser.add_argument("--desde", default="en", help="idioma de origen si el segmento no lo trae")
    parser.add_argument("--hacia", default="es", help="idioma destino")
    parser.add_argument("--probe", default=None, help="palabra a buscar en FTS para verificar")
    parser.add_argument("--texto", default=None, help="traduce esta frase y sale")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    args = parser.parse_args()

    try:
        translator = Translator(args.model_dir)
    except FileNotFoundError as exc:
        print(f"\nERROR: {exc}\n", file=sys.stderr)
        return 2

    if args.texto:
        t0 = time.monotonic()
        out = translator.translate(args.texto, args.desde, args.hacia)
        print(f"\n  {out}\n  ({(time.monotonic() - t0) * 1000:.0f} ms)")
        return 0

    warmup = translator.warmup(args.desde, args.hacia)

    conn = connect(args.db)
    session_id = args.session or latest_session(conn)
    if session_id is None:
        print(
            "\nERROR: no hay ninguna sesión en la base. Corré primero:"
            "\n  python -m src.spikes.f1_transcribe --seconds 30\n",
            file=sys.stderr,
        )
        return 2

    rows = segments(conn, session_id)
    if not rows:
        print(
            f"\nERROR: la sesión {session_id} no tiene segmentos confirmados.\n",
            file=sys.stderr,
        )
        return 2

    print(f"\n  sesión {session_id}: {len(rows)} segmentos\n")
    lat: list[float] = []
    written = 0
    for row in rows:
        src = row["lang"] or args.desde
        t0 = time.monotonic()
        trad = translator.translate(row["text"], src, args.hacia)
        lat.append(time.monotonic() - t0)
        update_translation(conn, session_id, row["track"], row["turn_index"], trad)
        written += 1
        who = SPEAKER.get(row["track"], row["track"])
        print(f"  [{row['t_start']:6.1f}s {who:5}] {row['text']}")
        print(f"  {'':16} {trad}   ({lat[-1] * 1000:.0f} ms)")

    # Un lote paga una sola vez el costo fijo del modelo. Sirve para el subtitulado
    # diferido, no para el vivo, donde los turnos llegan de a uno.
    texts = [r["text"] for r in rows]
    t0 = time.monotonic()
    translator.translate_many(texts, args.desde, args.hacia)
    batch = time.monotonic() - t0
    batch_gain = sum(lat) / batch if batch > 0 else None

    # Verificación de FTS: una palabra sacada de una traducción real tiene que encontrar
    # la fila. Si el trigger de UPDATE no reindexara, esto daría cero.
    probe = args.probe
    if probe is None:
        last = conn.execute(
            "SELECT text_trad FROM segments WHERE session_id = ? AND text_trad IS NOT NULL"
            " ORDER BY LENGTH(text_trad) DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        trad = last["text_trad"] or ""
        words = [w.strip(".,¿?¡!:;\"'") for w in trad.split()]
        probe = max((w for w in words if len(w) > 4), key=len, default="")
    hits = len(search(conn, probe, session_id=session_id)) if probe else 0

    return report(
        rows, lat, written, hits, probe, translator.load_time, warmup, batch_gain
    )


if __name__ == "__main__":
    raise SystemExit(main())
