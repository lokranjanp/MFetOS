from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from conftest import CUTOFF
from streamlit.testing.v1 import AppTest


def app_path():
    return str(Path(__file__).parents[1] / "src" / "mfetos" / "ui_app.py")


def test_empty_ui_and_data_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("MFETOS_DATA_DIR", str(tmp_path / "ui"))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    app = AppTest.from_file(app_path(), default_timeout=20).run()
    assert not app.exception
    assert [t.label for t in app.tabs] == ["Research", "Runs", "Data"]
    assert any("No IPOs" in item.value for item in app.info)
    assert any(s.label == "Table or view" for s in app.selectbox)


async def test_saved_report_and_rerun_do_not_duplicate_jobs(service, manifest, monkeypatch):
    await service.register(manifest)
    job = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    await service.execute(job.run_id)
    monkeypatch.setenv("MFETOS_DATA_DIR", str(service.store.root))
    app = AppTest.from_file(app_path(), default_timeout=20)
    app.session_state["selected_run"] = job.run_id
    app.run()
    assert not app.exception
    assert any("Long-term assessment" in m.value for m in app.markdown)
    assert any("Listing outlook" in m.value for m in app.markdown)
    before = service.store.browse("jobs")["total"]
    app.run()
    assert not app.exception
    assert service.store.browse("jobs")["total"] == before


def test_ui_queues_url_and_rejects_false_publication_attestation(tmp_path, monkeypatch):
    from mfetos.storage import Store

    root = tmp_path / "queue-ui"
    monkeypatch.setenv("MFETOS_DATA_DIR", str(root))
    app = AppTest.from_file(app_path(), default_timeout=20).run()
    next(w for w in app.text_input if w.label == "Legal company name").set_value("UI Fixture Limited")
    next(w for w in app.text_input if w.label == "IPO identifier").set_value("ui-fixture")
    next(w for w in app.text_area if w.label.startswith("Public PDF URLs")).set_value(
        "https://example.org/rhp.pdf"
    )
    app.run()
    next(w for w in app.checkbox if w.label.startswith("I have verified")).check()
    next(w for w in app.button if w.label == "Ingest documents").click()
    app.run()
    assert app.error and Store(root).browse("jobs")["total"] == 0
    next(w for w in app.checkbox if w.label.startswith("I have verified")).uncheck()
    next(w for w in app.button if w.label == "Ingest documents").click()
    app.run()
    assert not app.exception
    assert Store(root).browse("jobs")["total"] == 1
    app.run()
    assert Store(root).browse("jobs")["total"] == 1
