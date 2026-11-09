"""Durable LangGraph fan-out/fan-in workflow; adapters are ordinary Python calls."""

from __future__ import annotations

import asyncio
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, TimeoutPolicy, interrupt

from .analysis import (
    demand_branch,
    financial_quality,
    make_report,
    peer_branch,
    research_branch,
    supported_sector,
)
from .demand import subscription_matches
from .ingestion import evidence_for, extract_facts, select_documents, select_facts
from .models import Branch, Claim, Document, Evidence, Fact, Manifest, Report, Run
from .providers import OllamaResearch


def merge(left: dict, right: dict) -> dict:
    return {**left, **right}


class AnalysisState(TypedDict, total=False):
    run_id: str
    document_ids: list[str]
    fact_ids: list[str]
    evidence_ids: list[str]
    branches: Annotated[dict[str, dict], merge]
    conflicts: list[str]
    gaps: list[str]
    unsupported: bool
    core_ok: bool
    retrieval_round: int


def build_graph(service, checkpointer):
    store = service.store

    def inputs(state):
        raw = store.load_run_inputs(state["run_id"])
        return (
            Run.model_validate(store.load_run(state["run_id"])),
            Manifest.model_validate(raw["manifest"]),
            raw,
        )

    def stage(state, name):
        store.update_run(state["run_id"], stage=name)

    def documents(state):
        raw = store.load_run_inputs(state["run_id"])
        return [
            Document.model_validate(store.load_document(i, raw.get("extraction_ids", {}).get(i)))
            for i in state.get("document_ids", [])
        ]

    def facts(state):
        return [Fact.model_validate(store.load_fact(i)) for i in state.get("fact_ids", [])]

    async def resolve(state):
        stage(state, "resolve_issuer_cutoff")
        run, manifest, raw = inputs(state)
        issuer = manifest.issuer
        unsupported = issuer.board == "sme" or (
            issuer.sector != "unknown" and not supported_sector(issuer.sector)
        )
        if issuer.board == "unknown" or issuer.sector == "unknown":
            answer = interrupt(
                {
                    "reason": "Issuer board/sector requires an explicit decision",
                    "issuer": issuer.model_dump(),
                    "required": ["board", "sector"],
                }
            )
            issuer.board = answer.get("board", issuer.board)
            issuer.sector = answer.get("sector", issuer.sector)
            if issuer.board == "unknown" or issuer.sector == "unknown":
                raise ValueError("Resume must resolve board and sector")
            raw["manifest"] = manifest.model_dump(mode="json")
            store.save_run_inputs(run.run_id, raw)
            unsupported = issuer.board == "sme" or not supported_sector(issuer.sector)
        return {"unsupported": unsupported, "retrieval_round": 0, "gaps": [], "conflicts": []}

    async def discover(state):
        stage(state, "discover_version_documents")
        run, manifest, raw = inputs(state)
        docs = [Document.model_validate(store.load_document(i)) for i in raw["document_ids"]]
        applicable = select_documents(docs, run.as_of)
        gaps = [
            f"Document excluded: {d.source.kind}; publication unverified/unknown or after cutoff"
            for d in docs
            if d not in applicable
        ]
        return {"document_ids": [d.document_id for d in applicable], "gaps": state.get("gaps", []) + gaps}

    async def normalise(state):
        stage(state, "extract_normalise")
        all_facts, all_evidence = [], []
        docs = documents(state)
        run, manifest, raw = inputs(state)
        if docs:
            import re

            cover = " ".join(p.text for d in docs for p in d.pages[:3])
            cin = manifest.issuer.cin

            def normalise_name(text):
                return re.sub(r"[^a-z0-9]", "", text.lower())

            name_matches = any(
                normalise_name(n) in normalise_name(cover)
                for n in [manifest.issuer.legal_name] + manifest.issuer.aliases
            )
            if (cin and cin not in cover) or not name_matches:
                answer = interrupt(
                    {
                        "reason": "Supplied legal name/CIN does not match document covers; resolve legal identity",
                        "cin": cin,
                        "required": ["accept_identity"],
                    }
                )
                if answer.get("accept_identity") is not True:
                    raise ValueError("Identity unresolved")
        for doc in docs:
            fs, es = await asyncio.to_thread(extract_facts, doc)
            all_facts.extend(fs)
            all_evidence.extend(es)
        selected, conflicts = select_facts(all_facts, docs, all_evidence)
        for e in all_evidence:
            store.save_evidence(e)
        for f in all_facts:
            store.save_fact(f)
        # Store conflict state on selected facts too.
        for f in selected:
            store.save_fact(f)
        # Imported demand must reference existing, eligible evidence for this issue.
        ev_ids = {e.evidence_id for e in all_evidence}
        for s in manifest.subscriptions:
            for eid in s.evidence_ids:
                e = store.load_evidence(eid)
                if e and e["ipo_id"] == run.ipo_id:
                    ev = Evidence.model_validate(e)
                    if ev.published_at and ev.published_at <= run.as_of:
                        ev_ids.add(eid)
        return {
            "fact_ids": [f.fact_id for f in selected],
            "evidence_ids": sorted(ev_ids),
            "conflicts": conflicts,
        }

    async def gate(state):
        stage(state, "evidence_quality_gate")
        fs = facts(state)
        periods = {
            f.period_end
            for f in fs
            if f.metric == "revenue" and f.period_kind == "annual" and f.validation == "validated"
        }
        core_ok = bool(periods) and not state.get("unsupported")
        return {"core_ok": core_ok}

    def route_gate(state):
        if state.get("unsupported"):
            return "challenge"
        if state.get("core_ok"):
            return "business"
        if state.get("retrieval_round", 0) < service.settings.max_retrieval_rounds:
            return "retrieve_gaps"
        return "challenge"

    async def retrieve_gaps(state):
        stage(state, "targeted_gap_retrieval")
        run, manifest, raw = inputs(state)
        # Retry only declared, eligible inputs that previously failed. No speculative endpoints.
        warnings = []
        existing = {
            Document.model_validate(store.load_document(i)).source.location for i in raw["document_ids"]
        }
        for spec in manifest.documents:
            if spec.location not in existing and spec.published_at and spec.published_at <= run.as_of:
                try:
                    from .ingestion import ingest

                    doc = await asyncio.wait_for(
                        ingest(store, run.ipo_id, spec, service.fetcher, service.settings),
                        timeout=service.settings.docling_timeout + 60,
                    )
                    raw["document_ids"].append(doc.document_id)
                    raw.setdefault("extraction_ids", {})[doc.document_id] = doc.extraction_id
                except Exception as exc:
                    warnings.append(f"Targeted retrieval unavailable: {type(exc).__name__}")
        store.save_run_inputs(run.run_id, raw)
        # Record targeted section retrieval for recoverable extraction gaps.
        pages = store.retrieve(run.ipo_id, "revenue profit consolidated cash flow", limit=6)
        return {
            "retrieval_round": state.get("retrieval_round", 0) + 1,
            "gaps": state.get("gaps", [])
            + warnings
            + ([] if pages else ["No relevant sections found in targeted retrieval"]),
        }

    async def business(state):
        stage(state, "parallel_analysis")
        docs = documents(state)
        branch, evs = research_branch(docs)
        if service.settings.llm_provider == "ollama":
            for doc in docs[:1]:
                selected = [p for p in doc.pages if p.section in {"business", "governance"}][:8]
                try:
                    batch = await asyncio.wait_for(
                        OllamaResearch(service.settings, observer=service.observer).extract(
                            [{"pdf_page": p.pdf_page, "text": p.text[:8000]} for p in selected]
                        ),
                        timeout=service.settings.model_timeout,
                    )
                    page_map = {p.pdf_page: p for p in selected}
                    for c in batch.candidates:
                        p = page_map.get(c.pdf_page)
                        if p and c.quote in p.text:
                            e = evidence_for(doc, p, c.quote)
                            evs.append(e)
                            branch.claims.extend(
                                [
                                    Claim(
                                        kind="management_claim", text=c.quote, evidence_ids=[e.evidence_id]
                                    ),
                                    Claim(
                                        kind="inference", text=c.interpretation, evidence_ids=[e.evidence_id]
                                    ),
                                ]
                            )
                        else:
                            branch.gaps.append(
                                "Rejected model candidate: quote did not match supplied source page"
                            )
                except Exception as exc:
                    branch.gaps.append(f"Optional model unavailable: {type(exc).__name__}")
        for e in evs:
            store.save_evidence(e)
        store.save_branch_evidence(state["run_id"], [e.evidence_id for e in evs])
        return {"branches": {branch.name: branch.model_dump(mode="json")}}

    async def financial(state):
        branch = financial_quality(facts(state))
        return {"branches": {branch.name: branch.model_dump(mode="json")}}

    async def peers(state):
        run, manifest, raw = inputs(state)
        peer_data = {}
        peer_fact_ids, peer_evidence_ids = [], []
        for pid, payload in raw.get("peers", {}).items():
            pf, pe, pc = service.facts_for_ids(
                payload["document_ids"], run.as_of, payload.get("extraction_ids")
            )
            peer_data[pid] = (Manifest.model_validate(payload["manifest"]), pf)
            peer_fact_ids.extend(f.fact_id for f in pf)
            peer_evidence_ids.extend(e.evidence_id for e in pe)
        store.save_peer_inputs(run.run_id, {"fact_ids": peer_fact_ids, "evidence_ids": peer_evidence_ids})
        branch = peer_branch(facts(state), peer_data, run.as_of, manifest.peer_rationales)
        return {"branches": {branch.name: branch.model_dump(mode="json")}}

    async def demand(state):
        run, manifest, raw = inputs(state)
        valid_ids = set(state.get("evidence_ids", []))
        valid = [
            s
            for s in manifest.subscriptions
            if set(s.evidence_ids) <= valid_ids
            and subscription_matches(s, [store.load_evidence(e) for e in s.evidence_ids])
        ]
        branch = demand_branch(valid, run.as_of, facts(state))
        if len(valid) < len(manifest.subscriptions):
            branch.gaps.append("Subscription rows rejected: missing or ineligible page evidence")
        return {"branches": {branch.name: branch.model_dump(mode="json")}}

    async def challenge(state):
        stage(state, "challenge_thesis")
        fs = facts(state)
        gaps = list(state.get("gaps", []))
        if not state.get("core_ok"):
            gaps.append("Critical issuer financial evidence unavailable; analysis abstained")
        # Deterministic contradiction checks, no invented adversarial facts.
        by = {
            (f.metric, f.period_end, f.basis): f
            for f in fs
            if isinstance(f.value, __import__("decimal").Decimal)
        }
        conflicts = list(state.get("conflicts", []))
        for f in fs:
            if f.metric == "pat" and f.period_end:
                owners = by.get(("pat_owners", f.period_end, f.basis))
                if owners and abs(owners.value) > abs(f.value) * 10:
                    conflicts.append("Owners PAT versus total PAT requires reconciliation")
        return {"gaps": gaps, "conflicts": conflicts}

    async def validate_render(state):
        stage(state, "validate_claims_citations")
        run, manifest, raw = inputs(state)
        ev_ids = set(state.get("evidence_ids", [])) | set(store.load_branch_evidence(run.run_id) or [])
        peer_inputs = store.load_peer_inputs(run.run_id) or {"fact_ids": [], "evidence_ids": []}
        ev_ids.update(peer_inputs["evidence_ids"])
        evs = [Evidence.model_validate(store.load_evidence(eid)) for eid in sorted(ev_ids)]
        branches = {k: Branch.model_validate(v) for k, v in state.get("branches", {}).items()}
        report = make_report(
            run,
            manifest,
            facts(state),
            evs,
            branches,
            state.get("conflicts", []),
            state.get("gaps", []),
            state.get("unsupported", False),
            [Fact.model_validate(store.load_fact(i)) for i in peer_inputs["fact_ids"]],
        )
        # Round-trip ensures the exact persisted JSON satisfies the public contract.
        report = Report.model_validate_json(report.model_dump_json())
        store.save_report(report)
        status = "unsupported" if state.get("unsupported") else "insufficient_evidence"
        if report.long_term.status == "supported" or report.listing.status == "supported":
            status = "completed"
        store.update_run(run.run_id, status=status, stage="finished", error=None)
        return {}

    g = StateGraph(AnalysisState)
    nodes = {
        "resolve": resolve,
        "discover": discover,
        "normalise": normalise,
        "gate": gate,
        "retrieve_gaps": retrieve_gaps,
        "business": business,
        "financial": financial,
        "peers": peers,
        "demand": demand,
        "challenge": challenge,
        "validate_render": validate_render,
    }
    for name, fn in nodes.items():

        def instrument(node_name, node_fn):
            async def wrapped(state):
                with service.observer.span(
                    "graph." + node_name, correlation_id=state["run_id"], run_id=state["run_id"]
                ):
                    return await node_fn(state)

            return wrapped

        g.add_node(
            name,
            instrument(name, fn),
            retry_policy=RetryPolicy(max_attempts=1),
            timeout=TimeoutPolicy(run_timeout=600 if name == "retrieve_gaps" else 180),
        )
    g.add_edge(START, "resolve")
    g.add_edge("resolve", "discover")
    g.add_edge("discover", "normalise")
    g.add_edge("normalise", "gate")
    g.add_edge("retrieve_gaps", "discover")

    # The sufficient route fans out in one superstep; join waits for all four branches.
    # Gate fans to all branches together; a list-valued route avoids sequential branch work.
    def fanout(state):
        route = route_gate(state)
        return ["business", "financial", "peers", "demand"] if route == "business" else route

    g.add_conditional_edges("gate", fanout)
    g.add_edge(["business", "financial", "peers", "demand"], "challenge")
    g.add_edge("challenge", "validate_render")
    g.add_edge("validate_render", END)
    return g.compile(checkpointer=checkpointer)
