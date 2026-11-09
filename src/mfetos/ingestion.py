"""Immutable PDF ingestion, conservative table extraction and temporal selection."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import date, datetime
from pathlib import Path

import pdfplumber

from .finance import convert, number
from .models import Document, DocumentInput, Evidence, Fact, Page, digest, now
from .storage import Store

FINANCIAL_LABELS = {
    "revenue from operations": "revenue",
    "revenue": "revenue",
    "restated profit for the period/ year": "pat",
    "profit for the year": "pat",
    "profit for the period": "pat",
    "profit after tax": "pat",
    "pat": "pat",
    "profit attributable to owners": "pat_owners",
    "ebitda": "ebitda",
    "ebit": "ebit",
    "finance costs": "interest",
    "depreciation and amortisation expense": "depreciation",
    "depreciation and amortization expense": "depreciation",
    "net cash generated from operating activities": "cfo",
    "net cash from operating activities": "cfo",
    "net cash used in operating activities": "cfo",
    "net cash flows from operating activities": "cfo",
    "net cash flow from operating activities": "cfo",
    "cfo": "cfo",
    "capital expenditure": "capex",
    "capex": "capex",
    "net worth": "net_worth",
    "total equity": "equity",
    "equity": "equity",
    "equity attributable to owners": "equity_owners",
    "total borrowings": "debt",
    "total debt": "debt",
    "cash and cash equivalents": "cash",
    "inventories": "inventory",
    "current trade receivables": "receivables",
    "total assets": "assets",
    "total current liabilities": "current_liabilities",
    "cost of goods sold": "cogs",
    "total trade payables": "payables",
    "other income": "other_income",
    "exceptional items": "exceptional_items",
    "capitalised costs": "capitalised_costs",
    "tax expense": "tax",
    "non-controlling interests": "nci",
    "lease liabilities": "lease_liabilities_component",
    "net cash inflow from operating activities": "cfo",
    "depreciation and amortisation expenses": "depreciation",
    "acquisition of property, plant and equipment and intangible assets (including capital advances given).": "capex",
    "equity attributable to owners of the company": "equity_owners",
    "lease liabilities total": "lease_liabilities_total",
    "preference capital": "preference_capital",
}
OFFER_LABELS = {
    "price low": ("price_low", "INR/share"),
    "price high": ("price_high", "INR/share"),
    "pre issue shares": ("pre_shares", "shares"),
    "fresh issue": ("fresh_proceeds", "INR"),
    "ofs shares": ("ofs_shares", "shares"),
    "issue expenses": ("issue_expenses", "INR"),
    "debt repayment": ("debt_repayment", "INR"),
    "other immediate uses": ("other_uses", "INR"),
    "incremental diluted shares": ("incremental_diluted_shares", "shares"),
    "lot size": ("lot_size", "shares"),
    "promoter ownership": ("promoter_ownership", "percent"),
}
OFFER_LABELS.update(
    {
        "listed shares": ("listed_shares", "shares"),
        "peer price": ("peer_price", "INR/share"),
        "issue size": ("issue_size", "INR"),
        "free float": ("free_float", "percent"),
        "anchor allocation": ("anchor_allocation", "INR"),
        "index five day return": ("index_return_5d", "percent"),
        "comparable listing return": ("comparable_listing_return", "percent"),
        "debt excludes leases": ("debt_excludes_leases", "flag"),
        "cash is unrestricted": ("cash_is_unrestricted", "flag"),
    }
)
SECTIONS = {
    "financial": [
        "summary financial information",
        "summary of financial information",
        "restated consolidated",
        "restated standalone",
        "statement of cash flows",
        "statement of profit",
        "balance sheet",
    ],
    "business": ["our business", "business overview", "risk factors", "industry overview"],
    "governance": ["outstanding litigation", "related party transactions", "our management", "auditor"],
    "offer": ["capital structure", "objects of the offer", "the offer", "offer structure", "price band"],
}
EXTRACTOR_VERSION = "1"


def eligible(doc: Document, cutoff: datetime) -> bool:
    s = doc.source
    return (
        s.published_at is not None
        and s.published_at <= cutoff
        and s.publication_status == "verified"
        and bool(s.publication_evidence)
    )


def select_documents(documents: list[Document], cutoff: datetime) -> list[Document]:
    # Preserve all eligible versions: fact selection handles field-level supersession.
    return sorted(
        [d for d in documents if eligible(d, cutoff)], key=lambda d: (d.source.published_at, d.document_id)
    )


def ocr_page(path: Path, index: int) -> str | None:
    try:
        import pypdfium2
        import pytesseract

        pdf = pypdfium2.PdfDocument(path)
        try:
            image = pdf[index].render(scale=2).to_pil()
            return pytesseract.image_to_string(image, timeout=45)
        finally:
            pdf.close()
    except (ImportError, RuntimeError, OSError):
        return None


def extract_pdf(path: Path) -> tuple[list[Page], list[str]]:
    pages, warnings = [], []
    section = "other"
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) > 1500:
            raise ValueError("PDF exceeds 1500-page limit")
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            method = "text"
            if len(text.strip()) < 30:
                recovered = ocr_page(path, i)
                text, method = (recovered, "ocr") if recovered else (text, "unavailable")
                if not recovered:
                    warnings.append(f"ocr_unavailable: PDF page {i + 1}")
            heading = text[:700].lower()
            for candidate, terms in SECTIONS.items():
                if any(t in heading for t in terms):
                    section = candidate
                    break
            # Run table geometry only where useful, avoiding hundreds of prose pages.
            tables = page.extract_tables() if section in {"financial", "offer"} else []
            label = re.search(r"(?:^|\n)\s*(\d{1,4}|[ivxlcdm]+)\s*$", text, re.I)
            pages.append(
                Page(
                    pdf_page=i + 1,
                    printed_page=label[1] if label else None,
                    text=text,
                    section=section,
                    tables=tables,
                    method=method,
                )
            )
            page.close()
    return pages, warnings


async def ingest(store: Store, ipo_id: str, source: DocumentInput, fetcher, settings=None) -> Document:
    from .config import Settings
    from .parsers import parse_pdf, parser_spec

    settings = settings or Settings(data_dir=store.root)
    spec = parser_spec(settings, source)
    original = store.root / "documents" / f"{source.expected_sha256}.pdf" if source.expected_sha256 else None
    if original and original.is_file():
        content = await asyncio.to_thread(original.read_bytes)
    elif source.location.startswith(("https://", "http://")):
        content = await fetcher.bytes(source.location)
    else:
        content = await asyncio.to_thread(Path(source.location).read_bytes)
    if len(content) > 120 * 1024 * 1024 or not content.startswith(b"%PDF"):
        raise ValueError("Expected a PDF smaller than 120 MiB")
    sha = hashlib.sha256(content).hexdigest()
    if source.expected_sha256 and sha != source.expected_sha256:
        raise ValueError("Document checksum mismatch")
    document_id = digest([ipo_id, sha, source.model_dump(mode="json")])[:32]
    extraction_id = digest([sha, spec])[:32]
    cached = store.load_document(document_id)
    if cached and cached.get("extraction_id") == extraction_id:
        return Document.model_validate(cached)
    path = store.root / "documents" / f"{sha}.pdf"
    if not path.exists():
        path.write_bytes(content)
    retrieval = store.load_blob(sha)
    if retrieval is None:
        retrieval = {"retrieved_at": now().isoformat()}
        store.save_blob(sha, retrieval)
    cached_pages = store.load_extraction(extraction_id)
    if cached_pages:
        pages = [Page.model_validate(p) for p in cached_pages["pages"]]
        warnings = cached_pages["warnings"]
    else:
        pages, warnings, raw = await asyncio.to_thread(parse_pdf, path, settings, spec)
        raw_path = None
        if raw is not None:
            import json

            directory = store.root / "extractions"
            directory.mkdir(exist_ok=True)
            raw_path = f"extractions/{extraction_id}.json"
            (store.root / raw_path).write_text(json.dumps(raw, ensure_ascii=False))
        store.save_extraction(
            extraction_id,
            {
                "sha256": sha,
                "backend": spec["backend"],
                "options": spec,
                "raw_artifact_path": raw_path,
                "pages": [p.model_dump() for p in pages],
                "warnings": warnings,
                "extractor_version": spec["version"],
            },
        )
    doc = Document(
        document_id=document_id,
        ipo_id=ipo_id,
        source=source,
        sha256=sha,
        retrieved_at=retrieval["retrieved_at"],
        pages=pages,
        warnings=warnings,
        extraction_id=extraction_id,
        parser_backend=spec["backend"],
        extractor_version=spec["version"],
    )
    store.save_document(doc)
    store.index_document(doc)
    return doc


def evidence_for(doc: Document, page: Page, snippet: str) -> Evidence:
    return Evidence(
        evidence_id=digest([doc.document_id, doc.extraction_id, page.pdf_page, snippet])[:32],
        ipo_id=doc.ipo_id,
        document_id=doc.document_id,
        source_url=doc.source.source_url,
        pdf_page=page.pdf_page,
        printed_page=page.printed_page,
        snippet=snippet,
        published_at=doc.source.published_at,
        retrieved_at=doc.retrieved_at,
    )


def fact_for(
    doc,
    page,
    metric,
    value,
    unit="INR",
    period=None,
    basis="not_applicable",
    period_kind="offer",
    snippet="",
    category="extracted_fact",
):
    ev = evidence_for(doc, page, snippet)
    fact = Fact(
        fact_id=digest([ev.evidence_id, metric, str(value), str(period), basis])[:32],
        ipo_id=doc.ipo_id,
        metric=metric,
        value=value,
        unit=unit,
        currency="INR" if unit.startswith("INR") else None,
        period_end=period,
        period_kind=period_kind,
        basis=basis,
        evidence_ids=[ev.evidence_id],
        category=category,
    )
    return fact, ev


def parse_date(text: str) -> date | None:
    text = " ".join(text.split())
    iso = re.search(r"(20\d{2})-(\d{2})-(\d{2})", text)
    if iso:
        return date(*map(int, iso.groups()))
    m = re.search(r"(March|June|September|December)\s+(\d{1,2}),?\s+(20\d{2})", text, re.I)
    if m:
        month = {"march": 3, "june": 6, "september": 9, "december": 12}[m[1].lower()]
        return date(int(m[3]), month, int(m[2]))
    return None


def clean_label(label: str) -> str:
    label = re.sub(r"^\([a-zivx]+\)\s*", "", label.strip(), flags=re.I)
    label = " ".join(label.lower().replace("\n", " ").split()).rstrip("*")
    return re.sub(r"^(n et|t otal|r estated)\b", lambda m: m[0].replace(" ", ""), label)


def extract_facts(doc: Document) -> tuple[list[Fact], list[Evidence]]:
    facts, evidence = [], []
    inherited_basis, basis_age = "unknown", 999
    for page in doc.pages:
        text = page.text
        low = text.lower()
        basis_age += 1
        if "standalone" in low[:900] and "consolidated" not in low[:900]:
            inherited_basis, basis_age = "standalone", 0
        elif "consolidated" in low[:1800]:
            inherited_basis, basis_age = "consolidated", 0
        basis = inherited_basis if basis_age <= 6 else "unknown"
        scale_match = re.search(r"(?:₹|rs\.?|inr)\s*(?:in\s+)?(millions?|lakhs?|crores?|billions?)", low)
        scale = scale_match[1] if scale_match else None
        # Explicit key/value inputs and conventional offer notices.
        for line in text.splitlines():
            match = re.fullmatch(
                r"([A-Za-z ]+):\s*([\d,().-]+)\s*(INR|shares|percent|million|crore|lakh)?", line.strip(), re.I
            )
            if match and match[1].lower().strip() in OFFER_LABELS:
                metric, unit = OFFER_LABELS[match[1].lower().strip()]
                val = number(match[2])
                if val is not None:
                    if unit == "INR" and match[3] and match[3].lower() in {"million", "crore", "lakh"}:
                        val = convert(val, match[3])
                    f, e = fact_for(doc, page, metric, val, unit, snippet=line)
                    facts.append(f)
                    evidence.append(e)
        band = re.search(r"PRICE BAND\s*:\s*[₹`]?\s*([\d,]+)\s*TO\s*[₹`]?\s*([\d,]+)", text, re.I)
        if band:
            for metric, raw in (("price_low", band[1]), ("price_high", band[2])):
                f, e = fact_for(doc, page, metric, number(raw), "INR/share", snippet=band[0])
                facts.append(f)
                evidence.append(e)
        if page.pdf_page <= 4:
            patterns = [
                ("lot_size", r"BIDS CAN BE MADE FOR A MINIMUM OF\s+([\d,]+)\s+EQUITY SHARES", "shares"),
                ("ofs_shares", r"OFFER FOR SALE OF UP TO\s+([\d,]+)\s+EQUITY SHARES", "shares"),
                ("cin", r"(?:IDENTITY NUMBER|CIN)\s*:\s*([LU][A-Z0-9]{20})", "identifier"),
            ]
            for metric_name, pattern, unit in patterns:
                found = re.search(pattern, text, re.I)
                if found:
                    val = found[1] if unit == "identifier" else number(found[1])
                    if val is not None:
                        f, e = fact_for(doc, page, metric_name, val, unit, snippet=found[0])
                        facts.append(f)
                        evidence.append(e)
            for verb, metric_name in (("OPENS", "open_date"), ("CLOSES", "close_date")):
                found = re.search(
                    r"BID\s*/\s*OFFER\s+" + verb + r"\s+ON\s*:\s*([A-Z]+,\s*[A-Z]+\s+\d{1,2},\s+20\d{2})",
                    text,
                    re.I,
                )
                if found:
                    f, e = fact_for(doc, page, metric_name, found[1], "date", snippet=found[0])
                    facts.append(f)
                    evidence.append(e)
        # General capital-structure tables provide a safer pattern than prose counts.
        if page.section == "offer" and "issued, subscribed" in low:
            issued_block = re.search(
                r"ISSUED, SUBSCRIBED AND PAID.UP SHARE CAPITAL BEFORE THE OFFER\s*\n([^\n]+)", text, re.I
            )
            pre = (
                re.search(r"([\d,]+)\s+Equity Shares of face value.*?each", issued_block[1], re.I)
                if issued_block
                else None
            )
            if pre:
                f, e = fact_for(doc, page, "pre_shares", number(pre[1]), "shares", snippet=pre[0])
                facts.append(f)
                evidence.append(e)
        if page.pdf_page <= 3:
            fresh = re.search(
                r"(?:aggregating\s+up\s+to|aggregating\s+to)\s*₹\s*([\d,]+)\s*million", text, re.I
            )
            if fresh and "fresh issue" in low:
                f, e = fact_for(
                    doc, page, "fresh_proceeds", convert(number(fresh[1]), "million"), snippet=fresh[0]
                )
                facts.append(f)
                evidence.append(e)
        if page.section != "financial" or basis == "unknown" or scale is None:
            continue
        # Auditor subsidiary summaries are not issuer financial statements.
        if "subsidiaries" in low[:1800] and "auditor" in low[:1800]:
            continue
        for table in page.tables:
            first = next(
                (i for i, row in enumerate(table) if row and clean_label(row[0] or "") in FINANCIAL_LABELS),
                None,
            )
            if first is None:
                continue
            headers = table[:first]
            width = max(map(len, table))
            dates = []
            for col in range(1, width):
                header = " ".join(row[col] or "" for row in headers if col < len(row))
                dt = parse_date(header)
                if dt:
                    dates.append((col, dt))
            attribution = None
            for row in table[first:]:
                if not row:
                    continue
                label = clean_label(row[0] or "")
                if "attributable to" in label:
                    attribution = (
                        "pat_owners" if "net profit" in label and "comprehensive" not in label else None
                    )
                metric_name = (
                    attribution
                    if label == "owners of the company" and attribution
                    else FINANCIAL_LABELS.get(label)
                )
                if metric_name is None:
                    continue
                # Include missing markers in positional mapping; never shift columns left.
                cells = [(i, v) for i, v in enumerate(row[1:], 1) if v is not None and v.strip()]
                if len(cells) != len(dates) or not dates:
                    continue
                for (_, raw), (_, dt) in zip(cells, dates):
                    val = number(raw)
                    if val is None:
                        continue
                    val = convert(val, scale)
                    pk = "annual" if (dt.month, dt.day) == (3, 31) else "interim"
                    snippet = f"{' | '.join(str(c or '') for c in row)}\nHeaders: {headers}\nScale: {scale}; basis: {basis}"
                    f, e = fact_for(
                        doc, page, metric_name, val, period=dt, basis=basis, period_kind=pk, snippet=snippet
                    )
                    if pk == "annual":
                        f.period_start = date(dt.year - 1, 4, 1)
                    elif dt.month == 6:
                        f.period_start = date(dt.year, 4, 1)
                    facts.append(f)
                    evidence.append(e)
    return list({f.fact_id: f for f in facts}.values()), list({e.evidence_id: e for e in evidence}.values())


def select_facts(
    facts: list[Fact], documents: list[Document], evidence: list[Evidence]
) -> tuple[list[Fact], list[str]]:
    evs = {e.evidence_id: e for e in evidence}
    docs = {d.document_id: d for d in documents}
    groups = {}
    for f in facts:
        key = (f.metric, f.period_end, f.period_kind, f.basis, f.status, f.category)
        groups.setdefault(key, []).append(f)
    selected, conflicts = [], []
    rank = {
        "drhp": 0,
        "rhp": 1,
        "annual_report": 1,
        "other": 1,
        "prospectus": 2,
        "price_band": 2,
        "amendment": 3,
    }
    for key, group in groups.items():

        def priority(f):
            d = docs[evs[f.evidence_ids[0]].document_id]
            amendment = d.source.kind == "amendment"
            effective_rank = (
                rank[d.source.kind] if not amendment or f.metric in d.source.amended_metrics else -1
            )
            return d.source.published_at, effective_rank

        group = [f for f in group if priority(f)[1] >= 0]
        if not group:
            continue
        group.sort(key=priority, reverse=True)
        best = group[0]
        tied = [f for f in group if priority(f) == priority(best)]
        if len({str(f.value) for f in tied}) > 1:
            best = best.model_copy(update={"validation": "conflicting"})
            conflicts.append(f"Conflicting values for {key}")
        selected.append(best)
    return selected, conflicts
