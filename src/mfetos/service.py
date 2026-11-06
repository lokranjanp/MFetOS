from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from .analysis import peer_branch, subscriptions_at
from .config import Settings
from .demand import subscription_matches
from .ingestion import evidence_for, extract_facts, ingest, select_documents, select_facts
from .models import (
    Document,
    Evidence,
    Manifest,
    Report,
    Run,
    SearchResult,
    Subscription,
    ToolResult,
    digest,
    now,
)
from .providers import Fetcher, ProviderError
from .reporting import markdown
from .storage import Store
from .workflow import build_graph


class ResearchService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.store = Store(self.settings.data_dir)
        self.fetcher = Fetcher(self.settings)

    async def register(self, manifest: Manifest) -> ToolResult:
        issuer = manifest.issuer
        ids, errors = [], []
        for source in manifest.documents:
            try:
                doc = await asyncio.wait_for(
                    ingest(self.store, issuer.ipo_id, source, self.fetcher), timeout=600
                )
                ids.append(doc.document_id)
            except Exception as exc:
                # Exception classes are safe to persist; remote bodies/credential URLs are not.
                reason = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
                errors.append(f"{source.kind}: {reason}; input unavailable")
        for obs in manifest.subscription_sources:
            match = next(
                (
                    Document.model_validate(self.store.get("document", i))
                    for i in ids
                    if self.store.get("document", i)["source"]["source_url"] == obs.source_url
                ),
                None,
            )
            page = next((p for p in match.pages if p.pdf_page == obs.pdf_page), None) if match else None
            # Conservative adapter contract: labelled original line, not a loose number match.
            expected = (
                f"{obs.category} scope {obs.scope} bids {obs.bids} offered {obs.offered} "
                f"{obs.unit} anchors {obs.anchor_treatment} observed {obs.observed_at.isoformat()}"
            )
            if (
                not page
                or " ".join(obs.quote.split()) not in " ".join(page.text.split())
                or expected.lower() not in " ".join(obs.quote.lower().split())
            ):
                errors.append("Subscription source rejected: source line/labels/values do not match")
                continue
            if (
                not match.source.published_at
                or match.source.publication_status != "verified"
                or not match.source.publication_evidence
            ):
                errors.append("Subscription source rejected: publication unavailable")
                continue
            if obs.observed_at > match.source.published_at:
                errors.append("Subscription source rejected: observation follows document publication")
                continue
            ev = evidence_for(match, page, obs.quote)
            self.store.put("evidence", ev.evidence_id, ev, issuer.ipo_id)
            fields = obs.model_dump(exclude={"source_url", "pdf_page", "quote"})
            snapshot = Subscription(
                snapshot_id=digest([ev.evidence_id, fields])[:32],
                ipo_id=issuer.ipo_id,
                published_at=match.source.published_at,
                evidence_ids=[ev.evidence_id],
                **fields,
            )
            manifest.subscriptions.append(snapshot)
        record = {"manifest": manifest.model_dump(mode="json"), "document_ids": ids, "errors": errors}
        self.store.put("catalogue", issuer.ipo_id, record, issuer.ipo_id)
        return ToolResult(
            status="partial" if errors else "ok",
            data={"ipo_id": issuer.ipo_id, "document_ids": ids},
            warnings=errors,
        )

    def search_ipos(
        self, query: str = "", status: str | None = None, board: str | None = None
    ) -> SearchResult:
        matches = []
        for raw in self.store.list("catalogue"):
            issuer = Manifest.model_validate(raw["manifest"]).issuer
            if query.lower() not in " ".join([issuer.legal_name, issuer.ipo_id] + issuer.aliases).lower():
                continue
            if status and issuer.status != status or board and issuer.board != board:
                continue
            matches.append(issuer)
        return SearchResult(
            issuers=matches,
            warnings=["Search covers registered issues; use CLI discover to inspect live public links"],
        )

    def record(self, ipo_id):
        raw = self.store.get("catalogue", ipo_id)
        if raw is None:
            raise ValueError("Unknown IPO; ingest a document manifest first")
        return raw

    def docs(self, ipo_id, as_of):
        raw = self.record(ipo_id)
        docs = [Document.model_validate(self.store.get("document", i)) for i in raw["document_ids"]]
        return select_documents(docs, as_of)

    def get_ipo_documents(self, ipo_id: str, as_of: datetime) -> ToolResult:
        docs = self.docs(ipo_id, as_of)
        return ToolResult(
            status="ok" if docs else "insufficient_evidence",
            data=[
                {
                    "document_id": d.document_id,
                    "sha256": d.sha256,
                    "source": d.source.model_dump(mode="json"),
                    "retrieved_at": d.retrieved_at.isoformat(),
                    "page_count": len(d.pages),
                    "warnings": d.warnings,
                }
                for d in docs
            ],
            warnings=self.record(ipo_id)["errors"],
        )

    def facts_for_ids(self, ids: list[str], as_of: datetime):
        docs = select_documents([Document.model_validate(self.store.get("document", i)) for i in ids], as_of)
        fs, es = [], []
        for doc in docs:
            f, e = extract_facts(doc)
            fs.extend(f)
            es.extend(e)
        selected, conflicts = select_facts(fs, docs, es)
        for f in selected:
            self.store.put("fact", f.fact_id, f, f.ipo_id)
        for e in es:
            self.store.put("evidence", e.evidence_id, e, e.ipo_id)
        return selected, es, conflicts

    def get_ipo_facts(self, ipo_id: str, as_of: datetime) -> ToolResult:
        fs, evs, conflicts = self.facts_for_ids(self.record(ipo_id)["document_ids"], as_of)
        return ToolResult(
            status="ok" if fs else "insufficient_evidence",
            data={
                "facts": [f.model_dump(mode="json") for f in fs],
                "evidence": [e.model_dump(mode="json") for e in evs],
            },
            warnings=conflicts,
        )

    def get_peer_comparison(self, ipo_id, peer_ids, as_of):
        fs, es, conflicts = self.facts_for_ids(self.record(ipo_id)["document_ids"], as_of)
        peers = {}
        for pid in peer_ids:
            raw = self.record(pid)
            pf, pe, pc = self.facts_for_ids(raw["document_ids"], as_of)
            peers[pid] = Manifest.model_validate(raw["manifest"]), pf
            conflicts.extend(pc)
        rationale = Manifest.model_validate(self.record(ipo_id)["manifest"]).peer_rationales
        return ToolResult(
            status="partial",
            data=peer_branch(fs, peers, as_of, rationale).model_dump(mode="json"),
            warnings=conflicts,
        )

    def get_subscription_snapshot(self, ipo_id, as_of):
        manifest = Manifest.model_validate(self.record(ipo_id)["manifest"])
        valid = []
        for s in manifest.subscriptions:
            es = [self.store.get("evidence", i) for i in s.evidence_ids]
            if all(
                e
                and e["ipo_id"] == ipo_id
                and Evidence.model_validate(e).published_at
                and Evidence.model_validate(e).published_at <= as_of
                for e in es
            ) and subscription_matches(s, es):
                valid.append(s)
        rows, warnings = subscriptions_at(valid, as_of)
        return ToolResult(status="ok" if rows else "unavailable", data=rows, warnings=warnings)

    def start_ipo_analysis(self, ipo_id, objective="both", as_of=None) -> Run:
        as_of = as_of or now()
        if as_of.tzinfo is None:
            raise ValueError("as_of must have a timezone")
        import hashlib
        from datetime import timezone
        from pathlib import Path

        as_of = as_of.astimezone(timezone.utc)
        raw = self.record(ipo_id)
        manifest = Manifest.model_validate(raw["manifest"])
        peers = {pid: self.record(pid) for pid in manifest.peer_ids}
        # Explicit cutoffs + immutable input revisions make duplicate requests idempotent.
        inputs = {
            **raw,
            "peers": peers,
            "analysis_version": digest(
                {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in Path(__file__).parent.glob("*.py")
                }
            ),
            "llm_provider": self.settings.llm_provider,
            "llm_model": self.settings.ollama_model,
        }
        run_id = digest([ipo_id, objective, as_of.isoformat(), inputs])[:32]
        existing = self.store.get("run", run_id)
        if existing:
            return Run.model_validate(existing)
        run = Run(run_id=run_id, ipo_id=ipo_id, objective=objective, as_of=as_of)
        self.store.enqueue(run, inputs)
        return run

    def get_analysis_status(self, run_id) -> Run:
        raw = self.store.get("run", run_id)
        if raw is None:
            raise ValueError("Unknown run_id")
        return Run.model_validate(raw)

    def get_analysis_report(self, run_id) -> ToolResult:
        run = self.get_analysis_status(run_id)
        raw = self.store.get("report", run_id)
        if not raw:
            return ToolResult(status="not_ready", data=run.model_dump(mode="json"))
        report = Report.model_validate(raw)
        return ToolResult(
            status="ok", data={"report": report.model_dump(mode="json"), "markdown": markdown(report)}
        )

    def get_evidence(self, evidence_id) -> Evidence:
        raw = self.store.get("evidence", evidence_id)
        if raw is None:
            raise ValueError("Unknown evidence_id")
        return Evidence.model_validate(raw)

    async def execute(self, run_id: str, graph=None):
        current = self.get_analysis_status(run_id)
        if current.status in {"completed", "insufficient_evidence", "unsupported"}:
            return
        if graph is None:
            async with AsyncSqliteSaver.from_conn_string(
                str(self.store.root / "checkpoints.sqlite")
            ) as saver:
                return await self.execute(run_id, build_graph(self, saver))
        self.store.update_run(run_id, status="running", error=None)
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 40}
        saved = await graph.aget_state(config)
        decision = self.store.get("resume", run_id)
        value = (
            Command(resume=decision)
            if saved.interrupts and decision
            else None
            if saved.values
            else {"run_id": run_id}
        )
        if decision:
            with self.store.connect() as db:
                db.execute("DELETE FROM objects WHERE kind='resume' AND id=?", (run_id,))
        try:
            await asyncio.wait_for(graph.ainvoke(value, config, durability="sync"), timeout=1800)
            saved = await graph.aget_state(config)
            if saved.interrupts:
                self.store.update_run(run_id, status="awaiting_decision", decision=saved.interrupts[0].value)
        except asyncio.CancelledError:
            # Leave the lease/checkpoint recoverable on process shutdown.
            raise
        except Exception as exc:
            self.store.update_run(
                run_id,
                status="failed",
                error=f"{type(exc).__name__}: analysis failed; inspect inputs and retry with corrected revision",
            )

    def resume(self, run_id: str, decision: dict):
        run = self.get_analysis_status(run_id)
        if run.status != "awaiting_decision":
            raise ValueError("Run is not awaiting a decision")
        self.store.put("resume", run_id, decision)
        self.store.update_run(run_id, status="queued", decision=None)
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO jobs(id) VALUES(?)", (run_id,))

    def retry(self, run_id: str):
        run = self.get_analysis_status(run_id)
        if run.status != "failed":
            raise ValueError("Only failed runs can be retried")
        self.store.update_run(run_id, status="queued", error=None)
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO jobs(id) VALUES(?)", (run_id,))

    async def worker(self):
        owner = str(uuid.uuid4())
        async with AsyncSqliteSaver.from_conn_string(str(self.store.root / "checkpoints.sqlite")) as saver:
            graph = build_graph(self, saver)
            while True:
                run_id = self.store.claim(owner)
                if not run_id:
                    await asyncio.sleep(0.25)
                    continue

                async def heartbeat():
                    while True:
                        await asyncio.sleep(15)
                        self.store.heartbeat(run_id, owner)

                task = asyncio.create_task(heartbeat())
                try:
                    await self.execute(run_id, graph)
                    self.store.finish(run_id, owner)
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    @asynccontextmanager
    async def workers(self):
        tasks = [asyncio.create_task(self.worker()) for _ in range(self.settings.worker_concurrency)]
        try:
            yield self
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
