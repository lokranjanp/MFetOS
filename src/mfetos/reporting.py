from .models import Report


def safe(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def markdown(report: Report) -> str:
    lines = [
        f"# {safe(report.issuer)} IPO research",
        "",
        f"Analysis cutoff: {report.as_of.isoformat()}",
        f"Run: {report.run_id}",
        "",
        "## Executive conclusion",
        "",
        f"**Long term — {report.long_term.status}:** {safe(report.long_term.assessment)}",
        "",
        f"**Listing outlook — {report.listing.status}:** {safe(report.listing.assessment)}",
        "",
    ]
    citations = {e.evidence_id: f"[{i + 1}]" for i, e in enumerate(report.evidence)}
    lines += ["Supporting inputs: " + " ".join(citations[e] for e in report.long_term.evidence_ids), ""]
    for branch in report.branches.values():
        lines += [f"## {branch.name.replace('_', ' ').title()}", ""]
        for c in branch.claims:
            refs = " ".join(citations[e] for e in c.evidence_ids)
            lines += [f"- **{c.kind}:** {safe(c.text)} {refs}"]
        if branch.metrics:
            lines += [
                "",
                "| Metric | Period/basis | Value | Unit | Formula/input evidence |",
                "|---|---|---:|---|---|",
            ]
            facts = {f.fact_id: f for f in report.facts}
            for m in branch.metrics:
                refs = sorted(
                    {citations[e] for fid in m.input_ids if fid in facts for e in facts[fid].evidence_ids}
                )
                val = str(m.value) if m.value is not None else f"Unavailable: {m.unavailable_reason}"
                lines.append(
                    f"| {safe(m.name)} | {safe(m.period or 'offer')} | {safe(val)} | {safe(m.unit)} | {safe(m.formula)} {' '.join(refs)} |"
                )
        lines.append("")
    lines += [
        "## Extracted financial and offer facts",
        "",
        "| Issuer | Metric | Period | Basis/status | Value | Evidence |",
        "|---|---|---|---|---:|---|",
    ]
    for f in report.facts:
        lines.append(
            f"| {safe(f.ipo_id)} | {safe(f.metric)} | {f.period_end or f.period_kind} | {f.basis}/{f.status}/{f.validation} | {safe(f.value)} {safe(f.unit)} | {' '.join(citations[e] for e in f.evidence_ids)} |"
        )
    lines += ["", "## Bear/base/bull assumptions", ""]
    for scenario in report.scenarios:
        lines.append(f"- {safe(scenario)}")
    if not report.scenarios:
        lines.append("Unavailable: justified assumptions or required financial inputs are missing.")
    for title, values in (
        ("Bull case — conditional inferences", report.bull_case),
        ("Bear case — conditional inferences", report.bear_case),
        ("Conditions that would change the conclusion", report.conditions_for_change),
        ("Evidence coverage", [str(report.coverage)]),
        ("Missing evidence and unresolved gaps", report.missing_data),
        ("Stale inputs", report.stale_inputs),
        ("Conflicts", report.conflicts),
        ("Limitations", report.limitations),
    ):
        lines += ["", f"## {title}", ""] + [f"- {safe(v)}" for v in values]
        if not values:
            lines.append("None recorded.")
    lines += ["", "## Page-level evidence", ""]
    for e in report.evidence:
        # Source URL is recorded separately; quotes are bounded and never a PDF redistribution.
        lines += [
            f"{citations[e.evidence_id]} {safe(e.source_url)} — document `{e.document_id}`, PDF p. {e.pdf_page}"
            f" (printed {e.printed_page or 'unknown'}); published {e.published_at.isoformat()}; retrieved {e.retrieved_at.isoformat()}.",
            "",
        ]
    return "\n".join(lines)
