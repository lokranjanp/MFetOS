"""Local CLI. All outputs are JSON except report Markdown and MCP traffic."""

import argparse
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from .config import Settings
from .models import Manifest, now
from .providers import PublicDiscovery, capabilities
from .service import ResearchService


def cutoff(raw):
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("Include a timezone, for example +05:30")
    return value


def main():
    parser = argparse.ArgumentParser(description="Evidence-backed Indian IPO research")
    parser.add_argument("--data-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="Register a manifest and ingest its public URLs/local PDFs")
    ingest.add_argument("manifest", type=Path)
    search = sub.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    discover = sub.add_parser("discover")
    discover.add_argument("query")
    analyze = sub.add_parser("analyze")
    analyze.add_argument("ipo_id")
    analyze.add_argument("--as-of", type=cutoff)
    analyze.add_argument("--objective", choices=["both", "long_term", "listing"], default="both")
    analyze.add_argument(
        "--wait", action="store_true", help="Execute in this process and wait for the report"
    )
    for name in ("status", "report"):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        if name == "report":
            p.add_argument("--format", choices=["json", "markdown"], default="markdown")
            p.add_argument("--output", type=Path)
    resume = sub.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--decision", required=True, help="JSON object resolving the recorded ambiguity")
    retry = sub.add_parser("retry")
    retry.add_argument("run_id")
    sub.add_parser("worker")
    sub.add_parser("serve")
    sub.add_parser("providers")
    args = parser.parse_args()
    settings = Settings()
    if args.data_dir:
        settings.data_dir = args.data_dir
    # Suppress HTTP request lines, which can contain signed URLs/credential material.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "serve":
        from .server import create_server

        create_server(settings).run(transport="stdio")
        return

    async def run():
        service = ResearchService(settings)
        if args.command == "ingest":
            raw = json.loads(args.manifest.read_text())
            # Local locations are relative to the manifest rather than the caller's cwd.
            for d in raw.get("documents", []):
                if not d["location"].startswith(("https://", "http://")):
                    d["location"] = str((args.manifest.parent / d["location"]).resolve())
            return await service.register(Manifest.model_validate(raw))
        if args.command == "search":
            return service.search_ipos(args.query)
        if args.command == "discover":
            return await PublicDiscovery(service.fetcher).search(args.query)
        if args.command == "providers":
            return capabilities()
        if args.command == "analyze":
            job = service.start_ipo_analysis(args.ipo_id, args.objective, args.as_of or now())
            if args.wait and job.status in {"queued", "running"}:
                # Use the leased worker, avoiding duplicate execution with an MCP worker.
                async with service.workers():
                    while service.get_analysis_status(job.run_id).status in {"queued", "running"}:
                        await asyncio.sleep(0.2)
            return service.get_analysis_status(job.run_id)
        if args.command == "status":
            return service.get_analysis_status(args.run_id)
        if args.command == "report":
            result = service.get_analysis_report(args.run_id)
            if result.status != "ok":
                return result
            output = (
                result.data["markdown"]
                if args.format == "markdown"
                else json.dumps(result.data["report"], indent=2)
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output)
                return {"output": str(args.output)}
            return output
        if args.command == "resume":
            service.resume(args.run_id, json.loads(args.decision))
            return service.get_analysis_status(args.run_id)
        if args.command == "retry":
            service.retry(args.run_id)
            return service.get_analysis_status(args.run_id)
        if args.command == "worker":
            async with service.workers():
                await asyncio.Event().wait()

    try:
        result = asyncio.run(run())
        if isinstance(result, str):
            print(result)
        elif hasattr(result, "model_dump_json"):
            print(result.model_dump_json(indent=2))
        else:
            print(json.dumps(result, indent=2, default=str))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"{type(exc).__name__}: invalid or unavailable local input\n")


if __name__ == "__main__":
    main()
