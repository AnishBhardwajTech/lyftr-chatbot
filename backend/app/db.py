"""SQLite persistence for chat sessions.

The standard library sqlite3 module is enough here: every operation opens a short-lived
connection, so the code is safe to call from FastAPI's thread pool without sharing a
connection between threads. WAL mode lets reads and writes run at the same time.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    system_prompt TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    metadata    TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def ping(self) -> bool:
        with self.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return True

    # ---- sessions -------------------------------------------------------------------
    def get_session(self, session_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT session_id, system_prompt FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

    def session_exists(self, session_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? "
                "UNION SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                (session_id, session_id),
            ).fetchone()
        return row is not None

    # ---- messages -------------------------------------------------------------------
    def get_history(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Messages of one session, oldest first. ``limit`` keeps only the newest N."""
        with self.connect() as conn:
            if limit:
                rows = conn.execute(
                    "SELECT * FROM (SELECT id, role, content, metadata, created_at FROM messages "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, role, content, metadata, created_at FROM messages "
                    "WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
        history = []
        for row in rows:
            item: dict[str, Any] = {"role": row["role"], "content": row["content"]}
            if row["role"] == "assistant" and row["metadata"]:
                item["metadata"] = json.loads(row["metadata"])
            item["created_at"] = row["created_at"]
            history.append(item)
        return history

    def save_exchange(
        self,
        session_id: str,
        system_prompt: str | None,
        user_message: str,
        assistant_reply: str,
        metadata: dict[str, Any],
    ) -> None:
        """Store one user turn and its reply atomically (both or neither)."""
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, system_prompt, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "system_prompt = COALESCE(excluded.system_prompt, sessions.system_prompt), "
                "updated_at = excluded.updated_at",
                (session_id, system_prompt, now, now),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, metadata, created_at) "
                "VALUES (?, 'user', ?, NULL, ?)",
                (session_id, user_message, now),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, metadata, created_at) "
                "VALUES (?, 'assistant', ?, ?, ?)",
                (session_id, assistant_reply, json.dumps(metadata), utc_now()),
            )
