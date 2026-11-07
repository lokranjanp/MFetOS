from decimal import Decimal as D

from conftest import CUTOFF

from mfetos.finance import adjust_share_count, normalised_earnings, shares_from_tranches
from mfetos.ingestion import fact_for, select_facts
from mfetos.service import ResearchService


def test_explicit_adjustments_and_employee_tranche():
    assert adjust_share_count(D(100), D(2), D(".5")) == 300
    assert normalised_earnings(D(100), [D(-10), D(4)]) == 94
    assert normalised_earnings(D(100), [None]) is None
    assert shares_from_tranches(D(100), D(10), [(D(100), D(0)), (D(50), D(5))], D(2)) == 122


async def test_amendment_only_updates_named_fields(service, manifest):
    await service.register(manifest)
    doc = service.docs(manifest.issuer.ipo_id, CUTOFF)[0]
    page = doc.pages[0]
    old, old_e = fact_for(doc, page, "price_high", D(120), snippet="Price high: 120 INR")
    amendment = doc.model_copy(deep=True)
    amendment.document_id = "amendment"
    amendment.source.kind = "amendment"
    amendment.source.published_at = CUTOFF
    amendment.source.amended_metrics = ["price_high"]
    newer, newer_e = fact_for(amendment, page, "price_high", D(125), snippet="Price high: 125 INR")
    unrelated, unrelated_e = fact_for(amendment, page, "pre_shares", D(999), snippet="Unaffected field")
    selected, conflicts = select_facts(
        [old, newer, unrelated], [doc, amendment], [old_e, newer_e, unrelated_e]
    )
    assert len(selected) == 1 and selected[0].value == 125
    assert not conflicts
    duplicate, duplicate_e = fact_for(doc, page, "price_high", D(126), snippet="Different same-date value")
    selected, conflicts = select_facts([old, duplicate], [doc], [old_e, duplicate_e])
    assert conflicts and selected[0].validation == "conflicting"


async def test_checkpoint_recovers_without_rerunning_completed_branches(service, manifest, monkeypatch):
    await service.register(manifest)
    job = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    # Fail only the final report write, after all four branch results were checkpointed.
    original = service.store.put

    def fail_report(kind, *args, **kwargs):
        if kind == "report":
            raise RuntimeError("simulated storage failure")
        return original(kind, *args, **kwargs)

    monkeypatch.setattr(service.store, "put", fail_report)
    await service.execute(job.run_id)
    assert service.get_analysis_status(job.run_id).status == "failed"
    fresh = ResearchService(service.settings)
    fresh.retry(job.run_id)
    await fresh.execute(job.run_id)
    result = fresh.get_analysis_report(job.run_id)
    assert result.status == "ok", fresh.get_analysis_status(job.run_id)
    assert len(result.data["report"]["branches"]) == 4


async def test_subscription_mapping_requires_matching_original_line(service, manifest, tmp_path):
    import json

    from conftest import FIXTURE, make_pdf

    from mfetos.models import SubscriptionSource

    raw = json.loads(FIXTURE.read_text())
    quote = "qib scope consolidated bids 300 offered 100 shares anchors excluded observed 2024-10-17T12:00:00+05:30"
    raw["offer_lines"].append(quote)
    pdf = tmp_path / "snapshot.pdf"
    make_pdf(pdf, raw)
    manifest.documents[0].location = str(pdf)
    manifest.documents[0].published_at = CUTOFF
    manifest.subscription_sources = [
        SubscriptionSource(
            source_url=manifest.documents[0].source_url,
            pdf_page=1,
            quote=quote,
            observed_at="2024-10-17T12:00:00+05:30",
            scope="consolidated",
            category="qib",
            bids=300,
            offered=100,
            anchor_treatment="excluded",
        )
    ]
    result = await service.register(manifest)
    assert not result.warnings, result
    snapshot = service.get_subscription_snapshot(manifest.issuer.ipo_id, CUTOFF)
    assert snapshot.status == "ok" and snapshot.data[0]["multiple"] == "3"
    manifest.subscription_sources[0].bids = 999
    result = await service.register(manifest)
    assert any("rejected" in warning for warning in result.warnings)
