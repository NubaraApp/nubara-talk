"""Esquema SQLite y apertura de la base.

F0 no escribe nada todavía: el esquema queda listo para F1. Ver ADR-007.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/granola.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id         TEXT PRIMARY KEY,
  started_at REAL NOT NULL,
  ended_at   REAL,
  title      TEXT,
  summary    TEXT
);

CREATE TABLE IF NOT EXISTS segments (
  id         INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  track      TEXT NOT NULL CHECK (track IN ('mic','sys')),
  turn_index INTEGER NOT NULL,    -- contador de turnos por pista dentro de la sesión
  t_start    REAL NOT NULL,       -- segundos desde el inicio de la sesión
  t_end      REAL NOT NULL,
  lang       TEXT,
  text       TEXT NOT NULL,
  text_trad  TEXT,
  status     TEXT NOT NULL DEFAULT 'provisional'
             CHECK (status IN ('provisional','confirmed'))
);

-- La UI recorre una sesión en orden cronológico intercalando las dos pistas.
CREATE INDEX IF NOT EXISTS idx_segments_session_time ON segments(session_id, t_start);

-- Un turno tiene una sola fila: los parciales la reescriben hasta que el turno cierra.
CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_turn
  ON segments(session_id, track, turn_index);

-- Lo que fue contestando el LLM. Se guardan para poder releerlas: en una reunión uno
-- pide varias seguidas y perder la anterior al pedir la siguiente es perder el hilo.
CREATE TABLE IF NOT EXISTS answers (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  created_at  REAL NOT NULL,     -- reloj de pared
  template    TEXT NOT NULL,
  cli         TEXT NOT NULL,
  local       INTEGER NOT NULL,  -- si la CLI corrió en esta máquina
  lang        TEXT,
  fragmento   TEXT NOT NULL,     -- lo que se le pasó, para saber a qué contesta
  turnos      TEXT NOT NULL,     -- JSON con los turnos elegidos, para poder ampliar
  text        TEXT NOT NULL,
  parent_id   INTEGER REFERENCES answers(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_answers_session ON answers(session_id, created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
  text,
  text_trad,
  content='segments',
  content_rowid='id'
);

-- Sincronización del índice FTS. Con content='segments' el índice no guarda el texto,
-- así que hay que avisarle de cada cambio a mano. El row 'delete' tiene que repetir los
-- valores viejos: es así como FTS5 sabe qué tokens descontar.
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
  INSERT INTO segments_fts(rowid, text, text_trad)
  VALUES (new.id, new.text, new.text_trad);
END;

CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
  INSERT INTO segments_fts(segments_fts, rowid, text, text_trad)
  VALUES ('delete', old.id, old.text, old.text_trad);
END;

CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
  INSERT INTO segments_fts(segments_fts, rowid, text, text_trad)
  VALUES ('delete', old.id, old.text, old.text_trad);
  INSERT INTO segments_fts(rowid, text, text_trad)
  VALUES (new.id, new.text, new.text_trad);
END;
"""


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Abre la base creando el esquema si hace falta.

    WAL porque en F1 el hilo de STT escribe mientras la UI lee: sin WAL, cada escritura
    bloquea a los lectores.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()

    log.debug("base abierta en %s", path)
    return conn


def create_session(conn: sqlite3.Connection, session_id: str, started_at: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sessions(id, started_at) VALUES (?, ?)",
        (session_id, started_at),
    )
    conn.commit()


def end_session(conn: sqlite3.Connection, session_id: str, ended_at: float) -> None:
    conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (ended_at, session_id))
    conn.commit()


