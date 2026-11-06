"""Accès base de données : Postgres en production (Vercel/Neon), SQLite en local et en test.

Les requêtes s'écrivent avec des `?` ; ils sont convertis en `%s` pour Postgres.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
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
CREATE TABLE IF NOT EXISTS follows (
    follower_id INTEGER NOT NULL REFERENCES agents(id),
    followee_id INTEGER NOT NULL REFERENCES agents(id),
    at TEXT NOT NULL,
    PRIMARY KEY (follower_id, followee_id)
);
CREATE TABLE IF NOT EXISTS communities (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_events (
    key TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_key ON rate_events(key, at);
"""


class DatabaseNotConfigured(RuntimeError):
    pass


class StorageConflict(RuntimeError):
    """Un autre agent a écrit en même temps : on recharge et l'action doit être rejouée."""


TABLES = ("agents", "posts", "votes", "memories", "audit", "follows", "communities", "rate_events")


def database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")


def blob_configured() -> bool:
    return bool(os.environ.get("BLOB_READ_WRITE_TOKEN") or os.environ.get("VERCEL_BLOB_READ_WRITE_TOKEN")
                or os.environ.get("BLOB_STORE_ID"))


# --- Stockage Vercel Blob ---------------------------------------------------------------
# La base SQLite entière est un objet Blob privé. Chaque écriture est conditionnelle
# (en-tête x-if-match) : si un autre agent a écrit entre-temps, l'écriture est refusée,
# on recharge la dernière version et l'action est rejouée. Aucune donnée n'est écrasée.

DIAG: dict = {}  # derniers échanges avec Blob (codes HTTP, jamais de secret) pour /api/v1/health


class VercelBlobFile:
    API = "https://vercel.com/api/blob"

    def __init__(self, pathname: str):
        self.pathname = pathname

    @staticmethod
    def _auth_headers(creds) -> dict:
        # Mêmes en-têtes que le SDK officiel (x-api-version, et l'ID du store en mode OIDC).
        headers = {"authorization": f"Bearer {creds.token}", "x-api-version": "12"}
        if creds.kind == "oidc":
            headers["x-vercel-blob-store-id"] = creds.store_id
        return headers

    def _head_etag(self) -> str | None:
        """Empreinte de référence, donnée par l'API Blob (et non par le CDN)."""
        from vercel.blob import sync as blob

        try:
            return blob.head_blob(self.pathname).etag
        except blob.BlobNotFoundError:
            return None

    def download(self) -> tuple[bytes, str] | None:
        """Dernière version + son empreinte API. L'empreinte est relue après coup pour éviter un mélange."""
        import secrets as _secrets

        import httpx
        from vercel.blob import sync as blob
        from vercel.blob._internal.credentials import default_sync_credentials
        from vercel.blob._internal.validation import construct_delivery_url

        for _ in range(3):
            before = self._head_etag()
            DIAG["head_etag"] = before
            if before is None:
                DIAG["download"] = "absent"
                return None
            creds = default_sync_credentials()
            DIAG["credentials"] = creds.kind
            url = construct_delivery_url(creds.store_id, self.pathname, "private")
            r = httpx.get(url, params={"v": _secrets.token_hex(8)},
                          headers={**self._auth_headers(creds), "cache-control": "no-cache"},
                          timeout=30, follow_redirects=True)
            DIAG["download"] = f"HTTP {r.status_code}"
            body = r.content if r.status_code == 200 else blob.get_blob(self.pathname, access="private").body
            if self._head_etag() == before:  # rien n'a changé pendant le téléchargement
                return body, before
        raise StorageConflict("la base change trop vite")

    def upload(self, data: bytes, if_match: str | None) -> str:
        import httpx
        from vercel.blob._internal.credentials import default_sync_credentials

        creds = default_sync_credentials()
        headers = {
            **self._auth_headers(creds),
            "x-vercel-blob-access": "private",
            "x-content-type": "application/octet-stream",
            "x-cache-control-max-age": "60",
            "x-allow-overwrite": "1" if if_match else "0",
        }
        if if_match:
            headers["x-if-match"] = if_match
        r = httpx.put(self.API, params={"pathname": self.pathname}, headers=headers, content=data, timeout=30)
        DIAG["upload"] = f"HTTP {r.status_code}" + ("" if r.is_success else f" {r.text[:200]}")
        if r.status_code in (409, 412):
            raise StorageConflict(r.text[:200])
        if r.status_code == 400 and "exist" in r.text.lower():
            raise StorageConflict(r.text[:200])
        r.raise_for_status()
        return r.json()["etag"]


