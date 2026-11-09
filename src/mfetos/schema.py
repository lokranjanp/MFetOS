"""Version 2 research schema. Exact decimals are TEXT; JSON is limited to snapshots/lists."""

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issuers(
 id TEXT PRIMARY KEY, legal_name TEXT NOT NULL, cin TEXT, isin TEXT, aliases_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ipos(
 id TEXT PRIMARY KEY, issuer_id TEXT NOT NULL REFERENCES issuers(id), board TEXT NOT NULL,
 sector TEXT NOT NULL, status TEXT NOT NULL, manifest_id TEXT);
CREATE TABLE IF NOT EXISTS manifests(
 id TEXT PRIMARY KEY, ipo_id TEXT NOT NULL REFERENCES ipos(id), snapshot_json TEXT NOT NULL,
 document_ids_json TEXT NOT NULL, extraction_ids_json TEXT NOT NULL, errors_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS document_blobs(
 sha256 TEXT PRIMARY KEY, relative_path TEXT NOT NULL, retrieved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS extractions(
 id TEXT PRIMARY KEY, sha256 TEXT NOT NULL REFERENCES document_blobs(sha256), backend TEXT NOT NULL,
 parser_version TEXT NOT NULL, options_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
 raw_artifact_path TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS document_pages(
 extraction_id TEXT NOT NULL REFERENCES extractions(id), pdf_page INTEGER NOT NULL,
 printed_page TEXT, section TEXT NOT NULL, method TEXT NOT NULL, text TEXT NOT NULL,
 provenance_json TEXT NOT NULL, PRIMARY KEY(extraction_id,pdf_page));
CREATE TABLE IF NOT EXISTS document_tables(
 id TEXT PRIMARY KEY, extraction_id TEXT NOT NULL, pdf_page INTEGER NOT NULL, table_index INTEGER NOT NULL,
 row_count INTEGER NOT NULL, column_count INTEGER NOT NULL,
 FOREIGN KEY(extraction_id,pdf_page) REFERENCES document_pages(extraction_id,pdf_page));
CREATE TABLE IF NOT EXISTS table_cells(
 table_id TEXT NOT NULL REFERENCES document_tables(id), row_index INTEGER NOT NULL,
 column_index INTEGER NOT NULL, text TEXT, PRIMARY KEY(table_id,row_index,column_index));
CREATE TABLE IF NOT EXISTS documents(
 id TEXT PRIMARY KEY, ipo_id TEXT NOT NULL REFERENCES ipos(id), sha256 TEXT NOT NULL REFERENCES document_blobs(sha256),
 extraction_id TEXT NOT NULL REFERENCES extractions(id), kind TEXT NOT NULL, source_url TEXT NOT NULL,
 published_at TEXT, retrieved_at TEXT NOT NULL, publication_status TEXT NOT NULL,
 source_json TEXT NOT NULL, warnings_json TEXT NOT NULL, extractor_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, ipo_id TEXT NOT NULL REFERENCES ipos(id), document_id TEXT NOT NULL REFERENCES documents(id),
 source_url TEXT NOT NULL, pdf_page INTEGER NOT NULL, printed_page TEXT, snippet TEXT NOT NULL,
 published_at TEXT, retrieved_at TEXT NOT NULL, validation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facts(
 id TEXT PRIMARY KEY, ipo_id TEXT NOT NULL REFERENCES ipos(id), metric TEXT NOT NULL,
 value_decimal TEXT, value_text TEXT, unit TEXT NOT NULL, currency TEXT, period_start TEXT, period_end TEXT,
 period_kind TEXT NOT NULL, basis TEXT NOT NULL, status TEXT NOT NULL, category TEXT NOT NULL, validation TEXT NOT NULL,
 CHECK ((value_decimal IS NULL) != (value_text IS NULL)));
CREATE TABLE IF NOT EXISTS fact_evidence(
 fact_id TEXT NOT NULL REFERENCES facts(id), evidence_id TEXT NOT NULL REFERENCES evidence(id),
 PRIMARY KEY(fact_id,evidence_id));
CREATE TABLE IF NOT EXISTS analysis_runs(
 id TEXT PRIMARY KEY, ipo_id TEXT NOT NULL REFERENCES ipos(id), objective TEXT NOT NULL, as_of TEXT NOT NULL,
 status TEXT NOT NULL, stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 error TEXT, decision_json TEXT, trace_id TEXT, trace_url TEXT);
CREATE TABLE IF NOT EXISTS run_snapshots(
 run_id TEXT PRIMARY KEY REFERENCES analysis_runs(id), snapshot_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_documents(
 run_id TEXT NOT NULL REFERENCES analysis_runs(id), document_id TEXT NOT NULL REFERENCES documents(id),
 extraction_id TEXT NOT NULL REFERENCES extractions(id), role TEXT NOT NULL,
 PRIMARY KEY(run_id,document_id,role));
CREATE TABLE IF NOT EXISTS run_evidence(
 run_id TEXT NOT NULL REFERENCES analysis_runs(id), evidence_id TEXT NOT NULL REFERENCES evidence(id),
 role TEXT NOT NULL, PRIMARY KEY(run_id,evidence_id,role));
CREATE TABLE IF NOT EXISTS run_facts(
 run_id TEXT NOT NULL REFERENCES analysis_runs(id), fact_id TEXT NOT NULL REFERENCES facts(id),
 role TEXT NOT NULL, PRIMARY KEY(run_id,fact_id,role));
CREATE TABLE IF NOT EXISTS human_decisions(
 run_id TEXT PRIMARY KEY REFERENCES analysis_runs(id), decision_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports(
 run_id TEXT PRIMARY KEY REFERENCES analysis_runs(id), ipo_id TEXT NOT NULL REFERENCES ipos(id),
 generated_at TEXT NOT NULL, long_term_status TEXT NOT NULL, listing_status TEXT NOT NULL, report_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analysis_branches(
 run_id TEXT NOT NULL REFERENCES analysis_runs(id), name TEXT NOT NULL, gaps_json TEXT NOT NULL,
 PRIMARY KEY(run_id,name));
CREATE TABLE IF NOT EXISTS findings(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES analysis_runs(id), branch TEXT NOT NULL,
 kind TEXT NOT NULL, text TEXT NOT NULL, evidence_ids_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calculated_metrics(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES analysis_runs(id), branch TEXT NOT NULL,
 name TEXT NOT NULL, period TEXT, value_decimal TEXT, unit TEXT NOT NULL, formula TEXT NOT NULL,
 unavailable_reason TEXT, assumptions_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metric_inputs(
 metric_id TEXT NOT NULL REFERENCES calculated_metrics(id), fact_id TEXT NOT NULL REFERENCES facts(id),
 PRIMARY KEY(metric_id,fact_id));
CREATE TABLE IF NOT EXISTS ingestion_jobs(
 id TEXT PRIMARY KEY, ipo_id TEXT NOT NULL, manifest_json TEXT NOT NULL, status TEXT NOT NULL,
 completed_documents INTEGER NOT NULL DEFAULT 0, total_documents INTEGER NOT NULL,
 result_json TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, job_type TEXT NOT NULL DEFAULT 'analysis', owner TEXT,
 leased_until REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS execution_attempts(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, run_id TEXT REFERENCES analysis_runs(id),
 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, trace_id TEXT, trace_url TEXT);
CREATE TABLE IF NOT EXISTS activity_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, correlation_id TEXT,
 run_id TEXT REFERENCES analysis_runs(id), attempt_id TEXT, stage TEXT NOT NULL,
 event TEXT NOT NULL, level TEXT NOT NULL, duration_ms REAL, error_code TEXT, counts_json TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS document_pages_fts USING fts5(
 document_id UNINDEXED, ipo_id UNINDEXED, extraction_id UNINDEXED, page UNINDEXED, section, text);
CREATE INDEX IF NOT EXISTS facts_ipo_period ON facts(ipo_id,metric,period_end,basis);
CREATE INDEX IF NOT EXISTS evidence_document_page ON evidence(document_id,pdf_page);
CREATE INDEX IF NOT EXISTS documents_ipo_publication ON documents(ipo_id,published_at);
CREATE INDEX IF NOT EXISTS runs_ipo_created ON analysis_runs(ipo_id,created_at);
CREATE INDEX IF NOT EXISTS events_correlation ON activity_events(correlation_id,id);
CREATE VIEW IF NOT EXISTS financial_facts AS
 SELECT f.ipo_id,i.legal_name,f.metric,f.value_decimal,f.value_text,f.unit,f.currency,f.period_start,
 f.period_end,f.period_kind,f.basis,f.status,f.validation,e.document_id,e.pdf_page,e.printed_page,e.source_url
 FROM facts f JOIN ipos p ON p.id=f.ipo_id JOIN issuers i ON i.id=p.issuer_id
 LEFT JOIN fact_evidence fe ON fe.fact_id=f.id LEFT JOIN evidence e ON e.id=fe.evidence_id;
CREATE VIEW IF NOT EXISTS run_summary AS
 SELECT a.id AS run_id,a.ipo_id,a.as_of,a.objective,a.status,a.stage,a.created_at,a.updated_at,
 r.long_term_status,r.listing_status,a.trace_url FROM analysis_runs a LEFT JOIN reports r ON r.run_id=a.id;
CREATE VIEW IF NOT EXISTS document_coverage AS
 SELECT d.id AS document_id,d.ipo_id,d.kind,d.source_url,d.published_at,d.publication_status,x.backend,
 (SELECT count(*) FROM document_pages p WHERE p.extraction_id=d.extraction_id) AS page_count,
 (SELECT count(*) FROM evidence e WHERE e.document_id=d.id) AS evidence_count,
 d.warnings_json FROM documents d JOIN extractions x ON x.id=d.extraction_id;
"""
