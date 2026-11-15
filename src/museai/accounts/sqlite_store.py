"""SQLite persistence for accounts, API keys and usage counters.

One file (``data/museai.db``) instead of a stack of JSON files: single-process writes,
no cross-file drift, and queries for ``/admin/usage`` that would need a full scan of a
JSON blob. Everything runs through ``asyncio.to_thread`` so the event loop never blocks
on disk.

These stores implement the same protocols as their JSON counterparts, so switching is a
config change (``MUSE2API_ACCOUNT_STORE=sqlite``).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .model import Account

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id        TEXT PRIMARY KEY,
    payload   TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash   TEXT PRIMARY KEY,
    label      TEXT NOT NULL DEFAULT '',
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash     TEXT NOT NULL,
    account_id   TEXT,
    model        TEXT,
    route        TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    status       INTEGER NOT NULL DEFAULT 200,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_created ON usage(created_at);
"""


class SqliteAccountStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def load(self) -> list[Account]:
        rows = await asyncio.to_thread(self._select, "SELECT payload FROM accounts ORDER BY id")
        return [Account.model_validate(json.loads(row[0])) for row in rows]

    async def save(self, accounts: list[Account]) -> None:
        """Replace the whole set atomically; the pool always writes the full list."""
        payloads = [(a.id, json.dumps(a.model_dump(mode="json"), ensure_ascii=False)) for a in accounts]
        async with self._lock:
            await asyncio.to_thread(self._replace, payloads)

    def _replace(self, payloads: list[tuple[str, str]]) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute("DELETE FROM accounts")
            db.executemany(
                "INSERT INTO accounts (id, payload, updated_at) VALUES (?, ?, ?)",
                [(key, body, now) for key, body in payloads],
            )

    def _select(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._connect() as db:
            return db.execute(sql, params).fetchall()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.executescript(SCHEMA)
        return db


class SqliteKeyStore:
    """API keys hashed at rest; the plaintext never touches disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def add(self, key_hash: str, label: str = "") -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, key_hash, label)

    async def revoke(self, key_hash: str) -> bool:
        return await asyncio.to_thread(self._revoke, key_hash)

    async def exists(self, key_hash: str) -> bool:
        return await asyncio.to_thread(self._exists, key_hash)

    async def list(self) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(
            self._select, "SELECT key_hash, label, revoked, created_at FROM api_keys ORDER BY created_at"
        )
        return [
            {"key_hash": row[0][:12] + "…", "label": row[1], "revoked": bool(row[2]), "created_at": row[3]}
            for row in rows
        ]

    def _write(self, key_hash: str, label: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO api_keys (key_hash, label, revoked, created_at) VALUES (?, ?, 0, ?)",
                (key_hash, label, time.time()),
            )

    def _revoke(self, key_hash: str) -> bool:
        with self._connect() as db:
            cur = db.execute("UPDATE api_keys SET revoked = 1 WHERE key_hash = ?", (key_hash,))
            return cur.rowcount > 0

    def _exists(self, key_hash: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM api_keys WHERE key_hash = ? AND revoked = 0", (key_hash,)
            ).fetchone()
            return row is not None

    def _select(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._connect() as db:
            return db.execute(sql, params).fetchall()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.executescript(SCHEMA)
        return db


class SqliteUsageStore:
    """Per-request usage rows, aggregated for ``/admin/usage``."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def record(
        self,
        *,
        key_hash: str,
        route: str,
        account_id: str | None = None,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        status: int = 200,
    ) -> None:
        await asyncio.to_thread(
            self._insert,
            (key_hash, account_id, model, route, prompt_tokens, completion_tokens, status, time.time()),
        )

    async def summary(self, since: float | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._summary, since)

    def _insert(self, row: tuple) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO usage (key_hash, account_id, model, route, prompt_tokens,"
                " completion_tokens, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )

    def _summary(self, since: float | None) -> dict[str, Any]:
        where, params = ("WHERE created_at >= ?", (since,)) if since else ("", ())
        with self._connect() as db:
            total = db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0)"
                f" FROM usage {where}",
                params,
            ).fetchone()
            by_key = db.execute(
                f"SELECT key_hash, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens) FROM usage"
                f" {where} GROUP BY key_hash ORDER BY 2 DESC LIMIT 20",
                params,
            ).fetchall()
            by_account = db.execute(
                f"SELECT COALESCE(account_id, '-'), COUNT(*), SUM(prompt_tokens), SUM(completion_tokens)"
                f" FROM usage {where} GROUP BY 1 ORDER BY 2 DESC LIMIT 20",
                params,
            ).fetchall()
        return {
            "requests": total[0],
            "prompt_tokens": total[1],
            "completion_tokens": total[2],
            "by_key": [
                {"key_hash": row[0][:12] + "…", "requests": row[1], "prompt_tokens": row[2],
                 "completion_tokens": row[3]}
                for row in by_key
            ],
            "by_account": [
                {"account_id": row[0], "requests": row[1], "prompt_tokens": row[2],
                 "completion_tokens": row[3]}
                for row in by_account
            ],
        }

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.executescript(SCHEMA)
        return db
