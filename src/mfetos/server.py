"""Official MCP SDK interface. Stdout is reserved for protocol traffic."""

from contextlib import asynccontextmanager
from typing import Literal

from mcp.server import MCPServer
from pydantic import AwareDatetime

from .config import Settings
from .models import Evidence, Run, SearchResult, ToolResult, now
from .service import ResearchService


def create_server(settings: Settings | None = None) -> MCPServer:
    service = ResearchService(settings)

    @asynccontextmanager
    async def lifespan(server):
        async with service.workers():
            yield {"service": service}

    server = MCPServer(
        "MFetOS IPO research",
        lifespan=lifespan,
        instructions="Evidence-backed Indian mainboard IPO research. No trade execution. Missing evidence causes abstention.",
    )

    @server.tool()
    def search_ipos(
        query: str = "",
        status: Literal["upcoming", "open", "closed", "listed", "unknown"] | None = None,
        board: Literal["mainboard", "sme", "unknown"] | None = None,
    ) -> SearchResult:
        """Search registered IPO identities. Ingest PDFs/URLs through the CLI first."""
        return service.search_ipos(query, status, board)

    @server.tool()
    def get_ipo_documents(ipo_id: str, as_of: AwareDatetime | None = None) -> ToolResult:
        """Get versioned document metadata eligible at the requested cutoff."""
        return service.get_ipo_documents(ipo_id, as_of or now())

    @server.tool()
    def get_ipo_facts(ipo_id: str, as_of: AwareDatetime | None = None) -> ToolResult:
        """Get validated facts with page-level evidence; unavailable data is explicit."""
        return service.get_ipo_facts(ipo_id, as_of or now())

    @server.tool()
    def get_peer_comparison(
        ipo_id: str, peer_ids: list[str], as_of: AwareDatetime | None = None
    ) -> ToolResult:
        """Compare registered peer facts. Candidate peers are not automatically approved comparables."""
        return service.get_peer_comparison(ipo_id, peer_ids, as_of or now())

    @server.tool()
    def get_subscription_snapshot(ipo_id: str, as_of: AwareDatetime | None = None) -> ToolResult:
        """Get timestamped demand while preserving denominators, scope and anchors."""
        return service.get_subscription_snapshot(ipo_id, as_of or now())

    @server.tool()
    def start_ipo_analysis(
        ipo_id: str,
        objective: Literal["long_term", "listing", "both"] = "both",
        as_of: AwareDatetime | None = None,
    ) -> Run:
        """Durably enqueue analysis and immediately return run_id for later retrieval."""
        return service.start_ipo_analysis(ipo_id, objective, as_of)

    @server.tool()
    def get_analysis_status(run_id: str) -> Run:
        """Retrieve durable run state, progress and any material decision required."""
        return service.get_analysis_status(run_id)

    @server.tool()
    def get_analysis_report(run_id: str) -> ToolResult:
        """Retrieve validated JSON and Markdown, or not_ready while analysis runs."""
        return service.get_analysis_report(run_id)

    @server.tool()
    def get_evidence(evidence_id: str) -> Evidence:
        """Retrieve the original bounded source passage and page provenance."""
        return service.get_evidence(evidence_id)

    @server.resource("ipo://reports/{run_id}/report.json", mime_type="application/json")
    def report_json(run_id: str) -> str:
        import json

        result = service.get_analysis_report(run_id)
        return json.dumps(
            result.data["report"] if result.status == "ok" else result.model_dump(mode="json"),
            ensure_ascii=False,
        )

    @server.resource("ipo://reports/{run_id}/report.md", mime_type="text/markdown")
    def report_md(run_id: str) -> str:
        result = service.get_analysis_report(run_id)
        return result.data["markdown"] if result.status == "ok" else "Report not ready."

    @server.resource("ipo://evidence/{evidence_id}", mime_type="application/json")
    def evidence_json(evidence_id: str) -> str:
        return service.get_evidence(evidence_id).model_dump_json()

    return server
