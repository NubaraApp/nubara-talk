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
import os
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
    answers_for_session,
    connect,
    create_session,
    end_session,
    save_answer,
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

# Las plantillas están escritas en español, así que el nombre del idioma también.
LANG_NAMES = {
    "en": "inglés",
    "es": "español",
    "pt": "portugués",
    "fr": "francés",
    "de": "alemán",
    "it": "italiano",
    "ca": "catalán",
    "nl": "neerlandés",
}


# Lo que el usuario escribe en el cuadro de instrucciones entra en cada prompt. Se
# recorta: un prompt gigante hace lento al modelo y no mejora la respuesta.
MAX_INSTRUCCIONES = 2000


def format_instructions(text: str | None) -> str:
    """Prepara el bloque de instrucciones, o devuelve vacío.

    Va formateado desde acá y no desde la plantilla para que, sin instrucciones, el
    prompt no arranque con un encabezado colgando de una sección vacía.
    """
    clean = (text or "").strip()[:MAX_INSTRUCCIONES]
    if not clean:
        return ""
    return f"Quien te pregunta te da este contexto y estas instrucciones:\n{clean}\n"


# La transcripción entera no entra de una en el contexto del modelo. Se corta en tramos,
# se resume cada uno y después se resume el conjunto. El corte se mide en caracteres
# porque es lo único que se puede contar sin cargar acá el tokenizador del modelo.
MAX_CHARS_TRAMO = 6000

_PUNCT = str.maketrans("", "", ".,;:!?¿¡\"'()[]…-—")


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _norm(text: str) -> str:
    return " ".join(text.lower().translate(_PUNCT).split())


def _same_phrase(a: str, b: str) -> bool:
    """Si dos textos ya normalizados son la misma frase.

    No es igualdad a secas porque las dos pistas transcriben el mismo audio con errores
    distintos, y porque una suele quedar cortada antes que la otra.
    """
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 12 and short in long_


def dedup_turns(rows: list[sqlite3.Row], window: float = 2.5) -> list[sqlite3.Row]:
    """Saca el eco entre pistas.

    El micrófono levanta también lo que suena por los parlantes, así que la misma frase
    suele aparecer en `mic` y en `sys` con menos de un segundo de diferencia. Mandarla
    dos veces al resumen no sólo gasta contexto: el modelo la lee como si se hubiera
    dicho dos veces. Ante un duplicado gana `sys`, que es la fuente sin ruido de sala.
    """
    kept: list[sqlite3.Row] = []
    for row in rows:
        norm = _norm(row["text"] or "")
        if not norm:
            continue
        dup = None
        for other in reversed(kept):
            if row["t_start"] - other["t_start"] > window:
                break
            if _same_phrase(norm, _norm(other["text"] or "")):
                dup = other
                break
        if dup is None:
            kept.append(row)
        elif dup["track"] == "mic" and row["track"] == "sys":
            kept[kept.index(dup)] = row
    return kept


