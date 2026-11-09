import os
from pathlib import Path

import pytest
from conftest import CUTOFF


@pytest.mark.skipif(os.environ.get("MFETOS_DOCLING_LIVE") != "1", reason="opt-in local Docling model test")
async def test_local_docling_pdf_to_report(service, manifest):
    service.settings.docling_artifacts_path = Path("data/models/docling").resolve()
    manifest.documents[0].parser_backend = "docling"
    result = await service.register(manifest)
    assert result.status == "ok", result
    docs = service.docs(manifest.issuer.ipo_id, CUTOFF)
    assert docs and docs[0].parser_backend == "docling"
    assert any(p.tables for p in docs[0].pages)
    assert any(p.provenance for p in docs[0].pages)
    facts = service.get_ipo_facts(manifest.issuer.ipo_id, CUTOFF)
    assert any(f["metric"] == "revenue" and f["value"] == "125000000" for f in facts.data["facts"])
    job = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    await service.execute(job.run_id)
    assert service.get_analysis_report(job.run_id).status == "ok", service.get_analysis_status(job.run_id)


@pytest.mark.skipif(os.environ.get("MFETOS_DOCLING_LIVE") != "1", reason="opt-in local Docling OCR test")
def test_local_docling_scanned_page(service, tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    from mfetos.parsers import parse_docling

    image = Image.new("RGB", (1600, 800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=44)
    draw.text((60, 80), "RISK FACTORS", fill="black", font=font)
    draw.text((60, 180), "Synthetic industrial customer concentration", fill="black", font=font)
    pdf = tmp_path / "scanned.pdf"
    image.save(pdf, "PDF", resolution=150)
    service.settings.docling_artifacts_path = Path("data/models/docling").resolve()
    pages, warnings, raw = parse_docling(pdf, service.settings)
    assert pages and "concentration" in " ".join(p.text.lower() for p in pages)
    assert raw and not warnings
