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
