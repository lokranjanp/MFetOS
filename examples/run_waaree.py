"""Download the real source documents or use previously downloaded local originals."""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from mfetos.config import Settings
from mfetos.models import Manifest
from mfetos.service import ResearchService


async def run(args):
    raw = json.loads((Path(__file__).parent / "waaree-2024" / "manifest.json").read_text())
    for doc, override in zip(raw["documents"], (args.rhp, args.price_band, args.amendment)):
        if override:
            doc["location"] = str(override.resolve())
    service = ResearchService(Settings(data_dir=args.data_dir))
    registration = await service.register(Manifest.model_validate(raw))
    print(registration.model_dump_json(indent=2), flush=True)
    if not registration.data["document_ids"]:
        raise SystemExit("No documents accessible. Supply local PDFs with --rhp/--price-band/--amendment.")
    job = service.start_ipo_analysis("waaree-2024", as_of=datetime.fromisoformat("2024-10-17T23:59:59+05:30"))
    if job.status in {"queued", "running"}:
        async with service.workers():
            while service.get_analysis_status(job.run_id).status in {"queued", "running"}:
                await asyncio.sleep(0.2)
    result = service.get_analysis_report(job.run_id)
    if result.status != "ok":
        raise SystemExit(service.get_analysis_status(job.run_id).model_dump_json())
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(result.data["report"], indent=2))
    (args.output / "report.md").write_text(result.data["markdown"])
    print(service.get_analysis_status(job.run_id).model_dump_json(indent=2))
    print(f"Reports: {args.output.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rhp", type=Path)
    parser.add_argument("--price-band", type=Path)
    parser.add_argument("--amendment", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path("data/waaree"))
    parser.add_argument("--output", type=Path, default=Path("output/waaree-2024"))
    asyncio.run(run(parser.parse_args()))
