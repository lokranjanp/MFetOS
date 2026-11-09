"""Explicit, backed-up v1 -> v2 migration. Never alter LangGraph checkpoints."""

import json
import sqlite3
from pathlib import Path

from .models import Document, Report, Run, now
from .schema import SCHEMA, SCHEMA_VERSION
from .storage import Store

KINDS = {
    "blob",
    "extraction",
    "catalogue",
    "document",
    "evidence",
    "fact",
    "run",
    "run_inputs",
    "branch_evidence",
    "run_peer_inputs",
    "resume",
    "report",
}


def migrate(root: Path) -> dict:
    root = root.resolve()
    path = root / "research.sqlite"
    if not path.exists():
        Store(root)
        return {"status": "created", "version": SCHEMA_VERSION}
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='objects'").fetchone():
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise ValueError(f"Unsupported database version {version}")
            return {"status": "current", "version": version}
        records = [dict(r) for r in db.execute("SELECT * FROM objects ORDER BY kind,id")]
        unknown = {r["kind"] for r in records} - KINDS
        if unknown:
            raise ValueError("Unknown legacy record kinds; migration aborted: " + ",".join(sorted(unknown)))
        backup = root / ("research.pre-v2-" + now().strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite")
        with sqlite3.connect(backup) as target:
            db.backup(target)
        store = Store(root, migrating=True)
        store._migration_db = db
        # executescript commits implicitly, so start the transaction INSIDE the script.
        db.executescript("BEGIN IMMEDIATE; ALTER TABLE jobs RENAME TO legacy_jobs_v1; " + SCHEMA)
        try:
            grouped = {kind: [r for r in records if r["kind"] == kind] for kind in KINDS}
            for kind in (
                "blob",
                "extraction",
                "catalogue",
                "document",
                "evidence",
                "fact",
                "run",
                "run_inputs",
                "branch_evidence",
                "run_peer_inputs",
                "resume",
                "report",
            ):
                for r in grouped[kind]:
                    value = json.loads(r["body"])
                    if kind == "blob":
                        store.save_blob(r["id"], value)
                    elif kind == "extraction":
                        store.save_extraction(r["id"], value)
                    elif kind == "catalogue":
                        store.save_catalogue(r["id"], value)
                    elif kind == "document":
                        doc = Document.model_validate(value)
                        store.save_document(doc)
                        store.index_document(doc)
                    elif kind == "evidence":
                        store.save_evidence(value)
                    elif kind == "fact":
                        store.save_fact(value)
                    elif kind == "run":
                        run = Run.model_validate(value)
                        store._ensure_ipo(run.ipo_id)
                        store.save_run(run)
                    elif kind == "run_inputs":
                        store.save_run_inputs(r["id"], value)
                    elif kind == "branch_evidence":
                        store.save_branch_evidence(r["id"], value)
                    elif kind == "run_peer_inputs":
                        store.save_peer_inputs(r["id"], value)
                    elif kind == "resume":
                        store.save_decision(r["id"], value)
                    elif kind == "report":
                        store.save_report(Report.model_validate(value))
            for r in db.execute("SELECT * FROM legacy_jobs_v1").fetchall():
                # Owners belong to stopped pre-migration processes. Reclaim the preserved job.
                store.queue_job(r["id"])
            for kind, table in (
                ("document", "documents"),
                ("evidence", "evidence"),
                ("fact", "facts"),
                ("run", "analysis_runs"),
                ("report", "reports"),
                ("catalogue", "manifests"),
            ):
                count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if count < len(grouped[kind]):
                    raise ValueError(f"Migration count mismatch: {kind}")
            # Validate complete public contracts, not just row counts.
            for r in grouped["report"]:
                before = Report.model_validate_json(r["body"])
                after = Report.model_validate(store.load_report(r["id"]))
                if before != after:
                    raise ValueError("Report changed during migration")
            for kind, loader in (("fact", store.load_fact), ("evidence", store.load_evidence)):
                from .models import Evidence, Fact

                cls = Fact if kind == "fact" else Evidence
                for r in grouped[kind]:
                    if cls.model_validate_json(r["body"]) != cls.model_validate(loader(r["id"])):
                        raise ValueError(f"{kind} changed during migration")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Foreign key validation failed")
            db.execute("DROP TABLE objects")
            db.execute("DROP TABLE pages")
            db.execute("DROP TABLE legacy_jobs_v1")
            db.execute("INSERT INTO schema_migrations VALUES(?,?)", (SCHEMA_VERSION, now().isoformat()))
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            store._migration_db = None
        return {
            "status": "migrated",
            "version": SCHEMA_VERSION,
            "backup": str(backup),
            "legacy_records": len(records),
        }
    finally:
        db.close()
