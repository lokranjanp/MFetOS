from mfetos.parsers import normalise_docling, parser_spec


def test_docling_sparse_tables_and_provenance():
    raw = {
        "pages": {"1": {"page_no": 1}},
        "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/tables/0"}]},
        "texts": [
            {
                "label": "section_header",
                "text": "SUMMARY FINANCIAL INFORMATION",
                "prov": [{"page_no": 1, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4}}],
            }
        ],
        "tables": [
            {
                "label": "table",
                "prov": [{"page_no": 1}],
                "data": {
                    "num_rows": 2,
                    "num_cols": 3,
                    "table_cells": [
                        {
                            "start_row_offset_idx": 0,
                            "start_col_offset_idx": 0,
                            "text": "Particulars",
                            "end_col_offset_idx": 2,
                        },
                        {"start_row_offset_idx": 1, "start_col_offset_idx": 0, "text": "Revenue"},
                        {"start_row_offset_idx": 1, "start_col_offset_idx": 2, "text": "100"},
                    ],
                },
            }
        ],
    }
    page = normalise_docling(raw)[0]
    assert page.pdf_page == 1 and page.method == "docling"
    assert page.tables[0] == [["Particulars", None, None], ["Revenue", None, "100"]]
    assert page.provenance[0]["bbox"]["l"] == 1


async def test_selected_docling_missing_models_is_explicit(service, manifest):
    manifest.documents[0].parser_backend = "docling"
    result = await service.register(manifest)
    assert result.status == "partial" and not result.data["document_ids"]
    assert any("models" in e.lower() or "not installed" in e.lower() for e in result.warnings)
    assert parser_spec(service.settings)["backend"] == "pdfplumber"


async def test_reparsing_preserves_frozen_pages_and_evidence(service, manifest):
    from conftest import CUTOFF

    from mfetos.ingestion import evidence_for
    from mfetos.models import Document

    await service.register(manifest)
    run = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    original = service.docs(manifest.issuer.ipo_id, CUTOFF)[0]
    old_evidence = evidence_for(original, original.pages[0], "same source quote")
    service.store.save_evidence(old_evidence)
    revised = original.model_copy(deep=True)
    revised.extraction_id = "revised-parser-extraction"
    revised.extractor_version = "new-parser-version"
    revised.pages[0].printed_page = "revised-page-label"
    revised.warnings = ["new parser warning"]
    service.store.save_document(revised)
    new_evidence = evidence_for(revised, revised.pages[0], "same source quote")
    service.store.save_evidence(new_evidence)
    assert old_evidence.evidence_id != new_evidence.evidence_id
    frozen_id = service.store.load_run_inputs(run.run_id)["extraction_ids"][original.document_id]
    frozen = Document.model_validate(service.store.load_document(original.document_id, frozen_id))
    assert frozen.pages == original.pages
    assert frozen.warnings == original.warnings
    assert frozen.extractor_version == original.extractor_version
    assert service.get_evidence(old_evidence.evidence_id) == old_evidence
