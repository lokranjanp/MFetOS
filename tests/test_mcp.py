import asyncio
import os
import sys

from conftest import CUTOFF
from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def test_mcp_stdio_ingestion_to_cited_report(service, manifest):
    await service.register(manifest)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mfetos", "--data-dir", str(service.settings.data_dir), "serve"],
        env={**os.environ, "MFETOS_WORKER_CONCURRENCY": "1"},
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert names == {
            "search_ipos",
            "get_ipo_documents",
            "get_ipo_facts",
            "get_peer_comparison",
            "get_subscription_snapshot",
            "start_ipo_analysis",
            "get_analysis_status",
            "get_analysis_report",
            "get_evidence",
        }
        result = await client.call_tool(
            "start_ipo_analysis", {"ipo_id": manifest.issuer.ipo_id, "as_of": CUTOFF.isoformat()}
        )
        job = result.structured_content
        assert job["run_id"]
        for _ in range(100):
            result = await client.call_tool("get_analysis_status", {"run_id": job["run_id"]})
            if result.structured_content["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.1)
        result = await client.call_tool("get_analysis_report", {"run_id": job["run_id"]})
        report = result.structured_content
        assert report["status"] == "ok", report
        assert "PDF p." in report["data"]["markdown"]
        eid = report["data"]["report"]["evidence"][0]["evidence_id"]
        evidence = await client.call_tool("get_evidence", {"evidence_id": eid})
        assert evidence.structured_content["pdf_page"] >= 1
        resource = await client.read_resource(f"ipo://reports/{job['run_id']}/report.md")
        assert resource
