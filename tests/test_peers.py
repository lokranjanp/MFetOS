import json

from conftest import CUTOFF, FIXTURE, make_pdf

from mfetos.models import DocumentInput, Manifest, Report


async def test_peer_comparison_preserves_issuer_and_evidence(service, manifest, tmp_path):
    source = json.loads(FIXTURE.read_text())
    source["offer_lines"] = [
        s.replace("Fixture Industrial Limited", "Fixture Peer Limited").replace(
            "U12345MH2000PLC000001", "U12345MH2000PLC000002"
        )
        for s in source["offer_lines"]
    ]
    source["offer_lines"].extend(["Peer price: 140 INR", "Listed shares: 1000000 shares"])
    pdf = tmp_path / "peer.pdf"
    make_pdf(pdf, source)
    peer = Manifest(
        issuer=manifest.issuer.model_copy(
            update={
                "ipo_id": "fixture-peer",
                "legal_name": "Fixture Peer Limited",
                "cin": "U12345MH2000PLC000002",
                "status": "listed",
            }
        ),
        documents=[
            DocumentInput(
                location=str(pdf),
                source_url="https://example.org/peer.pdf",
                kind="annual_report",
                published_at="2024-10-16T23:59:59+05:30",
                publication_status="verified",
                publication_evidence="Synthetic publication event",
            )
        ],
    )
    await service.register(peer)
    manifest.peer_ids = [peer.issuer.ipo_id]
    manifest.peer_rationales = {
        peer.issuer.ipo_id: "Synthetic same business mix, growth, margins, size and capital intensity"
    }
    await service.register(manifest)
    job = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    await service.execute(job.run_id)
    result = service.get_analysis_report(job.run_id)
    assert result.status == "ok", service.get_analysis_status(job.run_id)
    report = Report.model_validate(result.data["report"])
    assert {f.ipo_id for f in report.facts} == {manifest.issuer.ipo_id, peer.issuer.ipo_id}
    metrics = {m.name: m for m in report.branches["peers_valuation"].metrics}
    assert metrics["peer:fixture-peer:pe"].value == 10
    peer_ids = metrics["peer:fixture-peer:pe"].input_ids
    facts = {f.fact_id: f for f in report.facts}
    assert all(facts[i].ipo_id == peer.issuer.ipo_id for i in peer_ids)
