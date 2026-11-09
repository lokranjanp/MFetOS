import json

import pytest
from conftest import CUTOFF

from mfetos.config import Settings
from mfetos.observability import Observability


class Exporter:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def create_run(self, **value):
        if self.fail:
            raise ConnectionError("sensitive remote body must never be exported/logged")
        self.calls.append(("create", value))

    def update_run(self, **value):
        self.calls.append(("update", value))

    def get_run_url(self, **kwargs):
        return "https://smith.langchain.com/test-run/" + str(kwargs["run_id"])


async def test_metadata_only_graph_traces_and_fail_open(service, manifest, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    exporter = Exporter()
    settings = Settings(data_dir=service.store.root, langsmith_tracing=True, langsmith_api_key="test-secret")
    service.observer = Observability(settings, service.store, client=exporter)
    await service.register(manifest)
    run = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    await service.execute(run.run_id)
    service.observer.flush()
    assert service.get_analysis_report(run.run_id).status == "ok"
    assert service.get_analysis_status(run.run_id).trace_url.startswith("https://smith.langchain.com/")
    created = [v for method, v in exporter.calls if method == "create"]
    assert any(v["name"] == "graph.normalise" and v.get("parent_run_id") for v in created)
    assert all(v["inputs"] == {} for v in created)
    serialised = json.dumps(exporter.calls, default=str)
    assert "test-secret" not in serialised and "Fixture Industrial" not in serialised
    assert "Revenue from operations" not in serialised and "snippet" not in serialised
    # A broken exporter produces diagnostics, never a failed research operation.
    broken = Observability(settings, service.store, client=Exporter(fail=True))
    with broken.span("provider.fetch", correlation_id=run.run_id):
        pass
    broken.flush()
    assert broken.export_failed
    logs = "".join(p.read_text() for p in (service.store.root / "logs").glob("*.jsonl"))
    assert "sensitive remote body" not in logs and "test-secret" not in logs


def test_disabled_tracing_makes_no_requests(service):
    client = Exporter()
    observer = Observability(
        Settings(data_dir=service.store.root, langsmith_tracing=False), service.store, client=client
    )
    with observer.span("local"):
        observer.event("local", "counter", counts={"facts": 2, "prompt": "never export"})
    assert not client.calls


async def test_worker_shutdown_does_not_mark_persisted_report_failed(service, manifest):
    import asyncio

    await service.register(manifest)
    run = service.start_ipo_analysis(manifest.issuer.ipo_id, as_of=CUTOFF)
    await service.execute(run.run_id)
    service.store.update_run(run.run_id, trace_id="previous-attempt", trace_url="https://example.org/old")
    with pytest.raises(asyncio.CancelledError):
        with service.observer.span("analysis", correlation_id=run.run_id, run_id=run.run_id, attempt=True):
            raise asyncio.CancelledError()
    with service.store.connect() as db:
        attempt = db.execute(
            "SELECT status FROM execution_attempts ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    assert attempt["status"] == "finished"
    assert service.get_analysis_status(run.run_id).trace_url is None
    assert service.get_analysis_report(run.run_id).status == "ok"
