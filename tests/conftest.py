import json
from datetime import datetime
from pathlib import Path

import pytest
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from mfetos.config import Settings
from mfetos.models import DocumentInput, Manifest
from mfetos.service import ResearchService

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_prospectus.json"
CUTOFF = datetime.fromisoformat("2024-10-17T23:59:59+05:30")


@pytest.fixture(autouse=True)
def offline_tracing(monkeypatch):
    # A developer's .env must not make this offline suite contact LangSmith.
    # Exporter tests explicitly override this setting and use an in-memory client.
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


def make_pdf(path, fixture=None):
    data = fixture or json.loads(FIXTURE.read_text())
    styles = getSampleStyleSheet()
    story = []

    def lines(items):
        for text in items:
            story.append(Paragraph(text, styles["Normal"]))
            story.append(Spacer(1, 5))

    lines(data["offer_lines"])
    story.append(PageBreak())
    lines(["SUMMARY FINANCIAL INFORMATION", "Restated Consolidated Summary Statements", "(INR million)"])
    table = Table(
        [data["financial_headers"]] + data["financial_rows"], colWidths=[215, 65, 65, 65, 65], repeatRows=1
    )
    table.setStyle(
        TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 8)])
    )
    story.append(table)
    story.append(PageBreak())
    lines(data["business_lines"])
    story.append(PageBreak())
    lines(data["governance_lines"])
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20, rightMargin=20).build(story)
    return data


@pytest.fixture
def service(tmp_path):
    return ResearchService(Settings(data_dir=tmp_path / "data", max_retrieval_rounds=1, worker_concurrency=1))


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "synthetic.pdf"
    data = make_pdf(path)
    return Manifest(
        issuer=data["issuer"],
        documents=[
            DocumentInput(
                location=str(path),
                source_url="https://example.org/synthetic-prospectus.pdf",
                kind="rhp",
                published_at="2024-10-14T23:59:59+05:30",
                publication_status="verified",
                publication_evidence="Synthetic fixture publication event; not a real-world assertion",
            )
        ],
        scenario_multiples={"bear": 8, "base": 10, "bull": 12},
        scenario_rationale="Synthetic analyst sensitivity only; not observed peer multiples",
    )
