"""Spike F2: seleccionar un tramo de la transcripción y pedirle algo al LLM.

    python -m src.spikes.f2_respond --list-prompts
    python -m src.spikes.f2_respond --prompt responder --turns 3

Toma segmentos de la base, arma el prompt con una plantilla de config/prompts.yaml y se
lo manda a la CLI por subprocess.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from src.llm.bridge import LlmCli, LlmError, NotAuthenticated, available, pick
from src.llm.prompts import CliSpec, Config, load
from src.store.db import DEFAULT_DB_PATH, connect

log = logging.getLogger("f2")

# Las dos pistas son los dos lados de la conversación. Etiquetarlas así le da al LLM lo
# que la diarización le daría, sin diarización. Ver ADR-001.
SPEAKER = {"mic": "Yo", "sys": "Ellos"}

# Patrones de secreto que no deben aparecer en el repo. No es un escáner serio: es una
# red de seguridad contra el error obvio de pegar una clave en un ejemplo.
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token)\s*[:=]\s*['\"][^'\"]{12,}['\"]"),
)


def latest_session(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def pick_segments(
    conn: sqlite3.Connection, session_id: str, turns: int, track: str | None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM segments WHERE session_id = ? AND status = 'confirmed'"
    params: list[object] = [session_id]
    if track:
        sql += " AND track = ?"
        params.append(track)
    sql += " ORDER BY t_start DESC LIMIT ?"
    params.append(turns)
    return list(reversed(list(conn.execute(sql, params))))


def context_before(
    conn: sqlite3.Connection, session_id: str, before: float, count: int
) -> list[sqlite3.Row]:
    if count <= 0:
        return []
    rows = conn.execute(
        """SELECT * FROM segments
           WHERE session_id = ? AND status = 'confirmed' AND t_start < ?
           ORDER BY t_start DESC LIMIT ?""",
        (session_id, before, count),
    )
    return list(reversed(list(rows)))


def as_text(rows: list[sqlite3.Row]) -> str:
    return "\n".join(
        f"[{SPEAKER.get(r['track'], r['track'])}] {r['t_start']:.1f}s  {r['text']}" for r in rows
    )


def scan_secrets(root: Path) -> list[str]:
    hits = []
    skip = {".git", ".venv", "data", "__pycache__", "models"}
    for path in root.rglob("*"):
        if not path.is_file() or set(path.relative_to(root).parts) & skip:
            continue
        if path.suffix in {".wav", ".sqlite", ".aiff", ".pkg"}:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append(f"{path.relative_to(root)}: {pattern.pattern}")
    return hits


def check_error_paths(config: Config) -> tuple[bool, str]:
    """Verifica que el puente falle bien: CLI inexistente y timeout.

    Es un criterio de F2 ("subprocess con timeout y manejo de error"), y no se puede
    comprobar en la corrida feliz. Se comprueba acá, contra fallos provocados.
    """
    missing = LlmCli(CliSpec("fantasma", ("no-existe-esta-cli",), True), timeout_s=5)
    try:
        missing.run("hola")
        return False, "una CLI inexistente no levantó error"
    except LlmError:
        pass

    slow = LlmCli(CliSpec("lento", ("/bin/sleep", "30"), True), timeout_s=1.0)
    try:
        slow.run("hola")
        return False, "el timeout no cortó un proceso colgado"
    except LlmError as exc:
        if "no respondió" not in str(exc):
            return False, f"el timeout dio un error confuso: {exc}"

    return True, "CLI inexistente y timeout dan errores accionables"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.spikes.f2_respond",
        description="Spike F2: pedirle algo al LLM sobre un tramo de la transcripción.",
    )
    parser.add_argument("--prompt", default="responder", help="id de plantilla")
    parser.add_argument("--turns", type=int, default=3, help="últimos N segmentos a usar")
    parser.add_argument("--context", type=int, default=4, help="segmentos previos de contexto")
    parser.add_argument("--track", choices=["mic", "sys"], default=None)
    parser.add_argument("--idioma", default="español")
    parser.add_argument("--session", default=None, help="id de sesión (default: la última)")
    parser.add_argument("--cli", default=None, help="forzar una CLI por id")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--config", type=Path, default=Path("config/prompts.yaml"))
    parser.add_argument("--list-prompts", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="mostrar el prompt y salir")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        config = load(args.config)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.list_prompts:
        print(f"Plantillas en {args.config}:\n")
        for tpl in config.prompts.values():
            print(f"  {tpl.id:16s} {tpl.nombre} — {tpl.descripcion}")
        print("\nCLIs configuradas:")
        for spec in config.candidates:
            mark = "instalada" if available(spec) else "no está"
            where = "local" if spec.local else "remota"
            print(f"  {spec.id:16s} {' '.join(spec.command)}  [{where}, {mark}]")
        return 0

    if args.prompt not in config.prompts:
        known = ", ".join(config.prompts)
        print(f"ERROR: no existe el prompt {args.prompt!r}. Hay: {known}", file=sys.stderr)
        return 2
    template = config.prompts[args.prompt]

    conn = connect(args.db)
    session_id = args.session or latest_session(conn)
    if session_id is None:
        print(
            f"ERROR: no hay ninguna sesión en {args.db}.\n"
            "  Grabá una primero: python -m src.spikes.f1_transcribe --seconds 60",
            file=sys.stderr,
        )
        return 2

    rows = pick_segments(conn, session_id, args.turns, args.track)
    if not rows:
        print(f"ERROR: la sesión {session_id} no tiene segmentos confirmados.", file=sys.stderr)
        return 2

    ctx = context_before(conn, session_id, rows[0]["t_start"], args.context)
    prompt = template.render(
        fragmento=as_text(rows), contexto=as_text(ctx) or "(sin contexto previo)",
        idioma=args.idioma,
    )

    print("=" * 72)
    print(f"SPIKE F2 — {template.nombre}")
    print("=" * 72)
    print(f"\n  sesión {session_id}, {len(rows)} segmentos, {len(ctx)} de contexto\n")
    print("-" * 72)
    print(prompt.strip())
    print("-" * 72)

    if args.dry_run:
        return 0

    try:
        spec = pick(config.candidates, args.cli)
    except LlmError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2

    where = "local, no sale de la máquina" if spec.local else "remota, sale al proveedor"
    print(f"\n  CLI: {spec.id} ({' '.join(spec.command)}) — {where}")
    print("  esperando respuesta...\n", flush=True)

    failed_reason = None
    try:
        response = LlmCli(spec, config.timeout_s).run(prompt)
        print("=" * 72)
        print(response.text)
        print("=" * 72)
        print(f"\n  {response.elapsed:.1f} s")
    except NotAuthenticated as exc:
        failed_reason = str(exc)
        print(f"\nERROR: {exc}", file=sys.stderr)
    except LlmError as exc:
        failed_reason = str(exc)
        print(f"\nERROR: {exc}", file=sys.stderr)

    errors_ok, errors_detail = check_error_paths(config)
    secrets = scan_secrets(Path.cwd())

    checks = [
        ("Plantillas cargadas desde YAML", True, f"{len(config.prompts)} en {args.config}"),
        ("Marcadores validados al cargar", True, "un typo falla al abrir, no en la reunión"),
        ("Timeout y errores accionables", errors_ok, errors_detail),
        ("Cero secretos en el repo", not secrets, secrets[0] if secrets else "0 coincidencias"),
        (
            "La CLI respondió",
            failed_reason is None,
            "ok" if failed_reason is None else failed_reason.splitlines()[0],
        ),
    ]

    print("\n" + "=" * 72)
    print("CRITERIOS F2")
    print("=" * 72)
    for name, ok, detail in checks:
        print(f"  [{'ok' if ok else 'NO'}] {name:34s} {detail}")

    passed = all(ok for _, ok, _ in checks)
    print("\n" + "=" * 72)
    print("  PASA" if passed else "  FALLA")
    print("=" * 72)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
