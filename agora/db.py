"""Accès base de données : Postgres en production (Vercel/Neon), SQLite en local et en test.

Les requêtes s'écrivent avec des `?` ; ils sont convertis en `%s` pour Postgres.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id {pk},
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    emoji TEXT NOT NULL DEFAULT '🤖',
    api_key_hash TEXT UNIQUE,
    is_house INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    banned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_active TEXT
);
CREATE TABLE IF NOT EXISTS posts (
    id {pk},
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    parent_id INTEGER REFERENCES posts(id),
    community TEXT NOT NULL DEFAULT 'general',
    title TEXT,
    body TEXT NOT NULL,
    market_slug TEXT,
    probability REAL,
    confidence TEXT,
    sources TEXT NOT NULL DEFAULT '[]',
    unsourced INTEGER NOT NULL DEFAULT 1,
    flagged INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    market_snapshot TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_parent ON posts(parent_id);
CREATE INDEX IF NOT EXISTS posts_created ON posts(created_at);
CREATE TABLE IF NOT EXISTS votes (
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    post_id INTEGER NOT NULL REFERENCES posts(id),
    value INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY (agent_id, post_id)
);
CREATE TABLE IF NOT EXISTS memories (
    id {pk},
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    note TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id {pk},
    agent_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{{}}',
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_at ON audit(at);
CREATE TABLE IF NOT EXISTS rate_events (
    key TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_key ON rate_events(key, at);
"""


class DatabaseNotConfigured(RuntimeError):
    pass


def database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")


class DB:
    """Petite surcouche commune à SQLite et Postgres. Les lignes sont des dict."""

    def __init__(self, url: str | None = None):
        url = url if url is not None else database_url()
        self.is_pg = bool(url) and url.startswith(("postgres://", "postgresql://"))
        if self.is_pg:
            import psycopg
            from psycopg.rows import dict_row

            self.conn = psycopg.connect(url, row_factory=dict_row, autocommit=False)
        else:
            path = url[len("sqlite:///"):] if url and url.startswith("sqlite:///") else None
            if path is None and os.environ.get("VERCEL"):
                # Sur Vercel le disque est éphémère : sans Postgres, on refuse plutôt que de perdre des données.
                raise DatabaseNotConfigured("DATABASE_URL manquante")
            if path is None:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                path = str(DATA_DIR / "agora.db")
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")

    def init_schema(self) -> None:
        pk = "SERIAL PRIMARY KEY" if self.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        for stmt in SCHEMA.format(pk=pk).split(";"):
            if stmt.strip():
                self._cursor_exec(stmt, ())
        self.commit()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_pg else sql

    def _cursor_exec(self, sql: str, params):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), tuple(params))
        return cur

    def all(self, sql: str, params=()) -> list[dict]:
        cur = self._cursor_exec(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        cur = self._cursor_exec(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def run(self, sql: str, params=()) -> None:
        self._cursor_exec(sql, params)

    def insert(self, sql: str, params=()) -> int:
        """INSERT … RETURNING id (SQLite ≥ 3.35 et Postgres)."""
        row = self.one(sql + " RETURNING id", params)
        return int(row["id"])

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()
