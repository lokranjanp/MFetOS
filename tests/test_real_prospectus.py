import os
from pathlib import Path

import pytest
from conftest import CUTOFF

from mfetos.models import Manifest, Report


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("MFETOS_LIVE_TESTS") != "1", reason="opt-in real prospectus ingestion")
async def test_real_waaree_ingestion_to_report(service):
    path = Path(__file__).parents[1] / "examples" / "waaree-2024" / "manifest.json"
    manifest = Manifest.model_validate_json(path.read_text())
    for doc, name in zip(manifest.documents, ("RHP", "PRICE_BAND", "AMENDMENT")):
        if value := os.environ.get(f"MFETOS_WAAREE_{name}"):
            doc.location = value
    registration = await service.register(manifest)
    assert len(registration.data["document_ids"]) == 3, registration
    job = service.start_ipo_analysis("waaree-2024", as_of=CUTOFF)
    await service.execute(job.run_id)
    result = service.get_analysis_report(job.run_id)
    assert result.status == "ok", service.get_analysis_status(job.run_id)
    report = Report.model_validate(result.data["report"])
    assert any(
        f.metric == "revenue" and f.value == 113976090000 and str(f.period_end) == "2024-03-31"
        for f in report.facts
    )
    assert any(f.metric == "price_low" and f.value == 1427 for f in report.facts)
    assert any(f.metric == "price_high" and f.value == 1503 for f in report.facts)
    assert any(f.metric == "pre_shares" and f.value == 263331104 for f in report.facts)
    assert all(e.published_at <= CUTOFF for e in report.evidence)
    assert report.listing.status == "insufficient_evidence"
