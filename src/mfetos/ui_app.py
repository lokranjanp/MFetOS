"""Local Streamlit interface. Widgets enqueue work; durable workers execute it."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from mfetos.config import Settings
from mfetos.models import DocumentInput, Issuer, Manifest
from mfetos.service import ResearchService
from mfetos.storage import MigrationRequired

INDIA = ZoneInfo("Asia/Kolkata")
ACTIVE = {"queued", "running"}


@st.cache_resource
def service_for(data_dir: str):
    return ResearchService(Settings(data_dir=Path(data_dir)))


def persist_upload(root: Path, content: bytes) -> tuple[Path, str]:
    if not content.startswith(b"%PDF") or len(content) > 120 * 1024 * 1024:
        raise ValueError("Upload a PDF smaller than 120 MiB")
    sha = hashlib.sha256(content).hexdigest()
    directory = root / "incoming"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha}.pdf"
    if not path.exists():
        path.write_bytes(content)
    return path.resolve(), sha


def upload_form(service):
    with st.expander("Add an IPO and documents"):
        legal_name = st.text_input("Legal company name")
        ipo_id = st.text_input(
            "IPO identifier", help="Stable ID such as waaree-2024; letters, numbers, hyphens and underscores"
        )
        cin = st.text_input("CIN (optional)")
        c1, c2 = st.columns(2)
        board = c1.selectbox("Board", ["mainboard", "sme", "unknown"])
        sector = c2.selectbox(
            "Sector",
            [
                "manufacturing",
                "technology",
                "consumer",
                "healthcare",
                "energy",
                "logistics",
                "chemicals",
                "financial",
                "unknown",
            ],
        )
        backend = st.selectbox(
            "PDF parser",
            ["pdfplumber", "docling"],
            index=["pdfplumber", "docling"].index(service.settings.pdf_parser),
            help="Docling requires the optional package and prepared local models",
        )
        files = st.file_uploader("Upload prospectuses or notices", type="pdf", accept_multiple_files=True)
        urls = st.text_area("Public PDF URLs (one per line)")
        items = [(f"upload:{i}", file.name, file) for i, file in enumerate(files or [])]
        items += [(f"url:{i}", url, None) for i, url in enumerate(urls.splitlines()) if url.strip()]
        metadata = []
        for key, label, file in items:
            st.text(label)
            kind = st.selectbox(
                "Document type",
                ["rhp", "drhp", "price_band", "amendment", "prospectus", "annual_report", "other"],
                key=key + "kind",
            )
            published = st.date_input(
                "Public availability date (India; optional until verified)", value=None, key=key + "date"
            )
            proof = st.text_input("Publication evidence URL or note", key=key + "proof")
            verified = st.checkbox("I have verified public availability on this date", key=key + "verified")
            metadata.append((label.strip(), file, kind, published, proof, verified))
        st.caption(
            "Unknown publication dates stay unknown. Ingestion does not verify historical availability for you."
        )
        if st.button("Ingest documents", disabled=not items):
            try:
                docs = []
                for label, file, kind, published, proof, verified in metadata:
                    if verified and (published is None or not proof.strip()):
                        raise ValueError("A verified publication needs a date and evidence")
                    if file is not None:
                        path, sha = persist_upload(service.store.root, file.getvalue())
                        location, source_url = str(path), "upload://" + sha
                    else:
                        location, source_url, sha = label, label, None
                        if not label.startswith("https://"):
                            raise ValueError("Document URLs must use HTTPS")
                    docs.append(
                        DocumentInput(
                            location=location,
                            source_url=source_url,
                            kind=kind,
                            expected_sha256=sha,
                            parser_backend=backend,
                            published_at=datetime.combine(published, time(23, 59, 59), INDIA)
                            if published
                            else None,
                            publication_evidence=proof or None,
                            publication_status="verified" if verified else "declared",
                        )
                    )
                manifest = Manifest(
                    issuer=Issuer(
                        ipo_id=ipo_id,
                        legal_name=legal_name,
                        cin=cin or None,
                        board=board,
                        sector=sector,
                        status="upcoming",
                    ),
                    documents=docs,
                )
                if not legal_name.strip():
                    raise ValueError("Enter the company's legal name")
                job = service.start_ingestion(manifest)
                st.session_state["ingestion_id"] = job["id"]
                st.success("Documents queued. The worker will ingest them in the background.")
            except ValueError:
                st.error("Check the IPO identifier, company name, publication evidence and PDF/URL inputs.")


def show_report(service, run_id):
    result = service.get_analysis_report(run_id)
    if result.status != "ok":
        return
    report = result.data["report"]
    st.subheader(report["issuer"])
    st.caption("Analysis cutoff: " + report["as_of"])
    a, b = st.columns(2)
    for column, label, field in ((a, "Long-term assessment", "long_term"), (b, "Listing outlook", "listing")):
        column.markdown(f"**{label} — {report[field]['status'].replace('_', ' ')}**")
        column.write(report[field]["assessment"])
        for reason in report[field]["reasons"]:
            column.caption(reason)
    st.download_button(
        "Download JSON", json.dumps(report, indent=2), file_name=run_id + ".json", mime="application/json"
    )
    st.download_button(
        "Download Markdown", result.data["markdown"], file_name=run_id + ".md", mime="text/markdown"
    )
    for name, branch in report["branches"].items():
        with st.expander(name.replace("_", " ").title()):
            if branch["metrics"]:
                st.dataframe(branch["metrics"], hide_index=True)
            for claim in branch["claims"]:
                st.text(claim["kind"] + ": " + claim["text"])
            for gap in branch["gaps"]:
                st.caption(gap)
    with st.expander("Facts, evidence gaps and conflicts"):
        st.dataframe(report["facts"], hide_index=True)
        for gap in report["missing_data"] + report["conflicts"]:
            st.write(gap)
    evidence = report["evidence"]
    if evidence:
        chosen = st.selectbox(
            "Inspect evidence",
            range(len(evidence)),
            format_func=lambda i: f"{evidence[i]['document_id'][:10]} — PDF page {evidence[i]['pdf_page']}",
        )
        e = evidence[chosen]
        st.text(e["snippet"])
        st.caption(
            f"PDF page {e['pdf_page']}; printed page {e['printed_page'] or 'unknown'}. Published {e['published_at']}; retrieved {e['retrieved_at']}"
        )
        st.text(e["source_url"])
        doc = service.store.load_document(e["document_id"])
        path = service.store.root / "documents" / (doc["sha256"] + ".pdf") if doc else None
        if path and path.is_file():
            st.download_button(
                "Download original PDF",
                path.read_bytes(),
                file_name=doc["sha256"] + ".pdf",
                mime="application/pdf",
            )


def run_panel(service, run_id):
    run = service.get_analysis_status(run_id)
    st.write(f"Status: **{run.status}** · Stage: {run.stage}")
    if run.trace_url:
        st.link_button("Open LangSmith trace", run.trace_url)
    if run.error:
        st.error(run.error)
    if run.status == "awaiting_decision":
        st.write(run.decision)
        if run.decision and "board" in run.decision.get("required", []):
            board = st.selectbox("Resolved board", ["mainboard", "sme"])
            sector = st.text_input("Resolved sector", "manufacturing")
            if st.button("Resolve and resume"):
                service.resume(run_id, {"board": board, "sector": sector})
                st.rerun()
        elif st.checkbox("I reviewed and accept the issuer identity") and st.button(
            "Confirm identity and resume"
        ):
            service.resume(run_id, {"accept_identity": True})
            st.rerun()
    if run.status == "failed" and st.button("Retry analysis"):
        service.retry(run_id)
        st.rerun()
    show_report(service, run_id)
    with st.expander("Activity timeline"):
        st.dataframe(service.activity(run_id), hide_index=True)


def main():
    st.set_page_config(page_title="MFetOS IPO research", page_icon="📄", layout="wide")
    st.title("Indian IPO research")
    st.caption("Evidence-backed research · separate long-term and listing assessments")
    settings = Settings()
    try:
        service = service_for(str(settings.data_dir.resolve()))
    except MigrationRequired:
        st.error(
            "This research database needs migration. Stop workers, then run: mfetos --data-dir PATH db migrate"
        )
        st.stop()
    st.sidebar.caption("Local data: " + str(service.store.root))
    st.sidebar.caption(
        "LangSmith: metadata only" if service.observer.enabled else "LangSmith: off / not configured"
    )
    st.sidebar.caption("Launch with mfetos ui to start a supervised worker.")
    research, runs, data = st.tabs(["Research", "Runs", "Data"])
    with research:
        upload_form(service)
        ingestion_id = st.session_state.get("ingestion_id")
        if ingestion_id:

            @st.fragment(run_every="2s")
            def ingestion_progress():
                job = service.get_ingestion_status(ingestion_id)
                st.write(
                    f"Ingestion: {job['status']} ({job['completed_documents']}/{job['total_documents']} documents)"
                )
                if job["error_code"]:
                    st.error("Ingestion failed: " + job["error_code"])
                if job["result_json"]:
                    result = json.loads(job["result_json"])
                    for warning in result.get("warnings", []):
                        st.warning(warning)
                if job["status"] in {"failed", "partial"} and st.button("Retry ingestion"):
                    service.retry_ingestion(ingestion_id)
                if job["status"] == "completed":
                    st.success("Documents ready. Refresh the IPO list to select the issue.")

            ingestion_progress()
        st.button("Refresh IPO list", key="refresh_ipos")
        issuers = service.search_ipos().issuers
        if not issuers:
            st.info("No IPOs registered yet. Add PDFs or public document URLs above.")
        else:
            ipo_id = st.selectbox(
                "IPO",
                [i.ipo_id for i in issuers],
                format_func=lambda i: next(x.legal_name for x in issuers if x.ipo_id == i),
            )
            day = st.date_input("Analysis cutoff date (India)", datetime.now(INDIA).date())
            clock = st.time_input("Analysis cutoff time (India)", time(23, 59, 59))
            objective = st.selectbox("Research objective", ["both", "long_term", "listing"])
            if st.button("Start analysis"):
                run = service.start_ipo_analysis(ipo_id, objective, datetime.combine(day, clock, INDIA))
                st.session_state["selected_run"] = run.run_id
            run_id = st.session_state.get("selected_run")
            if run_id:

                @st.fragment(run_every="2s" if service.get_analysis_status(run_id).status in ACTIVE else None)
                def active_run():
                    run_panel(service, run_id)

                active_run()
    with runs:
        imports = service.store.browse("ingestion_jobs", limit=50)["rows"]
        if imports:
            st.subheader("Document imports")
            st.dataframe(
                [
                    {
                        k: row[k]
                        for k in (
                            "id",
                            "ipo_id",
                            "status",
                            "completed_documents",
                            "total_documents",
                            "updated_at",
                        )
                    }
                    for row in imports
                ],
                hide_index=True,
            )
            import_id = st.selectbox("Open an import", [row["id"] for row in imports])
            if st.button("Inspect import status"):
                st.session_state["ingestion_id"] = import_id
                st.rerun()
        history = service.list_runs()
        if history:
            st.dataframe(history, hide_index=True)
            selected = st.selectbox("Open a saved run", [r["run_id"] for r in history])
            if st.button("View selected run"):
                st.session_state["selected_run"] = selected
                st.rerun()
        else:
            st.info("No analysis runs yet.")
    with data:
        table = st.selectbox(
            "Table or view",
            service.store.table_names(),
            index=service.store.table_names().index("financial_facts"),
        )
        ipo_filter = st.text_input("Filter IPO ID (where applicable)")
        run_filter = st.text_input("Filter run ID (where applicable)")
        page = st.number_input("Page", min_value=1, value=1, step=1)
        result = service.browse_data(
            table, limit=50, offset=(page - 1) * 50, ipo_id=ipo_filter or None, run_id=run_filter or None
        )
        st.caption(f"{result['total']} rows · read only · 50 rows per page")
        st.dataframe(result["rows"], hide_index=True)


if __name__ == "__main__":
    main()
