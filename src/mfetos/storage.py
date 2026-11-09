"""Named SQLite repositories, exact financial values and durable leased jobs."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from .models import Document, Evidence, Fact, Manifest, Report, Run, digest, now
from .schema import SCHEMA, SCHEMA_VERSION


def encode(value):
    return json.dumps(value, default=str, ensure_ascii=False)


class MigrationRequired(ValueError):
    pass


class Store:
    def __init__(self, root: Path, *, migrating=False):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "documents").mkdir(exist_ok=True)
        self.db = self.root / "research.sqlite"
        self._local = threading.local()
        self._migration_db = None
        with self.connect() as db:
            legacy = db.execute("SELECT 1 FROM sqlite_master WHERE name='objects'").fetchone()
            if legacy and not migrating:
                raise MigrationRequired(
                    "Legacy research database: run mfetos --data-dir PATH db migrate first"
                )
            if not legacy:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, SCHEMA_VERSION}:
                    raise MigrationRequired(f"Unsupported research database version {version}")
                db.executescript(SCHEMA)
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(?,?)", (SCHEMA_VERSION, now().isoformat())
                )
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @property
    def _migration_db(self):
        return getattr(self._local, "connection", None)

    @_migration_db.setter
    def _migration_db(self, value):
        self._local.connection = value

    @contextmanager
    def connect(self):
        if self._migration_db is not None:
            yield self._migration_db
            return
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _upsert(self, table, values, key="id", db=None):
        columns = list(values)
        sql = (
            f"INSERT INTO {table} ({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
            f"ON CONFLICT({key}) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in columns if c != key)
        )
        if db is not None:
            db.execute(sql, list(values.values()))
        else:
            with self.connect() as conn:
                conn.execute(sql, list(values.values()))

    def _row(self, table, identifier, key="id"):
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (identifier,)).fetchone()
            return dict(row) if row else None

    def _ensure_ipo(self, ipo_id, issuer=None):
        if self._row("ipos", ipo_id) and issuer is None:
            return
        data = issuer.model_dump(mode="json") if issuer else {"legal_name": ipo_id, "aliases": []}
        company_id = digest(data.get("cin") or data["legal_name"].lower())[:32]
        with self.connect() as db:
            self._upsert(
                "issuers",
                {
                    "id": company_id,
                    "legal_name": data["legal_name"],
                    "cin": data.get("cin"),
                    "isin": data.get("isin"),
                    "aliases_json": encode(data.get("aliases", [])),
                },
                db=db,
            )
            db.execute(
                "INSERT INTO ipos(id,issuer_id,board,sector,status) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET issuer_id=excluded.issuer_id, board=excluded.board, "
                "sector=excluded.sector,status=excluded.status",
                (
                    ipo_id,
                    company_id,
                    data.get("board", "unknown"),
                    data.get("sector", "unknown"),
                    data.get("status", "unknown"),
                ),
            )

    def save_catalogue(self, ipo_id, record):
        manifest = Manifest.model_validate(record["manifest"])
        self._ensure_ipo(ipo_id, manifest.issuer)
        mid = digest(record)
        self._upsert(
            "manifests",
            {
                "id": mid,
                "ipo_id": ipo_id,
                "snapshot_json": encode(record["manifest"]),
                "document_ids_json": encode(record["document_ids"]),
                "extraction_ids_json": encode(record.get("extraction_ids", {})),
                "errors_json": encode(record["errors"]),
                "created_at": now().isoformat(),
            },
        )
        with self.connect() as db:
            db.execute("UPDATE ipos SET manifest_id=? WHERE id=?", (mid, ipo_id))

    def load_catalogue(self, ipo_id):
        with self.connect() as db:
            r = db.execute(
                "SELECT m.* FROM ipos p JOIN manifests m ON m.id=p.manifest_id WHERE p.id=?", (ipo_id,)
            ).fetchone()
        return (
            {
                "manifest": json.loads(r["snapshot_json"]),
                "document_ids": json.loads(r["document_ids_json"]),
                "extraction_ids": json.loads(r["extraction_ids_json"]),
                "errors": json.loads(r["errors_json"]),
            }
            if r
            else None
        )

    def list_catalogue(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM ipos WHERE manifest_id IS NOT NULL ORDER BY id")]
        return [self.load_catalogue(i) for i in ids]

    def save_blob(self, sha, value):
        self._upsert(
            "document_blobs",
            {"sha256": sha, "relative_path": f"documents/{sha}.pdf", "retrieved_at": value["retrieved_at"]},
            key="sha256",
        )

    def load_blob(self, sha):
        row = self._row("document_blobs", sha, "sha256")
        return {"retrieved_at": row["retrieved_at"]} if row else None

    def save_extraction(self, extraction_id, value):
        sha = value.get("sha256", extraction_id)
        if not self.load_blob(sha):
            self.save_blob(sha, {"retrieved_at": now().isoformat()})
        with self.connect() as db:
            # Extraction revisions are immutable. Version/options are part of the caller's key.
            if db.execute("SELECT 1 FROM extractions WHERE id=?", (extraction_id,)).fetchone():
                return
            db.execute(
                "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?)",
                (
                    extraction_id,
                    sha,
                    value.get("backend", "pdfplumber"),
                    value.get("extractor_version", "1"),
                    encode(value.get("options", {})),
                    encode(value.get("warnings", [])),
                    value.get("raw_artifact_path"),
                    now().isoformat(),
                ),
            )
            for page in value["pages"]:
                db.execute(
                    "INSERT INTO document_pages VALUES(?,?,?,?,?,?,?)",
                    (
                        extraction_id,
                        page["pdf_page"],
                        page.get("printed_page"),
                        page.get("section", "other"),
                        page.get("method", "text"),
                        page["text"],
                        encode(page.get("provenance", [])),
                    ),
                )
                for i, table in enumerate(page.get("tables", [])):
                    tid = digest([extraction_id, page["pdf_page"], i])[:32]
                    db.execute(
                        "INSERT INTO document_tables VALUES(?,?,?,?,?,?)",
                        (
                            tid,
                            extraction_id,
                            page["pdf_page"],
                            i,
                            len(table),
                            max(map(len, table), default=0),
                        ),
                    )
                    db.executemany(
                        "INSERT INTO table_cells VALUES(?,?,?,?)",
                        [(tid, r, c, cell) for r, row in enumerate(table) for c, cell in enumerate(row)],
                    )

    def load_extraction(self, extraction_id):
        record = self._row("extractions", extraction_id)
        if not record:
            return None
        with self.connect() as db:
            pages = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM document_pages WHERE extraction_id=? ORDER BY pdf_page", (extraction_id,)
                )
            ]
            for p in pages:
                p.pop("extraction_id")
                p["provenance"] = json.loads(p.pop("provenance_json"))
                tables = []
                for table in db.execute(
                    "SELECT * FROM document_tables WHERE extraction_id=? AND pdf_page=? ORDER BY table_index",
                    (extraction_id, p["pdf_page"]),
                ):
                    grid = [[None] * table["column_count"] for _ in range(table["row_count"])]
                    for cell in db.execute("SELECT * FROM table_cells WHERE table_id=?", (table["id"],)):
                        grid[cell["row_index"]][cell["column_index"]] = cell["text"]
                    tables.append(grid)
                p["tables"] = tables
        return {
            "pages": pages,
            "warnings": json.loads(record["warnings_json"]),
            "backend": record["backend"],
            "extractor_version": record["parser_version"],
            "sha256": record["sha256"],
            "options": json.loads(record["options_json"]),
            "raw_artifact_path": record["raw_artifact_path"],
        }

    def save_document(self, doc):
        doc = Document.model_validate(doc)
        self._ensure_ipo(doc.ipo_id)
        if not self.load_blob(doc.sha256):
            self.save_blob(doc.sha256, {"retrieved_at": doc.retrieved_at.isoformat()})
        xid = doc.extraction_id or doc.sha256
        if not self._row("extractions", xid):
            self.save_extraction(
                xid,
                {
                    "sha256": doc.sha256,
                    "pages": [p.model_dump(mode="json") for p in doc.pages],
                    "warnings": doc.warnings,
                    "backend": doc.parser_backend,
                    "extractor_version": doc.extractor_version,
                },
            )
        self._upsert(
            "documents",
            {
                "id": doc.document_id,
                "ipo_id": doc.ipo_id,
                "sha256": doc.sha256,
                "extraction_id": xid,
                "kind": doc.source.kind,
                "source_url": doc.source.source_url,
                "published_at": doc.source.published_at.isoformat() if doc.source.published_at else None,
                "retrieved_at": doc.retrieved_at.isoformat(),
                "publication_status": doc.source.publication_status,
                "source_json": doc.source.model_dump_json(),
                "warnings_json": encode(doc.warnings),
                "extractor_version": doc.extractor_version,
            },
        )

    def load_document(self, document_id, extraction_id=None):
        r = self._row("documents", document_id)
        if not r:
            return None
        xid = extraction_id or r["extraction_id"]
        x = self.load_extraction(xid)
        return {
            "document_id": r["id"],
            "ipo_id": r["ipo_id"],
            "source": json.loads(r["source_json"]),
            "sha256": r["sha256"],
            "retrieved_at": r["retrieved_at"],
            "pages": x["pages"],
            "warnings": x["warnings"],
            "extractor_version": x["extractor_version"],
            "extraction_id": xid,
            "parser_backend": x["backend"],
        }

    def save_evidence(self, value):
        e = Evidence.model_validate(value).model_dump(mode="json")
        e["id"] = e.pop("evidence_id")
        self._upsert("evidence", e)

    def load_evidence(self, evidence_id):
        r = self._row("evidence", evidence_id)
        if r:
            r["evidence_id"] = r.pop("id")
        return r

    def save_fact(self, value):
        f = Fact.model_validate(value)
        values = f.model_dump(mode="json", exclude={"value", "evidence_ids", "fact_id"})
        values.update(
            id=f.fact_id,
            value_decimal=str(f.value) if isinstance(f.value, Decimal) else None,
            value_text=None if isinstance(f.value, Decimal) else f.value,
        )
        with self.connect() as db:
            self._upsert("facts", values, db=db)
            db.executemany(
                "INSERT OR IGNORE INTO fact_evidence VALUES(?,?)",
                [(f.fact_id, eid) for eid in f.evidence_ids],
            )

    def load_fact(self, fact_id):
        r = self._row("facts", fact_id)
        if r:
            r["fact_id"] = r.pop("id")
            value = r.pop("value_decimal")
            text = r.pop("value_text")
            r["value"] = value if value is not None else text
            with self.connect() as db:
                r["evidence_ids"] = [
                    x[0]
                    for x in db.execute(
                        "SELECT evidence_id FROM fact_evidence WHERE fact_id=? ORDER BY evidence_id",
                        (fact_id,),
                    )
                ]
        return r

    def save_run(self, value):
        r = Run.model_validate(value).model_dump(mode="json")
        r["id"] = r.pop("run_id")
        r["decision_json"] = encode(r.pop("decision"))
        self._upsert("analysis_runs", r)

    def load_run(self, run_id):
        r = self._row("analysis_runs", run_id)
        if r:
            r["run_id"] = r.pop("id")
            r["decision"] = json.loads(r.pop("decision_json"))
        return r

    def update_run(self, run_id, **changes):
        run = Run.model_validate(self.load_run(run_id))
        for key, value in changes.items():
            setattr(run, key, value)
        run.updated_at = now()
        self.save_run(run)
        return run

    def save_run_inputs(self, run_id, value):
        with self.connect() as db:
            self._upsert(
                "run_snapshots", {"run_id": run_id, "snapshot_json": encode(value)}, key="run_id", db=db
            )
            roles = [("issuer", value)] + [("peer", p) for p in value.get("peers", {}).values()]
            for role, payload in roles:
                for did in payload.get("document_ids", []):
                    d = self._row("documents", did)
                    if d:
                        xid = payload.get("extraction_ids", {}).get(did, d["extraction_id"])
                        db.execute(
                            "INSERT OR IGNORE INTO run_documents VALUES(?,?,?,?)", (run_id, did, xid, role)
                        )

    def load_run_inputs(self, run_id):
        r = self._row("run_snapshots", run_id, "run_id")
        return json.loads(r["snapshot_json"]) if r else None

    def save_branch_evidence(self, run_id, ids):
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO run_evidence VALUES(?,?,?)", [(run_id, i, "business") for i in ids]
            )

    def load_branch_evidence(self, run_id):
        with self.connect() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT evidence_id FROM run_evidence WHERE run_id=? AND role='business' ORDER BY evidence_id",
                    (run_id,),
                )
            ]

    def save_peer_inputs(self, run_id, value):
        with self.connect() as db:
            for table, col, key in (
                ("run_facts", "fact_id", "fact_ids"),
                ("run_evidence", "evidence_id", "evidence_ids"),
            ):
                db.executemany(
                    f"INSERT OR IGNORE INTO {table}(run_id,{col},role) VALUES(?,?,?)",
                    [(run_id, i, "peer") for i in value[key]],
                )

    def load_peer_inputs(self, run_id):
        with self.connect() as db:
            return {
                key: [
                    r[0]
                    for r in db.execute(
                        f"SELECT {col} FROM {table} WHERE run_id=? AND role='peer' ORDER BY {col}", (run_id,)
                    )
                ]
                for table, col, key in (
                    ("run_facts", "fact_id", "fact_ids"),
                    ("run_evidence", "evidence_id", "evidence_ids"),
                )
            }

    def save_decision(self, run_id, value):
        self._upsert("human_decisions", {"run_id": run_id, "decision_json": encode(value)}, key="run_id")

    def load_decision(self, run_id):
        r = self._row("human_decisions", run_id, "run_id")
        return json.loads(r["decision_json"]) if r else None

    def clear_decision(self, run_id):
        with self.connect() as db:
            db.execute("DELETE FROM human_decisions WHERE run_id=?", (run_id,))

    def save_report(self, value):
        report = Report.model_validate(value)
        with self.connect() as db:
            self._upsert(
                "reports",
                {
                    "run_id": report.run_id,
                    "ipo_id": report.ipo_id,
                    "generated_at": report.generated_at.isoformat(),
                    "long_term_status": report.long_term.status,
                    "listing_status": report.listing.status,
                    "report_json": report.model_dump_json(),
                },
                key="run_id",
                db=db,
            )
            for name, branch in report.branches.items():
                db.execute(
                    "INSERT INTO analysis_branches VALUES(?,?,?) ON CONFLICT(run_id,name) DO UPDATE SET gaps_json=excluded.gaps_json",
                    (report.run_id, name, encode(branch.gaps)),
                )
                for i, claim in enumerate(branch.claims):
                    self._upsert(
                        "findings",
                        {
                            "id": digest([report.run_id, name, "claim", i]),
                            "run_id": report.run_id,
                            "branch": name,
                            "kind": claim.kind,
                            "text": claim.text,
                            "evidence_ids_json": encode(claim.evidence_ids),
                        },
                        db=db,
                    )
                for i, metric in enumerate(branch.metrics):
                    mid = digest([report.run_id, name, "metric", i])
                    self._upsert(
                        "calculated_metrics",
                        {
                            "id": mid,
                            "run_id": report.run_id,
                            "branch": name,
                            "name": metric.name,
                            "period": metric.period,
                            "value_decimal": str(metric.value) if metric.value is not None else None,
                            "unit": metric.unit,
                            "formula": metric.formula,
                            "unavailable_reason": metric.unavailable_reason,
                            "assumptions_json": encode(metric.assumptions),
                        },
                        db=db,
                    )
                    db.executemany(
                        "INSERT OR IGNORE INTO metric_inputs VALUES(?,?)",
                        [(mid, fid) for fid in metric.input_ids],
                    )
            db.executemany(
                "INSERT OR IGNORE INTO run_facts VALUES(?,?,?)",
                [(report.run_id, f.fact_id, "report") for f in report.facts],
            )

    def load_report(self, run_id):
        r = self._row("reports", run_id, "run_id")
        return json.loads(r["report_json"]) if r else None

    def enqueue(self, run, inputs):
        # One transaction: clients never receive a durable ID without its job and frozen inputs.
        with self.connect() as db:
            old = self._migration_db
            self._migration_db = db
            try:
                self.save_run(run)
                self.save_run_inputs(run.run_id, inputs)
                self.queue_job(run.run_id)
            finally:
                self._migration_db = old

    def queue_job(self, identifier, job_type="analysis"):
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,job_type,created_at) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING",
                (identifier, job_type, now().isoformat()),
            )

    def claim(self, owner, seconds=60):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute(
                "SELECT id FROM jobs WHERE leased_until<? ORDER BY created_at,id LIMIT 1", (time.time(),)
            ).fetchone()
            if r:
                db.execute(
                    "UPDATE jobs SET owner=?,leased_until=? WHERE id=?", (owner, time.time() + seconds, r[0])
                )
            return r[0] if r else None

    def job_kind(self, identifier):
        row = self._row("jobs", identifier)
        return row["job_type"] if row else None

    def heartbeat(self, identifier, owner):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET leased_until=? WHERE id=? AND owner=?", (time.time() + 60, identifier, owner)
            )

    def finish(self, identifier, owner):
        with self.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=? AND owner=?", (identifier, owner))

    def save_ingestion(self, value):
        self._upsert("ingestion_jobs", value)

    def load_ingestion(self, identifier):
        return self._row("ingestion_jobs", identifier)

    def index_document(self, doc):
        with self.connect() as db:
            db.execute(
                "DELETE FROM document_pages_fts WHERE document_id=? AND extraction_id=?",
                (doc.document_id, doc.extraction_id or doc.sha256),
            )
            db.executemany(
                "INSERT INTO document_pages_fts VALUES(?,?,?,?,?,?)",
                [
                    (
                        doc.document_id,
                        doc.ipo_id,
                        doc.extraction_id or doc.sha256,
                        p.pdf_page,
                        p.section,
                        p.text,
                    )
                    for p in doc.pages
                ],
            )

    def retrieve(self, ipo_id, query, limit=8):
        tokens = re.findall(r"[a-zA-Z0-9]+", query)
        if not tokens:
            return []
        match = " OR ".join('"' + t + '"' for t in tokens[:12])
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT document_id,page,section,text FROM document_pages_fts "
                    "WHERE document_pages_fts MATCH ? AND ipo_id=? ORDER BY rank LIMIT ?",
                    (match, ipo_id, limit),
                )
            ]

    def table_names(self):
        with self.connect() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE (type='table' OR type='view') "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'document_pages_fts%' ORDER BY name"
                )
            ]

    def browse(self, table, limit=50, offset=0, ipo_id=None, run_id=None):
        if table not in self.table_names():
            raise ValueError("Unknown public table")
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
        with self.connect() as db:
            columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            conditions, args = [], []
            for col, value in (("ipo_id", ipo_id), ("run_id", run_id)):
                if value and col in columns:
                    conditions.append(f"{col}=?")
                    args.append(value)
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            total = db.execute(f'SELECT count(*) FROM "{table}"{where}', args).fetchone()[0]
            rows = [
                dict(r)
                for r in db.execute(
                    f'SELECT * FROM "{table}"{where} LIMIT ? OFFSET ?', args + [limit, offset]
                )
            ]
        return {"rows": rows, "total": total, "columns": sorted(columns)}

    def summary(self):
        return [{"table": name, "rows": self.browse(name, 1)["total"]} for name in self.table_names()]

    def record_event(self, payload):
        value = dict(payload)
        value["counts_json"] = encode(value.pop("counts"))
        columns = list(value)
        with self.connect() as db:
            db.execute(
                f"INSERT INTO activity_events({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                list(value.values()),
            )

    def start_attempt(self, identifier, job_id, run_id, trace_id):
        self._upsert(
            "execution_attempts",
            {
                "id": identifier,
                "job_id": job_id,
                "run_id": run_id,
                "started_at": now().isoformat(),
                "finished_at": None,
                "status": "running",
                "trace_id": trace_id,
                "trace_url": None,
            },
        )
        if run_id:
            self.update_run(run_id, trace_id=trace_id, trace_url=None)

    def finish_attempt(self, identifier, status):
        with self.connect() as db:
            db.execute(
                "UPDATE execution_attempts SET finished_at=?,status=? WHERE id=?",
                (now().isoformat(), status, identifier),
            )

    def set_trace_url(self, trace_id, url):
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            return
        with self.connect() as db:
            db.execute("UPDATE execution_attempts SET trace_url=? WHERE trace_id=?", (url, trace_id))
            db.execute("UPDATE analysis_runs SET trace_url=? WHERE trace_id=?", (url, trace_id))