def chunk_lines(lines: list[str], limit: int = MAX_CHARS_TRAMO) -> list[str]:
    """Agrupa renglones en tramos de a lo sumo `limit` caracteres, sin partir renglones."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def answer_language(
    rows: list[sqlite3.Row], asked: str | None, policy: str, propio: str
) -> str:
    """En qué idioma tiene que contestar el LLM.

    Depende de la acción, no sólo del fragmento. Responderle a alguien va en el idioma
    en que habló; traducir, explicar o resumir van en el idioma en que yo leo —
    traducir al mismo idioma del original no traduce nada. Cada plantilla declara cuál
    de los dos quiere en `config/prompts.yaml`.
    """
    if asked and asked != "auto":
        return asked
    if policy != "fragmento":
        return propio or "español"

    votes: dict[str, int] = {}
    for row in rows:
        code = (row["lang"] or "").lower()
        if code:
            votes[code] = votes.get(code, 0) + 1
    if not votes:
        return propio or "español"
    winner = max(votes, key=lambda code: votes[code])
    return LANG_NAMES.get(winner, winner)


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
            # La sesión se guarda con reloj de pared para poder mostrarla como fecha;
            # los tiempos de cada segmento siguen siendo relativos a `session_start`.
            create_session(self._conn, session_id, time.time())
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
            end_session(self._conn, session_id, time.time())

        self.emit("state", running=False, session_id=session_id)

    # ------------------------------------------------------------------ consultas

    def answers(self, session_id: str) -> list[dict[str, Any]]:
        return [_row(r) for r in answers_for_session(self._conn, session_id)]

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
                {
                    "id": t.id,
                    "nombre": t.nombre,
                    "corto": t.corto,
                    "descripcion": t.descripcion,
                    "idioma": t.idioma,
                    # Las que amplían algo ya contestado no se ofrecen sueltas: la
                    # ventana las pone como botón sobre cada respuesta.
                    "usa_respuesta": t.usa_respuesta,
                    "ampliar_con": t.ampliar_con,
                    # Las de sesión corren sobre la reunión entera: la ventana las
                    # dispara desde el botón de resumen, no desde cada frase.
                    "alcance": t.alcance,
                }
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

    def _pick_cli(self, *wanted: str | None) -> Any:
        """Elige con qué CLI correr.

        Preferencia explícita de la ventana, si no la de la plantilla, si no la primera
        instalada. Una preferencia que no está instalada no puede romper la llamada: se
        cae al orden de siempre.
        """
        for name in wanted:
            if not name:
                continue
            try:
                candidate = bridge.pick(self._prompts.candidates, name)
            except bridge.LlmError:
                continue
            if bridge.available(candidate):
                return candidate
        return bridge.pick(self._prompts.candidates)

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

        spec = self._pick_cli(payload.get("cli"), template.cli)
        idioma = answer_language(
            rows, payload.get("idioma"), template.idioma, payload.get("propio") or "español"
        )
        prompt = template.render(
            fragmento=fragmento,
            contexto=contexto,
            idioma=idioma,
            respuesta=payload.get("respuesta", ""),
            instrucciones=format_instructions(payload.get("instrucciones")),
        )
        cli = bridge.LlmCli(spec, timeout_s=self._prompts.timeout_s)
        # Los trozos salen por el canal de broadcast a medida que el modelo escribe: la
        # respuesta completa tarda lo mismo, pero se empieza a leer varios segundos antes.
        self.emit(
            "answer_start",
            template=template.id,
            cli=spec.id,
            local=spec.local,
            idioma=idioma,
            fragmento=fragmento,
            parent_id=payload.get("parent_id"),
        )
        response = cli.run(prompt, on_chunk=lambda piece: self.emit("answer_chunk", text=piece))

        created_at = time.time()
        answer_id = save_answer(
            self._conn,
            session_id=session_id,
            created_at=created_at,
            template=template.id,
            cli=response.cli_id,
            local=spec.local,
            lang=idioma,
            fragmento=fragmento,
            turnos=json.dumps(keys),
            text=response.text,
            parent_id=payload.get("parent_id"),
        )
        return {
            "id": answer_id,
            "created_at": created_at,
            "template": template.id,
            "fragmento": fragmento,
            # Los turnos vuelven con la respuesta: son los que necesita la ventana
            # para poder pedir una ampliación de esta misma respuesta.
            "turnos": json.dumps(keys),
            "parent_id": payload.get("parent_id"),
            "text": response.text,
            "idioma": idioma,
            "cli": response.cli_id,
            "local": spec.local,
            "elapsed": response.elapsed,
        }


    # ------------------------------------------------------------------ resumen

    def session_lines(self, session_id: str) -> tuple[list[str], list[dict[str, Any]]]:
        """La conversación entera como renglones `Quién: texto`, sin ecos ni parciales."""
        rows = [r for r in transcript(self._conn, session_id) if (r["text"] or "").strip()]
        # Los parciales son texto que todavía podía cambiar; si hay confirmados, sobran.
        confirmed = [r for r in rows if r["status"] == "confirmed"]
        kept = dedup_turns(confirmed or rows)
        lines = [f"{SPEAKER.get(r['track'], r['track'])}: {r['text'].strip()}" for r in kept]
        keys = [{"track": r["track"], "turn_index": r["turn_index"]} for r in kept]
        return lines, keys

    def summarize(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resume la reunión entera.

        Si la transcripción no entra en una sola pasada se resume por tramos y después
        se resume el conjunto. Es más lento y pierde algún detalle, pero es preferible a
        mandar un prompt que el modelo va a truncar por el medio sin avisar.
        """
        session_id = payload.get("session_id") or self._session_id
        if not session_id:
            raise RuntimeError("No hay ninguna reunión abierta para resumir.")

        final = self._prompts.prompts.get("resumen_reunion")
        if final is None:
            raise RuntimeError("Falta la plantilla 'resumen_reunion' en config/prompts.yaml.")

        lines, keys = self.session_lines(session_id)
        if not lines:
            raise RuntimeError("Esa reunión no tiene nada transcrito todavía.")

        propio = payload.get("propio") or "español"
        asked = payload.get("idioma")
        idioma = asked if asked and asked != "auto" else propio
        instrucciones = format_instructions(payload.get("instrucciones"))
        started = time.perf_counter()

        # Un resumen se pide una vez, al final, y sobre un texto mucho más largo que una
        # respuesta: el tiempo de espera de siempre se queda corto.
        timeout_s = max(self._prompts.timeout_s, 300.0)

        chunks = chunk_lines(lines)
        if len(chunks) > 1:
            parte = self._prompts.prompts.get("resumen_parte")
            if parte is None:
                raise RuntimeError("Falta la plantilla 'resumen_parte' en config/prompts.yaml.")
            spec_parte = self._pick_cli(parte.cli)
            notas = []
            for i, chunk in enumerate(chunks, 1):
                self.emit("status", message=f"resumiendo tramo {i} de {len(chunks)}…")
                salida = bridge.LlmCli(spec_parte, timeout_s=timeout_s).run(
                    parte.render(fragmento=chunk, idioma=idioma)
                )
                notas.append(f"--- tramo {i} de {len(chunks)} ---\n{salida.text.strip()}")
            material = "\n\n".join(notas)
        else:
            material = chunks[0]

        spec = self._pick_cli(payload.get("cli"), final.cli)
        etiqueta = f"Reunión entera · {len(keys)} turnos"
        if len(chunks) > 1:
            etiqueta += f" · {len(chunks)} tramos"

        self.emit("status", message="armando el resumen…")
        self.emit(
            "answer_start",
            template=final.id,
            cli=spec.id,
            local=spec.local,
            idioma=idioma,
            fragmento=etiqueta,
            parent_id=None,
        )
        response = bridge.LlmCli(spec, timeout_s=timeout_s).run(
            final.render(fragmento=material, idioma=idioma, instrucciones=instrucciones),
            on_chunk=lambda piece: self.emit("answer_chunk", text=piece),
        )

        created_at = time.time()
        # Se guarda como una respuesta más: así vuelve sola al reabrir la sesión, sin
        # una tabla ni un panel aparte. En `fragmento` va la etiqueta y no la
        # transcripción: el texto completo ya está en `segments`, y los turnos exactos
        # quedan en `turnos`.
        answer_id = save_answer(
            self._conn,
            session_id=session_id,
            created_at=created_at,
            template=final.id,
            cli=response.cli_id,
            local=spec.local,
            lang=idioma,
            fragmento=etiqueta,
            turnos=json.dumps(keys),
            text=response.text,
            parent_id=None,
        )
        self.emit("status", message="resumen listo")
        return {
            "id": answer_id,
            "created_at": created_at,
            "template": final.id,
            "fragmento": etiqueta,
            "turnos": json.dumps(keys),
            "parent_id": None,
            "text": response.text,
            "idioma": idioma,
            "cli": response.cli_id,
            "local": spec.local,
            "elapsed": time.perf_counter() - started,
            "tramos": len(chunks),
            "turnos_usados": len(keys),
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
        session_id = message["session_id"]
        rows = hub.transcript(session_id)
        await socket.send(
            json.dumps({"type": "transcript", "session_id": session_id, "rows": rows})
        )
        # Las respuestas viajan con la transcripción: abrir una sesión vieja tiene que
        # traer también lo que el LLM había contestado en su momento.
        await socket.send(
            json.dumps(
                {"type": "answers", "session_id": session_id, "rows": hub.answers(session_id)}
            )
        )
    elif kind == "answers":
        session_id = message.get("session_id") or hub._session_id
        rows = hub.answers(session_id) if session_id else []
        await socket.send(json.dumps({"type": "answers", "session_id": session_id, "rows": rows}))
    elif kind == "search":
        rows = hub.search(message.get("q", ""), message.get("session_id"))
        await socket.send(json.dumps({"type": "search_result", "rows": rows}))
    elif kind == "ask":
        answer = await asyncio.to_thread(hub.ask, message)
        await socket.send(json.dumps({"type": "answer", **answer}))
    elif kind == "resumen":
        answer = await asyncio.to_thread(hub.summarize, message)
        await socket.send(json.dumps({"type": "answer", **answer}))
    else:
        raise RuntimeError(f"Mensaje desconocido: {kind!r}")


def watch_parent(interval: float = 2.0) -> None:
    """Se apaga si muere quien lo lanzó.

    La ventana mata al sidecar al cerrarse, pero eso sólo cubre el cierre normal: si la
    app se cae o la matan con una señal, el sidecar queda vivo capturando audio y
    ocupando el puerto. Cuando el padre muere, `getppid()` pasa a 1.
    """
    parent = os.getppid()
    if parent <= 1:
        return  # ya arrancó suelto, no hay a quién vigilar

    def loop() -> None:
        while True:
            time.sleep(interval)
            if os.getppid() != parent:
                log.info("el proceso padre murió; cerrando el sidecar")
                os._exit(0)

    threading.Thread(target=loop, name="watch-parent", daemon=True).start()


async def main_async(db: Path, config: Path | None, port: int) -> None:
    watch_parent()
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
