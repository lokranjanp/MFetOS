"""Local PDF parser adapters with versioned options and original-page provenance."""

from __future__ import annotations

import importlib.metadata
import json
import threading
from pathlib import Path

from .models import Page


class ParserUnavailable(ValueError):
    pass


DOCLING_LOCK = threading.Lock()


def parser_spec(settings, source=None):
    backend = (source.parser_backend if source else None) or settings.pdf_parser
    package = "docling" if backend == "docling" else "pdfplumber"
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        version = "unavailable"
    return {
        "backend": backend,
        "version": version,
        "adapter_version": "2",
        "ocr": "rapidocr" if backend == "docling" else "tesseract_fallback",
        "table_structure": True,
        "timeout": settings.docling_timeout if backend == "docling" else 600,
    }


def parse_pdf(path: Path, settings, spec):
    if spec["backend"] == "pdfplumber":
        from .ingestion import extract_pdf

        pages, warnings = extract_pdf(path)
        return pages, warnings, None
    return parse_docling(path, settings)


def parse_docling(path: Path, settings):
    # Shared offline-loader environment and heavyweight CPU models must not race.
    with DOCLING_LOCK:
        return _parse_docling(path, settings)


def _parse_docling(path: Path, settings):
    import importlib.util

    if importlib.util.find_spec("docling") is None:
        raise ParserUnavailable("Docling is not installed: install mfetos[docling]")
    artifacts = settings.docling_artifacts_path or settings.data_dir / "models" / "docling"
    if not (artifacts / "mfetos-models-ready.json").exists():
        raise ParserUnavailable("Docling models missing: run mfetos parsers prepare")
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise ParserUnavailable("Docling is not installed: install mfetos[docling]") from exc
    # Offline means offline, including HuggingFace/ocr model loaders. No implicit downloads.
    import os

    old_hf = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        options = PdfPipelineOptions(
            artifacts_path=artifacts,
            do_ocr=True,
            do_table_structure=True,
            document_timeout=settings.docling_timeout,
        )
        options.ocr_options = RapidOcrOptions()
        options.enable_remote_services = False
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        result = converter.convert(path, max_num_pages=1500, max_file_size=120 * 1024 * 1024)
        if getattr(result.status, "value", str(result.status)).lower() != "success":
            raise ParserUnavailable("Docling conversion incomplete; no partial financial extraction accepted")
        raw = result.document.export_to_dict()
        pages = normalise_docling(raw)
        return pages, [], raw
    finally:
        if old_hf is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = old_hf


def normalise_docling(raw: dict) -> list[Page]:
    """Keep sparse merged-cell grids, not flattened DataFrame headers."""
    by_page = {}
    for key, info in raw.get("pages", {}).items():
        n = int(info.get("page_no", key))
        by_page[n] = Page(pdf_page=n, text="", method="docling")
    section = "other"
    # Docling's body preserves reading order through references to texts/tables.
    refs = raw.get("body", {}).get("children", [])
    objects = {f"#/texts/{i}": item for i, item in enumerate(raw.get("texts", []))}
    objects.update({f"#/tables/{i}": item for i, item in enumerate(raw.get("tables", []))})
    objects.update({f"#/groups/{i}": item for i, item in enumerate(raw.get("groups", []))})
    ordered, visited = [], set()

    def visit(ref):
        key = ref.get("$ref")
        if key in visited or key not in objects:
            return
        visited.add(key)
        item = objects[key]
        if "prov" in item:
            ordered.append(item)
        for child in item.get("children", []):
            visit(child)

    for ref in refs:
        visit(ref)
    seen = set()
    # Nested groups may omit objects from direct children. Include their provenance once.
    ordered += [o for o in objects.values() if id(o) not in {id(v) for v in ordered}]
    from .ingestion import SECTIONS

    for item in ordered:
        if id(item) in seen:
            continue
        seen.add(id(item))
        text = item.get("text", "")
        if item.get("label") in {"section_header", "title"}:
            for name, terms in SECTIONS.items():
                if any(t in text.lower() for t in terms):
                    section = name
                    break
        prov = item.get("prov", [])
        for p in prov:
            n = int(p["page_no"])
            page = by_page.setdefault(n, Page(pdf_page=n, text="", method="docling"))
            page.section = section
            page.provenance.append(
                {
                    "label": item.get("label"),
                    "page_no": n,
                    "bbox": p.get("bbox"),
                    "charspan": p.get("charspan"),
                    "table_cells": item.get("data", {}).get("table_cells")
                    if item.get("label") == "table"
                    else None,
                }
            )
            if text:
                page.text += text + "\n"
            if item.get("label") == "page_footer" and text.strip().isdigit():
                page.printed_page = text.strip()
        data = item.get("data")
        if data and prov:
            grid = [[None] * data["num_cols"] for _ in range(data["num_rows"])]
            for cell in data.get("table_cells", []):
                r, c = cell["start_row_offset_idx"], cell["start_col_offset_idx"]
                if 0 <= r < len(grid) and 0 <= c < len(grid[r]):
                    grid[r][c] = cell.get("text", "")
            # Do not duplicate a spanning table's financial rows on each page.
            page = by_page[int(prov[0]["page_no"])]
            page.tables.append(grid)
            page.text += "\n".join(" | ".join(c or "" for c in row) for row in grid) + "\n"
    return [by_page[n] for n in sorted(by_page)]


def prepare_models(settings):
    try:
        from docling.utils.model_downloader import download_models
    except ImportError as exc:
        raise ParserUnavailable("Install mfetos[docling] before preparing models") from exc
    directory = settings.docling_artifacts_path or settings.data_dir / "models" / "docling"
    directory.mkdir(parents=True, exist_ok=True)
    # Explicit user-invoked download only. Restrict to layout/table/OCR, no VLM/audio models.
    download_models(
        output_dir=directory,
        with_layout=True,
        with_tableformer=True,
        with_rapidocr=True,
        with_code_formula=False,
        with_picture_classifier=False,
        progress=True,
    )
    (directory / "mfetos-models-ready.json").write_text(
        json.dumps(
            {
                "backend": "docling",
                "options": parser_spec(settings.model_copy(update={"pdf_parser": "docling"})),
            }
        )
    )
    return {"status": "ready", "directory": str(directory)}
