"""Sidecar de la UI — F5.

Un servidor WebSocket en 127.0.0.1 que expone lo que ya existe: captura, VAD, STT,
traducción, búsqueda FTS5 y el puente al LLM. La ventana es sólo la cara; toda la
lógica sigue viviendo en los módulos de F0 a F4.

Escucha únicamente en loopback y no habla con nadie más. Ver ADR-018.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

from src.audio.base import Track
from src.audio.factory import make_source
from src.llm import bridge
from src.llm.prompts import Config as PromptConfig
from src.llm.prompts import load as load_prompts
from src.store.db import (
    DEFAULT_DB_PATH,
    connect,
    create_session,
    end_session,
    search,
    transcript,
    update_translation,
)
from src.stt.engine import Result, TranscriptionEngine, default_backend
from src.stt.pipeline import SessionWriter, TrackPipeline
from src.stt.vad import TurnDetector

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8765
WEB_ROOT = Path(__file__).with_name("web")

SPEAKER = {"mic": "Yo", "sys": "Ellos"}


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


class Hub:
    """El estado de la app. Una sola sesión de captura por vez."""

    def __init__(self, db: Path, config: Path | None = None) -> None:
        self._db_path = db
        self._conn = connect(db)
        self._prompts: PromptConfig = load_prompts(config) if config else load_prompts()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[ServerConnection] = set()
        self._outbox: asyncio.Queue[str] = asyncio.Queue()

        self._lock = threading.Lock()
        self._engine: TranscriptionEngine | None = None
        self._sources: list[Any] = []
        self._pipelines: list[TrackPipeline] = []
        self._trad = None
        self._trad_conn: sqlite3.Connection | None = None
        self._session_id: str | None = None
        self._writer: SessionWriter | None = None
        self._running = False

        # El modelo tarda en cargar. Se carga una vez y se reusa entre sesiones.
        self._translator = None

    # ------------------------------------------------------------------ salida

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, kind: str, **payload: Any) -> None:
        """Encola un mensaje para todos los clientes. Seguro desde cualquier hilo."""
        message = json.dumps({"type": kind, **payload})
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._outbox.put_nowait, message)

    async def pump(self) -> None:
        while True:
            message = await self._outbox.get()
            for client in list(self._clients):
                try:
                    await client.send(message)
                except websockets.exceptions.ConnectionClosed:
                    self._clients.discard(client)

    # ------------------------------------------------------------------ captura

    def _on_result(self, result: Result) -> None:
        turn = result.turn
        self.emit(
            "segment",
            track=turn.track,
            turn_index=turn.index,
            t_start=turn.t_start,
            t_end=turn.t_end,
            text=result.text,
            lang=result.lang,
            final=turn.kind == "final",
            latency=result.latency,
        )
        if self._writer is not None:
            self._writer(result)
        if self._trad is not None:
            self._trad.submit(result)

    def _on_translation(self, item: Any) -> None:
        turn = item.result.turn
        if self._trad_conn is not None and self._session_id is not None:
            update_translation(self._trad_conn, self._session_id, turn.track, turn.index, item.text)
        self.emit(
            "translation",
            track=turn.track,
            turn_index=turn.index,
            text=item.text,
            latency=item.latency,
        )

    def start_capture(self, options: dict[str, Any]) -> dict[str, Any]:
        """Arranca la captura. Bloquea mientras carga los modelos: va en un hilo."""
        with self._lock:
            if self._running:
                raise RuntimeError("Ya hay una sesión corriendo.")

            mic = options.get("mic_device") or None
            sys_device = options.get("sys_device") or "BlackHole"
            langs: dict[Track, str | None] = {
                "mic": options.get("lang_mic") or None,
                "sys": options.get("lang_sys") or None,
            }
            target = options.get("traducir") or None

            self.emit("status", message="cargando el modelo de transcripción...")
            engine = TranscriptionEngine(
                self._on_result, backend=default_backend(), language_by_track=langs
            )
            engine.warmup()

            if target:
                from src.translate.nllb import Translator
                from src.translate.worker import TranslationWorker

                self.emit("status", message="cargando el traductor...")
                if self._translator is None:
                    self._translator = Translator()
                self._trad_conn = connect(self._db_path)
                self._trad = TranslationWorker(self._translator, target, self._on_translation)
                self._trad.start()

            import sounddevice as sd

            if mic is None:
                default_in = sd.default.device[0]
                if default_in is None or default_in < 0:
                    raise RuntimeError("No hay entrada por defecto. Elegí un micrófono.")
                mic = int(default_in)

            sources = [make_source("mic", mic), make_source("sys", sys_device)]
            for source in sources:
                source.open()

            session_id = uuid.uuid4().hex[:12]
            session_start = time.monotonic()
            create_session(self._conn, session_id, session_start)
            self._writer = SessionWriter(self._conn, session_id)
            self._session_id = session_id

            engine.start()
            pipelines = [
                TrackPipeline(
                    source, TurnDetector(source.track, session_start), engine, session_start
                )
                for source in sources
            ]
            for source in sources:
                source.start()
            for pipe in pipelines:
                pipe.start()

            self._engine = engine
            self._sources = sources
            self._pipelines = pipelines
            self._running = True

        self.emit("state", running=True, session_id=session_id, backend=engine.backend_name)
        return {"session_id": session_id}

    def stop_capture(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            sources, self._sources = self._sources, []
            pipelines, self._pipelines = self._pipelines, []
            engine, self._engine = self._engine, None
            trad, self._trad = self._trad, None
            trad_conn, self._trad_conn = self._trad_conn, None
            session_id = self._session_id

        stoppers = [threading.Thread(target=source.stop) for source in sources]
        for thread in stoppers:
            thread.start()
        for thread in stoppers:
            thread.join(timeout=5.0)
        for pipe in pipelines:
            pipe.join(timeout=5.0)

        if engine is not None:
            engine.stop(drain=True)
        if trad is not None:
            trad.stop()
        if trad_conn is not None:
            trad_conn.close()
        if session_id is not None:
            end_session(self._conn, session_id, time.monotonic())

        self.emit("state", running=False, session_id=session_id)

    # ------------------------------------------------------------------ consultas

    def sessions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT s.id, s.started_at, s.ended_at, COUNT(g.id) AS segmentos
            FROM sessions s LEFT JOIN segments g ON g.session_id = s.id
            GROUP BY s.id ORDER BY s.started_at DESC LIMIT 50
            """
        )
        return [_row(r) for r in rows]

    def transcript(self, session_id: str) -> list[dict[str, Any]]:
        return [_row(r) for r in transcript(self._conn, session_id)]

    def search(self, query: str, session_id: str | None) -> list[dict[str, Any]]:
        try:
            return [_row(r) for r in search(self._conn, query, session_id=session_id)]
        except sqlite3.OperationalError as exc:
            # FTS5 tiene su propia sintaxis: un `"` suelto es un error, no cero
            # resultados. Se lo devolvemos al usuario en vez de tragarlo.
            raise RuntimeError(f"Consulta inválida para FTS5: {exc}") from exc

    def prompt_catalog(self) -> dict[str, Any]:
        return {
            "templates": [
                {"id": t.id, "nombre": t.nombre, "descripcion": t.descripcion}
                for t in self._prompts.prompts.values()
            ],
            "clis": [
                {
                    "id": c.id,
                    "command": c.command[0],
                    "local": c.local,
                    "installed": bridge.available(c),
                }
                for c in self._prompts.candidates
            ],
        }

    def ask(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Arma el prompt con los segmentos elegidos y lo manda a la CLI."""
        template = self._prompts.prompts.get(payload.get("template", ""))
        if template is None:
            raise RuntimeError(f"No existe la plantilla {payload.get('template')!r}.")

        session_id = payload.get("session_id") or self._session_id
        keys = payload.get("keys") or []
        if not keys:
            raise RuntimeError("No hay ningún fragmento seleccionado.")

        rows = []
        for key in keys:
            row = self._conn.execute(
                "SELECT * FROM segments WHERE session_id = ? AND track = ? AND turn_index = ?",
                (session_id, key["track"], key["turn_index"]),
            ).fetchone()
            if row is not None:
                rows.append(row)
        if not rows:
            raise RuntimeError("Los fragmentos elegidos no están en la base todavía.")

        rows.sort(key=lambda r: r["t_start"])
        fragmento = "\n".join(f"{SPEAKER.get(r['track'], r['track'])}: {r['text']}" for r in rows)

        first = rows[0]["t_start"]
        contexto_rows = self._conn.execute(
            "SELECT * FROM segments WHERE session_id = ? AND t_start < ?"
            " ORDER BY t_start DESC LIMIT ?",
            (session_id, first, int(payload.get("contexto", 4))),
        ).fetchall()
        contexto = "\n".join(
            f"{SPEAKER.get(r['track'], r['track'])}: {r['text']}" for r in reversed(contexto_rows)
        )

        spec = bridge.pick(self._prompts.candidates, payload.get("cli") or None)
        prompt = template.render(
            fragmento=fragmento, contexto=contexto, idioma=payload.get("idioma", "español")
        )
        cli = bridge.LlmCli(spec, timeout_s=self._prompts.timeout_s)
        response = cli.run(prompt)
        return {
            "text": response.text,
            "cli": response.cli_id,
            "local": spec.local,
            "elapsed": response.elapsed,
        }



# ---------------------------------------------------------------------- protocolo


async def handle(hub: Hub, socket: ServerConnection) -> None:
    hub._clients.add(socket)
    try:
        await socket.send(json.dumps({"type": "prompts", **hub.prompt_catalog()}))
        await socket.send(json.dumps({"type": "sessions", "rows": hub.sessions()}))

        async for raw in socket:
            try:
                message = json.loads(raw)
                await dispatch(hub, socket, message)
            except Exception as exc:  # noqa: BLE001 — cualquier fallo vuelve como error
                log.exception("fallo atendiendo %s", raw[:120])
                await socket.send(json.dumps({"type": "error", "message": str(exc)}))
    finally:
        hub._clients.discard(socket)


async def dispatch(hub: Hub, socket: ServerConnection, message: dict[str, Any]) -> None:
    kind = message.get("type")

    if kind == "start":
        # Cargar modelos bloquea varios segundos: fuera del loop, o la UI se congela.
        await asyncio.to_thread(hub.start_capture, message)
    elif kind == "stop":
        await asyncio.to_thread(hub.stop_capture)
    elif kind == "sessions":
        await socket.send(json.dumps({"type": "sessions", "rows": hub.sessions()}))
    elif kind == "transcript":
        rows = hub.transcript(message["session_id"])
        await socket.send(
            json.dumps({"type": "transcript", "session_id": message["session_id"], "rows": rows})
        )
    elif kind == "search":
        rows = hub.search(message.get("q", ""), message.get("session_id"))
        await socket.send(json.dumps({"type": "search_result", "rows": rows}))
    elif kind == "ask":
        answer = await asyncio.to_thread(hub.ask, message)
        await socket.send(json.dumps({"type": "answer", **answer}))
    else:
        raise RuntimeError(f"Mensaje desconocido: {kind!r}")


async def main_async(db: Path, config: Path | None, port: int) -> None:
    hub = Hub(db, config)
    hub.bind(asyncio.get_running_loop())
    pump = asyncio.create_task(hub.pump())

    async with serve(lambda s: handle(hub, s), HOST, port):
        log.info("sidecar escuchando en ws://%s:%d", HOST, port)
        print(f"granola-local sidecar en ws://{HOST}:{port}", flush=True)
        try:
            await asyncio.Future()
        finally:
            pump.cancel()
            hub.stop_capture()


def main() -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Sidecar de la UI de granola-local")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    try:
        asyncio.run(main_async(args.db, args.config, args.port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
