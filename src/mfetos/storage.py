"""Small transactional local store; all graph state references immutable revisions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import Run, now


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "documents").mkdir(exist_ok=True)
        self.db = self.root / "research.sqlite"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, ipo_id TEXT, body TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT, leased_until REAL DEFAULT 0);
            CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(document_id UNINDEXED,
                ipo_id UNINDEXED, page UNINDEXED, section, text);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def put(self, kind: str, object_id: str, value: Any, ipo_id: str | None = None):
        body = value.model_dump_json() if isinstance(value, BaseModel) else json.dumps(value, default=str)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?,?)", (kind, object_id, ipo_id, body))

    def get(self, kind: str, object_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", (kind, object_id)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind: str, ipo_id: str | None = None) -> list[dict]:
        with self.connect() as db:
            sql = "SELECT body FROM objects WHERE kind=?"
            args: tuple = (kind,)
            if ipo_id is not None:
                sql += " AND ipo_id=?"
                args += (ipo_id,)
            return [json.loads(r[0]) for r in db.execute(sql + " ORDER BY id", args)]

    def enqueue(self, run: Run, inputs: dict):
        with self.connect() as db:
            for kind, value in (("run", run.model_dump(mode="json")), ("run_inputs", inputs)):
                db.execute(
                    "INSERT OR IGNORE INTO objects VALUES(?,?,?,?)",
                    (kind, run.run_id, run.ipo_id, json.dumps(value)),
                )
            db.execute("INSERT OR IGNORE INTO jobs(id) VALUES(?)", (run.run_id,))

    def claim(self, owner: str, seconds: float = 60) -> str | None:
        import time

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM jobs WHERE leased_until<? ORDER BY rowid LIMIT 1", (time.time(),)
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET owner=?, leased_until=? WHERE id=?", (owner, time.time() + seconds, row[0])
            )
            return row[0]

    def heartbeat(self, run_id: str, owner: str):
        import time

        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET leased_until=? WHERE id=? AND owner=?", (time.time() + 60, run_id, owner)
            )

    def finish(self, run_id: str, owner: str):
        with self.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=? AND owner=?", (run_id, owner))

    def update_run(self, run_id: str, **changes) -> Run:
        run = Run.model_validate(self.get("run", run_id))
        for k, v in changes.items():
            setattr(run, k, v)
        run.updated_at = now()
        self.put("run", run_id, run, run.ipo_id)
        return run

    def index_document(self, doc):
        with self.connect() as db:
            db.execute("DELETE FROM pages WHERE document_id=?", (doc.document_id,))
            db.executemany(
                "INSERT INTO pages VALUES(?,?,?,?,?)",
                [(doc.document_id, doc.ipo_id, p.pdf_page, p.section, p.text) for p in doc.pages],
            )

    def retrieve(self, ipo_id: str, query: str, limit: int = 8) -> list[dict]:
        # Caller supplies keywords, never SQL/FTS operators from retrieved content.
        import re

        tokens = re.findall(r"[a-zA-Z0-9]+", query)
        if not tokens:
            return []
        match = " OR ".join('"' + t + '"' for t in tokens[:12])
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT document_id,page,section,text FROM pages WHERE pages MATCH ? AND ipo_id=? "
                    "ORDER BY rank LIMIT ?",
                    (match, ipo_id, limit),
                )
            ]
