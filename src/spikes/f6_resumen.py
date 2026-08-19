"""Spike F6: resumen de la reunión entera.

Toma una sesión ya transcrita, arma la conversación completa, la manda a la CLI del LLM
y devuelve un resumen detallado. Mide lo que decide si la función sirve: cuánto texto se
va en ecos entre pistas, en cuántos tramos hubo que partir y cuánto tardó.

No usa la ventana ni el WebSocket: corre el mismo código que el sidecar.

    python -m src.spikes.f6_resumen
    python -m src.spikes.f6_resumen --sesion 4031177656b3
    python -m src.spikes.f6_resumen --solo-material   # sin llamar al LLM
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

from src.store.db import DEFAULT_DB_PATH, connect
from src.ui.server import Hub

SEPARADOR = "=" * 72


def latest_session(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT s.id FROM sessions s JOIN segments g ON g.session_id = s.id
        GROUP BY s.id ORDER BY COUNT(g.id) DESC, s.started_at DESC LIMIT 1
        """
    ).fetchone()
    return row["id"] if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--sesion", default=None, help="id de sesión; por defecto, la más larga")
    parser.add_argument("--idioma", default="español")
    parser.add_argument("--instrucciones", default="")
    parser.add_argument("--solo-material", action="store_true", help="no llama al LLM")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    print(SEPARADOR)
    print("SPIKE F6 — resumen de la reunión")
    print(SEPARADOR)

    conn = connect(args.db)
    session_id = args.sesion or latest_session(conn)
    if session_id is None:
        print("\n  No hay ninguna sesión con segmentos en la base.")
        return 1

    crudo = conn.execute(
        "SELECT COUNT(*) n FROM segments WHERE session_id = ? AND status = 'confirmed'",
        (session_id,),
    ).fetchone()["n"]
    conn.close()

    hub = Hub(args.db, args.config)
    lines, keys = hub.session_lines(session_id)
    material = "\n".join(lines)

    print(f"\n  sesión              {session_id}")
    print(f"  turnos confirmados  {crudo}")
    print(f"  turnos tras el eco  {len(keys)}   (se cayeron {crudo - len(keys)})")
    print(f"  caracteres          {len(material)}")

    if not lines:
        print("\n  Esa sesión no tiene nada transcrito.")
        return 1

    print("\n  primeros renglones:")
    for line in lines[:5]:
        print(f"    {line[:90]}")

    if args.solo_material:
        return 0

    print("\n  llamando al LLM…")
    t0 = time.monotonic()
    answer = hub.summarize(
        {
            "session_id": session_id,
            "propio": args.idioma,
            "instrucciones": args.instrucciones,
        }
    )
    elapsed = time.monotonic() - t0

    print(f"\n{SEPARADOR}\nRESUMEN\n{SEPARADOR}\n")
    print(answer["text"])

    secciones = ["Resumen", "Temas", "Decisiones", "Tareas", "Preguntas abiertas"]
    presentes = [s for s in secciones if s.lower() in answer["text"].lower()]

    print(f"\n{SEPARADOR}\nCRITERIOS F6\n{SEPARADOR}")
    ok = True
    texto = answer["text"].strip()
    criterios = [
        ("El resumen no viene vacío", len(texto) > 200, f"{len(texto)} caracteres"),
        (
            "Trae las secciones pedidas",
            len(presentes) >= 4,
            f"{len(presentes)}/{len(secciones)}: {presentes}",
        ),
        ("Quedó guardado en answers", bool(answer["id"]), f"id={answer['id']}"),
        ("Tardó menos de 300 s", elapsed < 300, f"{elapsed:.1f} s"),
    ]
    for etiqueta, cumple, detalle in criterios:
        ok = ok and cumple
        print(f"  [{'ok' if cumple else 'NO'}] {etiqueta:34} {detalle}")
    print(f"  tramos: {answer['tramos']}   CLI: {answer['cli']}   local: {answer['local']}")

    print(f"\n{SEPARADOR}\n  {'PASA' if ok else 'FALLA'}\n{SEPARADOR}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