def upsert_segment(
    conn: sqlite3.Connection,
    session_id: str,
    track: str,
    turn_index: int,
    t_start: float,
    t_end: float,
    text: str,
    lang: str | None,
    status: str,
) -> None:
    """Escribe el turno, pisando el parcial anterior si ya había uno.

    Un turno confirmado no vuelve a provisional: los parciales de un turno pueden llegar
    después del final si la cola se desordenó, y no tienen que degradar lo ya confirmado.
    """
    conn.execute(
        """
        INSERT INTO segments(session_id, track, turn_index, t_start, t_end, text, lang, status)
        VALUES (:sid, :track, :idx, :t0, :t1, :text, :lang, :status)
        ON CONFLICT(session_id, track, turn_index) DO UPDATE SET
            t_end  = excluded.t_end,
            text   = excluded.text,
            lang   = excluded.lang,
            status = excluded.status
        WHERE segments.status != 'confirmed'
        """,
        {
            "sid": session_id,
            "track": track,
            "idx": turn_index,
            "t0": t_start,
            "t1": t_end,
            "text": text,
            "lang": lang,
            "status": status,
        },
    )
    conn.commit()


def transcript(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """La sesión completa en orden cronológico, con las dos pistas intercaladas."""
    return list(
        conn.execute(
            "SELECT * FROM segments WHERE session_id = ? ORDER BY t_start, track",
            (session_id,),
        )
    )


def search(
    conn: sqlite3.Connection, query: str, session_id: str | None = None, limit: int = 50
) -> list[sqlite3.Row]:
    """Búsqueda de texto completo sobre transcripción y traducción.

    El operador MATCH de FTS5 exige el nombre de la tabla virtual a la izquierda, no un
    alias: `f MATCH ?` falla con "no such column: f". Por eso la consulta vive acá y no
    se escribe a mano en cada llamador.
    """
    sql = """
        SELECT s.*, bm25(segments_fts) AS rank
        FROM segments_fts
        JOIN segments s ON s.id = segments_fts.rowid
        WHERE segments_fts MATCH :q
    """
    params: dict[str, object] = {"q": query, "limit": limit}
    if session_id is not None:
        sql += " AND s.session_id = :sid"
        params["sid"] = session_id
    sql += " ORDER BY rank LIMIT :limit"
    return list(conn.execute(sql, params))


def update_translation(
    conn: sqlite3.Connection, session_id: str, track: str, turn_index: int, text_trad: str
) -> None:
    """Guarda la traducción de un turno ya escrito.

    Va aparte de `upsert_segment` porque la traducción llega después que el texto: el
    turno se persiste apenas transcribe y se completa cuando el traductor termina. El
    trigger de UPDATE reindexa la fila en FTS5, así que `text_trad` queda buscable.
    """
    conn.execute(
        """
        UPDATE segments SET text_trad = :trad
        WHERE session_id = :sid AND track = :track AND turn_index = :idx
        """,
        {"trad": text_trad, "sid": session_id, "track": track, "idx": turn_index},
    )
    conn.commit()


def save_answer(
    conn: sqlite3.Connection,
    session_id: str,
    created_at: float,
    template: str,
    cli: str,
    local: bool,
    lang: str,
    fragmento: str,
    turnos: str,
    text: str,
    parent_id: int | None = None,
) -> int:
    """Guarda una respuesta y devuelve su id.

    `parent_id` apunta a la respuesta que se amplió, si esta es una ampliación.
    """
    cur = conn.execute(
        """
        INSERT INTO answers(session_id, created_at, template, cli, local, lang,
                            fragmento, turnos, text, parent_id)
        VALUES (:sid, :at, :tpl, :cli, :local, :lang, :frag, :turnos, :text, :parent)
        """,
        {
            "sid": session_id,
            "at": created_at,
            "tpl": template,
            "cli": cli,
            "local": 1 if local else 0,
            "lang": lang,
            "frag": fragmento,
            "turnos": turnos,
            "text": text,
            "parent": parent_id,
        },
    )
    conn.commit()
    return int(cur.lastrowid)


def answers_for_session(
    conn: sqlite3.Connection, session_id: str, limit: int = 100
) -> list[sqlite3.Row]:
    """Las respuestas de una sesión, de la más nueva a la más vieja."""
    return list(
        conn.execute(
            "SELECT * FROM answers WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        )
    )
