from conftest import CUTOFF

from mfetos.analysis import subscriptions_at
from mfetos.ingestion import select_documents
from mfetos.models import Fact, Report, Subscription


async def test_vertical_slice(service, manifest):
    registration = await service.register(manifest)
    assert registration.status == "ok"
    facts = service.get_ipo_facts(manifest.issuer.ipo_id, CUTOFF)
    assert facts.status == "ok"
    fs = [Fact.model_validate(f) for f in facts.data["facts"]]
    assert any(f.metric == "revenue" and f.value == 125000000 and f.period_kind == "annual" for f in fs)
    assert any(f.metric == "revenue" and f.value == 34000000 and f.period_kind == "interim" for f in fs)
    job = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    assert job.run_id == service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF).run_id
    assert service.get_analysis_report(job.run_id).status == "not_ready"
    await service.execute(job.run_id)
    status = service.get_analysis_status(job.run_id)
    assert status.status != "failed", status.error
    result = service.get_analysis_report(job.run_id)
    assert result.status == "ok", status
    report = Report.model_validate(result.data["report"])
    assert len(report.branches) == 4
    assert "PDF p." in result.data["markdown"]
    assert report.listing.status == "insufficient_evidence"
    assert len(report.scenarios) == 6
    assert report.long_term.status == "supported"  # Missing demand does not block fundamentals.
    ms = {m.name: m for m in report.branches["peers_valuation"].metrics}
    assert ms["market_cap_high"].value == 140000000
    assert ms["pe_high"].value == 10
    assert ms["enterprise_value_high"].value == 124000000  # Latest interim cash/debt, not older FY balances.
    assert report.evidence
    ev = service.get_evidence(report.evidence[0].evidence_id)
    assert ev.pdf_page >= 1


async def test_insufficient_and_unsupported(service, manifest):
    manifest.documents = []
    await service.register(manifest)
    job = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    await service.execute(job.run_id)
    report = service.get_analysis_report(job.run_id)
    assert report.status == "ok"
    assert report.data["report"]["long_term"]["status"] == "insufficient_evidence"
    manifest.issuer.board = "sme"
    await service.register(manifest)
    job = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    await service.execute(job.run_id)
    assert service.get_analysis_status(job.run_id).status == "unsupported"


async def test_cutoff_and_revision(service, manifest):
    await service.register(manifest)
    docs = service.docs(manifest.issuer.ipo_id, CUTOFF)
    future = docs[0].model_copy(deep=True)
    future.document_id = "later"
    future.source.published_at = CUTOFF.replace(year=2025)
    assert select_documents(docs + [future], CUTOFF) == docs
    future.source.published_at = None
    assert select_documents([future], CUTOFF) == []
    future.source.published_at = CUTOFF
    future.source.publication_status = "declared"
    assert select_documents([future], CUTOFF) == []


def sub(scope, bids, **kw):
    data = dict(
        snapshot_id=scope + str(bids),
        ipo_id="test",
        observed_at=CUTOFF,
        published_at=CUTOFF,
        scope=scope,
        category="qib",
        bids=bids,
        offered=100,
        anchor_treatment="excluded",
        evidence_ids=["e"],
    )
    return Subscription(**(data | kw))


def test_subscription_deduplication():
    rows, _ = subscriptions_at([sub("nse", 300), sub("bse", 400), sub("consolidated", 500)], CUTOFF)
    assert len(rows) == 1 and rows[0]["multiple"] == "5"
    rows, warning = subscriptions_at([sub("nse", 300), sub("bse", 400)], CUTOFF)
    assert len(rows) == 2 and warning
    rows, _ = subscriptions_at(
        [sub("nse", 300, disjoint_exchange_scope=True), sub("bse", 400, disjoint_exchange_scope=True)], CUTOFF
    )
    assert len(rows) == 1 and rows[0]["multiple"] == "3.5"
    assert subscriptions_at([sub("nse", 1, published_at=CUTOFF.replace(year=2025))], CUTOFF)[0] == []
    conflicting, warnings = subscriptions_at([sub("consolidated", 10), sub("consolidated", 20)], CUTOFF)
    assert not conflicting and any("conflicting" in w for w in warnings)


async def test_material_interrupt_and_resume(service, manifest):
    manifest.issuer.board = "unknown"
    await service.register(manifest)
    job = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    await service.execute(job.run_id)
    assert service.get_analysis_status(job.run_id).status == "awaiting_decision"
    service.resume(job.run_id, {"board": "mainboard", "sector": "manufacturing"})
    await service.execute(job.run_id)
    assert service.get_analysis_report(job.run_id).status == "ok", service.get_analysis_status(job.run_id)
