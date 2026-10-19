from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    """Single-process SQLite; all sensitive payloads use reviewed Fernet encryption."""

    def __init__(self, path: Path, key: str):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.crypt = Fernet(key.encode())
        self.lock = open(str(path) + ".lock", "a+")
        os.chmod(str(path) + ".lock", 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("SQLite deployment permits one application process") from None
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS objects (
          kind TEXT, id TEXT, owner TEXT, payload BLOB, updated REAL,
          PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS deliveries (
          event_id TEXT, sub_id TEXT, payload BLOB, attempts INTEGER DEFAULT 0,
          due REAL, status TEXT DEFAULT 'pending', PRIMARY KEY(event_id,sub_id));
        """)
        try:
            sentinel = self.get("system", "sentinel")
            if sentinel is None:
                self.put("system", "sentinel", "system", {"version": 1})
        except Exception:
            self.close()
            raise

    def encode(self, value):
        return self.crypt.encrypt(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())

    def decode(self, value):
        return json.loads(self.crypt.decrypt(value))

    def put(self, kind, id, owner, payload):
        self.db.execute(
            "INSERT INTO objects VALUES(?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET owner=excluded.owner,payload=excluded.payload,updated=excluded.updated",
            (kind, id, owner, self.encode(payload), time.time()),
        )

    def get(self, kind, id, owner=None):
        row = self.db.execute(
            "SELECT owner,payload FROM objects WHERE kind=? AND id=?", (kind, id)
        ).fetchone()
        if not row or (owner is not None and row["owner"] != owner):
            return None
        return self.decode(row["payload"])

    def all(self, kind, owner=None):
        rows = self.db.execute("SELECT owner,payload FROM objects WHERE kind=?", (kind,)).fetchall()
        return [self.decode(r["payload"]) for r in rows if owner is None or r["owner"] == owner]

    def delete(self, kind, id):
        self.db.execute("DELETE FROM objects WHERE kind=? AND id=?", (kind, id))

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()
        self.lock.close()