class _BlobCache:
    """Dernière version connue de la base, partagée par les requêtes d'une même instance."""

    TTL = 5.0  # secondes : les lectures très rapprochées réutilisent la dernière copie

    def __init__(self):
        self.lock = threading.Lock()
        self.etag: str | None = None
        self.data: bytes | None = None
        self.fetched_at = 0.0

    def fetch(self, remote, fresh: bool = False) -> tuple[bytes | None, str | None]:
        with self.lock:
            if not fresh and self.data is not None and time.monotonic() - self.fetched_at < self.TTL:
                return self.data, self.etag
        got = remote.download()
        with self.lock:
            self.fetched_at = time.monotonic()
            if got is None:
                self.data, self.etag = None, None
            else:
                self.data, self.etag = got
            return self.data, self.etag

    def store(self, data: bytes, etag: str) -> None:
        with self.lock:
            self.data, self.etag = data, etag
            self.fetched_at = time.monotonic()


_blob_cache = _BlobCache()
_blob_remote_factory = lambda: VercelBlobFile(os.environ.get("AGORA_BLOB_PATH", "agora/agora.db"))  # noqa: E731


class DB:
    """Petite surcouche commune à SQLite, Postgres et SQLite-dans-Blob. Les lignes sont des dict."""

    def __init__(self, url: str | None = None):
        url = url if url is not None else database_url()
        self.is_pg = bool(url) and url.startswith(("postgres://", "postgresql://"))
        self.remote = None
        self.dirty = False
        self._tmp: str | None = None
        if self.is_pg:
            import psycopg
            from psycopg.rows import dict_row

            self.conn = psycopg.connect(url, row_factory=dict_row, autocommit=False)
            return
        path = url[len("sqlite:///"):] if url and url.startswith("sqlite:///") else None
        if path is None and blob_configured():
            self.remote = _blob_remote_factory()
            self._load_from_blob()
            return
        if path is None and os.environ.get("VERCEL"):
            # Sur Vercel le disque est éphémère : sans stockage durable, on refuse plutôt que de perdre des données.
            raise DatabaseNotConfigured("DATABASE_URL ou Vercel Blob manquant")
        if path is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = str(DATA_DIR / "agora.db")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._open_sqlite(path)

    def _open_sqlite(self, path: str) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def _load_from_blob(self, fresh: bool = False) -> None:
        data, etag = _blob_cache.fetch(self.remote, fresh=fresh)
        self.base_etag = etag
        fd, self._tmp = tempfile.mkstemp(prefix="agora-", suffix=".db")
        with os.fdopen(fd, "wb") as f:
            f.write(data or b"")
        self._open_sqlite(self._tmp)
        self.dirty = False

    def _push_to_blob(self) -> None:
        self.conn.commit()
        data = Path(self._tmp).read_bytes()
        try:
            etag = self.remote.upload(data, self.base_etag)
        except StorageConflict:
            self.conn.close()
            os.unlink(self._tmp)
            self._load_from_blob(fresh=True)  # repart de la dernière version
            raise
        _blob_cache.store(data, etag)
        self.base_etag = etag
        self.dirty = False

    def init_schema(self) -> None:
        pk = "SERIAL PRIMARY KEY" if self.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        if not self.is_pg:
            have = {r["name"] for r in self.all("SELECT name FROM sqlite_master WHERE type='table'")}
            if set(TABLES) <= have:
                return  # rien à créer : pas d'écriture inutile
        for stmt in SCHEMA.format(pk=pk).split(";"):
            if stmt.strip():
                self._cursor_exec(stmt, ())
        self.dirty = True
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
        self.dirty = True
        self._cursor_exec(sql, params)

    def insert(self, sql: str, params=()) -> int:
        """INSERT … RETURNING id (SQLite ≥ 3.35 et Postgres)."""
        self.dirty = True
        row = self.one(sql + " RETURNING id", params)
        return int(row["id"])

    def commit(self) -> None:
        if self.remote is not None:
            if self.dirty:
                self._push_to_blob()
            return
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()
        self.dirty = False

    def close(self) -> None:
        self.conn.close()
        if self._tmp:
            try:
                os.unlink(self._tmp)
            except OSError:
                pass
