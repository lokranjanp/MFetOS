import json
import sqlite3

import pytest
from conftest import CUTOFF

from mfetos.migrations import migrate
from mfetos.storage import MigrationRequired, Store


def legacy_database(root, records, jobs=()):
    root.mkdir()
    with sqlite3.connect(root / "research.sqlite") as db:
        db.executescript("""
        CREATE TABLE objects(kind TEXT,id TEXT,ipo_id TEXT,body TEXT,PRIMARY KEY(kind,id));
        CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,leased_until REAL DEFAULT 0);
        CREATE VIRTUAL TABLE pages USING fts5(document_id,ipo_id,page,section,text);
        """)
        db.executemany(
            "INSERT INTO objects VALUES(?,?,?,?)",
            [(k, i, ipo, json.dumps(v, default=str)) for k, i, ipo, v in records],
        )
        db.executemany("INSERT INTO jobs VALUES(?,?,?)", [(i, "old-process", 9999999999) for i in jobs])


async def test_populated_migration_preserves_public_data_and_jobs(service, manifest, tmp_path):
    await service.register(manifest)
    run = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    await service.execute(run.run_id)
    report = service.get_analysis_report(run.run_id).data["report"]
    store = service.store
    ipo = manifest.issuer.ipo_id
    records = [
        ("catalogue", ipo, ipo, store.load_catalogue(ipo)),
        ("run", run.run_id, ipo, store.load_run(run.run_id)),
        ("run_inputs", run.run_id, ipo, {**store.load_run_inputs(run.run_id), "extraction_ids": {}}),
        ("report", run.run_id, ipo, report),
        ("branch_evidence", run.run_id, ipo, store.load_branch_evidence(run.run_id)),
        ("run_peer_inputs", run.run_id, ipo, store.load_peer_inputs(run.run_id)),
        ("resume", run.run_id, ipo, {"accept_identity": True}),
    ]
    for identifier in store.load_catalogue(ipo)["document_ids"]:
        doc = store.load_document(identifier)
        extraction = store.load_extraction(doc["extraction_id"])
        records += [
            ("blob", doc["sha256"], None, store.load_blob(doc["sha256"])),
            ("extraction", doc["sha256"], None, extraction),
            (
                "document",
                identifier,
                ipo,
                {k: v for k, v in doc.items() if k not in {"extraction_id", "parser_backend"}},
            ),
        ]
    for kind, table, loader in (
        ("evidence", "evidence", store.load_evidence),
        ("fact", "facts", store.load_fact),
    ):
        with store.connect() as db:
            ids = [r[0] for r in db.execute(f"SELECT id FROM {table}")]
        records += [(kind, i, ipo, loader(i)) for i in ids]
    old = tmp_path / "legacy"
    legacy_database(old, records, [run.run_id])
    with pytest.raises(MigrationRequired):
        Store(old)
    result = migrate(old)
    assert result["status"] == "migrated"
    assert __import__("pathlib").Path(result["backup"]).exists()
    converted = Store(old)
    assert converted.load_report(run.run_id) == report
    assert converted.load_decision(run.run_id) == {"accept_identity": True}
    assert converted.claim("new-worker") == run.run_id
    assert converted.browse("financial_facts")["total"] > 0
    assert "objects" not in converted.table_names()
    assert migrate(old)["status"] == "current"
    with converted.connect() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_migration_rejects_unknown_data_and_rolls_back(tmp_path, monkeypatch):
    root = tmp_path / "legacy"
    legacy_database(root, [("unknown-record", "id", None, {})])
    with pytest.raises(ValueError, match="Unknown legacy"):
        migrate(root)
    with sqlite3.connect(root / "research.sqlite") as db:
        assert db.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
    root2 = tmp_path / "failing"
    legacy_database(root2, [("blob", "abc", None, {"retrieved_at": "2024-01-01T00:00:00Z"})])

    def fail(*args, **kwargs):
        raise ValueError("injected migration failure")

    monkeypatch.setattr(Store, "save_blob", fail)
    with pytest.raises(ValueError, match="injected"):
        migrate(root2)
    with sqlite3.connect(root2 / "research.sqlite") as db:
        assert db.execute("SELECT count(*) FROM objects").fetchone()[0] == 1
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0


async def test_durable_ingestion_and_read_only_browser(service, manifest):
    job = service.start_ingestion(manifest)
    assert service.start_ingestion(manifest)["id"] == job["id"]
    async with service.workers():
        import asyncio

        for _ in range(100):
            if service.get_ingestion_status(job["id"])["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.1)
    assert service.get_ingestion_status(job["id"])["status"] == "completed"
    assert service.search_ipos().issuers
    with pytest.raises(ValueError):
        service.browse_data("facts; DROP TABLE facts")
    assert len(service.browse_data("document_pages", limit=2)["rows"]) == 2
