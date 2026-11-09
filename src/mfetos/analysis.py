"""Analysis branches and report validation. No model performs arithmetic."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from .finance import cagr, metric, post_issue_shares, proceeds_bridge, ratio
from .models import Branch, Claim, Conclusion, Document, Evidence, Fact, Manifest, Report, Run, Subscription

D = Decimal


def supported_sector(sector: str) -> bool:
    s = sector.lower()
    return s not in {"unknown", ""} and not any(
        x in s
        for x in (
            "financial",
            "finance",
            "fintech",
            "credit",
            "bank",
            "insurance",
            "nbfc",
            "reit",
            "invit",
            "asset management",
            "lending",
            "real estate",
            "investment trust",
            "brokerage",
            "securities",
        )
    )


def preferred_basis(facts: list[Fact]) -> str:
    return "consolidated" if any(f.basis == "consolidated" for f in facts) else "standalone"


def financial_quality(facts: list[Fact]) -> Branch:
    out = Branch(name="financial_quality")
    basis = preferred_basis(facts)
    groups = defaultdict(dict)
    for f in facts:
        if f.validation == "validated" and f.basis == basis and f.period_end and isinstance(f.value, D):
            groups[(f.period_kind, f.period_end)][f.metric] = f
    annual = sorted(k for k in groups if k[0] == "annual")
    for key in sorted(groups):
        rows = groups[key]
        period = f"{key[1]} {key[0]} {basis}"

        def v(n):
            return rows[n].value if n in rows else None

        def add(name, value, formula, names, unit="ratio"):
            out.metrics.append(
                metric(name, value, formula, [rows[n].fact_id for n in names if n in rows], unit, period)
            )

        add("pat_margin", ratio(v("pat"), v("revenue")), "PAT / revenue", ["pat", "revenue"])
        add("ebitda_margin", ratio(v("ebitda"), v("revenue")), "EBITDA / revenue", ["ebitda", "revenue"])
        add("cash_conversion", ratio(v("cfo"), v("pat")), "CFO / PAT", ["cfo", "pat"])
        capex = v("capex")
        fcf = v("cfo") - abs(capex) if v("cfo") is not None and capex is not None else None
        add("free_cash_flow", fcf, "CFO - absolute cash capex", ["cfo", "capex"], "INR")
        add(
            "debt_equity",
            ratio(v("debt"), v("equity")),
            "total borrowings / closing total equity",
            ["debt", "equity"],
        )
        add(
            "interest_coverage", ratio(v("ebit"), v("interest")), "EBIT / finance costs", ["ebit", "interest"]
        )
        add(
            "other_income_share",
            ratio(v("other_income"), v("pat")),
            "other income / PAT (pre-tax versus post-tax diagnostic)",
            ["other_income", "pat"],
        )
        prev_keys = [k for k in annual if k[1] < key[1]]
        prev = groups[prev_keys[-1]] if prev_keys and key[0] == "annual" else {}
        # ROE/ROCE/turnover require opening and closing balances; no closing-balance substitution.
        for name, numerator, balance, formula in [
            (
                "roe",
                "pat_owners" if basis == "consolidated" else "pat",
                "equity_owners" if basis == "consolidated" else "equity",
                "owners PAT / average owners equity",
            ),
            (
                "receivable_days",
                "revenue",
                "receivables",
                "average current receivables / revenue * period days",
            ),
            ("inventory_days", "cogs", "inventory", "average inventory / cost of goods sold * period days"),
            ("payable_days", "cogs", "payables", "average trade payables / cost of goods sold * period days"),
        ]:
            opening = prev.get(balance)
            closing = rows.get(balance)
            avg = (opening.value + closing.value) / 2 if opening and closing else None
            num = v(numerator)
            value = ratio(num, avg) if name == "roe" else ratio(avg, num)
            if value is not None and name.endswith("days"):
                f = rows.get(numerator)
                value *= D((f.period_end - f.period_start).days + 1) if f.period_start else D(365)
            ids = [x.fact_id for x in (opening, closing, rows.get(numerator)) if x]
            out.metrics.append(
                metric(name, value, formula, ids, "days" if name.endswith("days") else "ratio", period)
            )
        employed = None
        if all(n in rows and n in prev for n in ("assets", "current_liabilities")):
            employed = (
                v("assets")
                - v("current_liabilities")
                + prev["assets"].value
                - prev["current_liabilities"].value
            ) / 2
        add(
            "roce",
            ratio(v("ebit"), employed),
            "EBIT / average(total assets - current liabilities)",
            ["ebit", "assets", "current_liabilities"],
        )
        out.metrics[-1].input_ids.extend(
            prev[n].fact_id for n in ("assets", "current_liabilities") if n in prev
        )
        if prev:
            for name in ("revenue", "pat", "receivables"):
                value = ratio(v(name), prev[name].value) if name in prev else None
                ids = [rows[name].fact_id] if name in rows else []
                if name in prev:
                    ids.append(prev[name].fact_id)
                out.metrics.append(
                    metric(
                        name + "_growth",
                        value - 1 if value is not None else None,
                        "current / prior comparable annual - 1",
                        ids,
                        period=period,
                    )
                )
    if len(annual) >= 2:
        for name in ("revenue", "pat"):
            first, last = groups[annual[0]].get(name), groups[annual[-1]].get(name)
            value = (
                cagr(first.value, last.value, first.period_end, last.period_end) if first and last else None
            )
            out.metrics.append(
                metric(
                    name + "_cagr",
                    value,
                    "(last / first) ** (1 / elapsed years) - 1",
                    [x.fact_id for x in (first, last) if x],
                    period=basis,
                )
            )
    else:
        out.gaps.append("At least two comparable annual periods are needed for a growth assessment")
    for required in (
        "cfo",
        "capex",
        "interest",
        "exceptional_items",
        "capitalised_costs",
        "tax",
        "receivables",
    ):
        if not any(f.metric == required and f.basis == basis for f in facts):
            out.gaps.append(f"{required}: unavailable or not safely extracted")
    out.claims.append(
        Claim(
            kind="inference",
            text=f"Financial calculations use {basis} figures; interim periods are not annualised.",
        )
    )
    return out


def offer_valuation(facts: list[Fact]) -> Branch:
    out = Branch(name="peers_valuation")
    valid = [f for f in facts if f.validation == "validated" and isinstance(f.value, D)]
    offer = {f.metric: f for f in valid if f.period_kind == "offer"}
    basis = preferred_basis(valid)
    annual = [f for f in valid if f.basis == basis and f.period_kind == "annual"]
    latest_end = max((f.period_end for f in annual), default=None)
    financial = {f.metric: f for f in annual if f.period_end == latest_end}
    balance_names = {"cash", "debt", "nci", "lease_liabilities_total", "preference_capital"}
    balance_date = max(
        (f.period_end for f in valid if f.basis == basis and f.metric in balance_names and f.period_end),
        default=None,
    )
    balance = {f.metric: f for f in valid if f.basis == basis and f.period_end == balance_date}
    # Consolidated P/E uses profit attributable to owners, never total profit including NCI.
    pat = financial.get("pat_owners" if basis == "consolidated" else "pat")
    for endpoint in ("low", "high"):
        price, pre, fresh = (offer.get(n) for n in (f"price_{endpoint}", "pre_shares", "fresh_proceeds"))
        diluted = offer.get("incremental_diluted_shares")
        inputs = [f.fact_id for f in (price, pre, fresh, diluted) if f]
        shares = None
        if price and pre and fresh and diluted:
            try:
                shares = post_issue_shares(
                    pre.value, price.value, fresh.value, incremental_diluted=diluted.value
                )
            except ValueError:
                out.gaps.append(
                    f"{endpoint}: invalid/non-meaningful issuance denominator; valuation unavailable"
                )
        out.metrics.append(
            metric(
                f"post_issue_shares_{endpoint}",
                shares,
                "pre shares + fresh proceeds / price + incremental diluted shares; OFS adds none",
                inputs,
                "shares",
                reason="Missing price, pre shares, gross fresh proceeds or explicit dilution input",
            )
        )
        cap = price.value * shares if price and shares else None
        out.metrics.append(
            metric(f"market_cap_{endpoint}", cap, "offer price * post-issue diluted shares", inputs, "INR")
        )
        out.metrics.append(
            metric(
                f"pe_{endpoint}",
                ratio(cap, pat.value if pat else None),
                "post-issue diluted market cap / latest annual owners PAT",
                inputs + ([pat.fact_id] if pat else []),
            )
        )
        bridge_names = ["cash", "debt"]
        bridge_facts = [balance.get(n) for n in bridge_names] + [
            offer.get(n) for n in ("fresh_proceeds", "issue_expenses", "debt_repayment", "other_uses")
        ]
        ev = None
        lease_flag = offer.get("debt_excludes_leases")
        unrestricted = offer.get("cash_is_unrestricted")
        preference = balance.get("preference_capital")
        leases = balance.get("lease_liabilities_total")
        nci = balance.get("nci")
        if cap is not None and all(bridge_facts):
            try:
                cash, debt = proceeds_bridge(*(f.value for f in bridge_facts))
                # Include leases once, using an explicit definition of reported debt.
                if (
                    preference
                    and unrestricted
                    and unrestricted.value == 1
                    and lease_flag
                    and lease_flag.value in {0, 1}
                    and (lease_flag.value == 0 or leases)
                    and (basis != "consolidated" or nci)
                ):
                    ev = cap + debt - cash + preference.value + (nci.value if nci else D(0))
                    if lease_flag.value == 1:
                        ev += leases.value
            except ValueError as exc:
                out.gaps.append(str(exc))
        ev_ids = (
            inputs
            + [f.fact_id for f in bridge_facts if f]
            + [f.fact_id for f in (lease_flag, unrestricted, preference, leases, nci) if f]
        )
        out.metrics.append(
            metric(
                f"enterprise_value_{endpoint}",
                ev,
                "market cap + post-use debt + preference capital + NCI + leases(if excluded from debt) - post-use unrestricted cash; reported Ind AS earnings",
                ev_ids,
                "INR",
            )
        )
        for den in ("ebitda", "ebit"):
            f = financial.get(den)
            out.metrics.append(
                metric(
                    f"ev_{den}_{endpoint}",
                    ratio(ev, f.value if f else None),
                    f"enterprise value / latest comparable annual {den}",
                    ev_ids + ([f.fact_id] if f else []),
                )
            )
        cfo, capex = financial.get("cfo"), financial.get("capex")
        fcf = cfo.value - abs(capex.value) if cfo and capex else None
        out.metrics.append(
            metric(
                f"fcf_yield_{endpoint}",
                ratio(fcf, cap),
                "(CFO - cash capex) / diluted market cap",
                inputs + [f.fact_id for f in (cfo, capex) if f],
            )
        )
    out.gaps.extend(
        [
            "Final share issuance, allocation and employee-discount tranches require confirmation",
            "EV requires an evidenced cash/debt bridge, unrestricted cash and consistent lease treatment",
        ]
    )
    return out


def subscriptions_at(rows: list[Subscription], cutoff: datetime) -> tuple[list[dict], list[str]]:
    eligible = [s for s in rows if s.observed_at <= cutoff and s.published_at <= cutoff]
    grouped = defaultdict(list)
    for s in eligible:
        grouped[(s.category, s.scope, s.observed_at, s.published_at)].append(s)
    eligible, conflicting = [], []
    for key, candidates in grouped.items():
        if len({(s.bids, s.offered, s.unit, s.anchor_treatment) for s in candidates}) > 1:
            conflicting.append(
                f"{key[0]} {key[1]}: conflicting observations at identical timestamp; excluded"
            )
        else:
            eligible.append(candidates[0])
    categories = {s.category for s in eligible}
    out, warnings = [], conflicting
    for category in sorted(categories):
        options = [s for s in eligible if s.category == category]
        consolidated = [s for s in options if s.scope == "consolidated"]
        picked = []
        if consolidated:
            picked = [max(consolidated, key=lambda s: (s.observed_at, s.published_at))]
        else:
            for scope in ("nse", "bse"):
                values = [s for s in options if s.scope == scope]
                if values:
                    picked.append(max(values, key=lambda s: (s.observed_at, s.published_at)))
        # Never combine unlike denominators, anchors, timestamps or overlapping scopes.
        combine = (
            len(picked) == 2
            and all(s.disjoint_exchange_scope for s in picked)
            and len({(s.observed_at, s.unit, s.anchor_treatment) for s in picked}) == 1
        )
        groups = [picked] if combine else [[s] for s in picked]
        if len(picked) == 2 and not combine:
            warnings.append(f"{category}: exchange observations retained separately; no safe consolidation")
        for group in groups:
            bids = sum((s.bids for s in group), D(0))
            offered = sum((s.offered for s in group), D(0))
            out.append(
                {
                    "category": category,
                    "scope": "combined_disjoint" if combine else group[0].scope,
                    "bids": str(bids),
                    "offered": str(offered),
                    "multiple": str(bids / offered),
                    "observed_at": group[0].observed_at.isoformat(),
                    "unit": group[0].unit,
                    "anchor_treatment": group[0].anchor_treatment,
                    "evidence_ids": sorted({e for s in group for e in s.evidence_ids}),
                }
            )
    return out, warnings


def demand_branch(rows: list[Subscription], cutoff: datetime, facts: list[Fact] | None = None) -> Branch:
    out = Branch(name="demand_market")
    snapshots, warnings = subscriptions_at(rows, cutoff)
    out.gaps.extend(warnings)
    for s in snapshots:
        if s["anchor_treatment"] == "unknown":
            out.gaps.append(f"{s['category']}: anchor treatment unknown; no consolidated demand inference")
        out.claims.append(
            Claim(
                kind="fact",
                text=(
                    f"{s['category']} {s['scope']} bids {s['bids']} / offered {s['offered']} "
                    f"{s['unit']} = {s['multiple']}x at {s['observed_at']}; anchors {s['anchor_treatment']}"
                ),
                evidence_ids=s["evidence_ids"],
            )
        )
    if not snapshots:
        out.gaps.append(
            "No validated subscription observation available at cutoff; absence is not zero demand"
        )
    supplied = {f.metric: f for f in facts or [] if f.validation == "validated"}
    for key in (
        "anchor_allocation",
        "free_float",
        "issue_size",
        "index_return_5d",
        "comparable_listing_return",
    ):
        if key not in supplied:
            out.gaps.append(f"{key}: timestamped source unavailable")
        else:
            f = supplied[key]
            out.claims.append(
                Claim(kind="fact", text=f"{key}: {f.value} {f.unit}", evidence_ids=f.evidence_ids)
            )
    return out


def research_branch(documents: list[Document]) -> tuple[Branch, list[Evidence]]:
    from .ingestion import evidence_for

    branch, evidence = Branch(name="business_governance"), []
    for section in ("business", "governance"):
        count = 0
        for doc in sorted(documents, key=lambda d: d.source.published_at, reverse=True):
            for page in doc.pages:
                if page.section != section:
                    continue
                # Preserve exact passages, explicitly as issuer/management disclosures.
                sentences = [s.strip() for s in page.text.split("\n") if len(s.strip()) > 65]
                snippet = "\n".join(sentences[:8])[:1600]
                if not snippet:
                    continue
                ev = evidence_for(doc, page, snippet)
                evidence.append(ev)
                branch.claims.append(
                    Claim(kind="management_claim", text=snippet, evidence_ids=[ev.evidence_id])
                )
                count += 1
                if count >= 3:
                    break
            if count >= 3:
                break
        if count == 0:
            branch.gaps.append(f"{section}: no safely identified section")
    branch.gaps.extend(
        [
            "Extractive disclosures require analyst interpretation; no clean-governance conclusion is inferred",
            "Customer/supplier concentration, segments, capacity, order book and litigation materiality require full review",
        ]
    )
    return branch, evidence


def peer_branch(
    facts: list[Fact],
    peers: dict[str, tuple[Manifest, list[Fact]]],
    cutoff: datetime,
    rationales: dict[str, str] | None = None,
) -> Branch:
    out = offer_valuation(facts)
    for peer_id, (manifest, peer_facts) in peers.items():
        rationale = (rationales or {}).get(peer_id)
        include = bool(rationale) and supported_sector(manifest.issuer.sector)
        out.claims.append(
            Claim(
                kind="inference",
                text=(
                    f"Peer {manifest.issuer.legal_name} ({peer_id}): "
                    + (
                        f"included by analyst selection: {rationale}"
                        if include
                        else "excluded from valuation: no supported business-mix, growth, margin, size and capital-intensity rationale"
                    )
                ),
            )
        )
        financial = financial_quality(peer_facts)
        for m in financial.metrics:
            out.metrics.append(m.model_copy(update={"name": f"peer:{peer_id}:{m.name}"}))
        offer = {f.metric: f for f in peer_facts if f.period_kind == "offer" and f.validation == "validated"}
        basis = preferred_basis(peer_facts)
        owners = sorted(
            [
                f
                for f in peer_facts
                if f.metric == ("pat_owners" if basis == "consolidated" else "pat")
                and f.period_kind == "annual"
                and f.validation == "validated"
            ],
            key=lambda f: f.period_end,
        )
        price, shares = offer.get("peer_price"), offer.get("listed_shares")
        pat = owners[-1] if owners else None
        pe = ratio(price.value * shares.value, pat.value) if include and price and shares and pat else None
        out.metrics.append(
            metric(
                f"peer:{peer_id}:pe",
                pe,
                "dated peer price * action-adjusted listed shares / owners PAT",
                [f.fact_id for f in (price, shares, pat) if f],
                period=f"{pat.period_end} {basis}" if pat else basis,
                reason="Missing price/share/owners earnings or peer selection rationale",
            )
        )
    if not peers:
        out.gaps.append(
            "No independently validated listed-peer documents/prices; relative valuation unavailable"
        )
    return out


def make_report(
    run: Run,
    manifest: Manifest,
    facts: list[Fact],
    evidence: list[Evidence],
    branches: dict[str, Branch],
    conflicts: list[str],
    gaps: list[str],
    unsupported=False,
    peer_facts: list[Fact] | None = None,
) -> Report:
    metrics = {m.name: m for m in branches.get("peers_valuation", Branch(name="peers_valuation")).metrics}
    annual = {
        f.period_end
        for f in facts
        if f.metric == "revenue"
        and f.period_kind == "annual"
        and f.validation == "validated"
        and f.basis == preferred_basis(facts)
    }
    critical = list(gaps)
    if len(annual) < 2:
        critical.append("Two comparable annual revenue periods unavailable")
    if conflicts:
        critical.append("Material source conflicts require resolution")
    if not metrics.get("pe_high") or metrics["pe_high"].value is None:
        critical.append("Supported post-issue owners-earnings valuation unavailable")
    business = branches.get("business_governance")
    if not business or not business.claims:
        critical.append("Business/governance evidence unavailable")
    # MVP does not turn coverage alone into an investment recommendation.
    long_term = Conclusion(
        status="unsupported" if unsupported else "insufficient_evidence",
        assessment="Unsupported issue/sector"
        if unsupported
        else "Research available; investment attractiveness remains unresolved",
        reasons=critical or ["Peer comparability and governance materiality require analyst review"],
    )
    demand = branches.get("demand_market", Branch(name="demand_market"))
    listing = Conclusion(
        status="unsupported" if unsupported else "insufficient_evidence",
        assessment="Listing return cannot be reliably inferred from available evidence",
        reasons=demand.gaps or ["Demand alone cannot establish an expected listing return"],
    )
    eligible_demand = [
        s
        for s in manifest.subscriptions
        if s.observed_at <= run.as_of
        and s.published_at <= run.as_of
        and s.anchor_treatment == "excluded"
        and set(s.evidence_ids) <= {e.evidence_id for e in evidence}
    ]
    if (
        not unsupported
        and not demand.gaps
        and {s.category for s in eligible_demand} >= {"qib", "nii", "retail"}
    ):
        snapshots, _ = subscriptions_at(eligible_demand, run.as_of)
        covered = all(D(s["multiple"]) >= 1 for s in snapshots if s["category"] in {"qib", "nii", "retail"})
        listing = Conclusion(
            status="supported",
            assessment=(
                "Demand covers offered shares in all three investor categories; listing outcome remains uncertain"
                if covered
                else "Demand is uneven across investor categories; listing outcome remains uncertain"
            ),
            reasons=[
                "Demand, issue structure and market observations are descriptive, not a validated return model"
            ],
            evidence_ids=sorted({eid for c in demand.claims for eid in c.evidence_ids}),
        )
    scenarios = []
    # Explicit analyst-supplied multiple sensitivities, never numerical confidence.
    owners = sorted(
        [
            f
            for f in facts
            if f.metric == ("pat_owners" if preferred_basis(facts) == "consolidated" else "pat")
            and f.period_kind == "annual"
            and f.validation == "validated"
        ],
        key=lambda f: f.period_end,
    )
    if manifest.scenario_rationale and owners and owners[-1].value > 0:
        for endpoint in ("low", "high"):
            endpoint_shares = metrics.get(f"post_issue_shares_{endpoint}")
            if endpoint_shares is None or endpoint_shares.value is None:
                continue
            for scenario in ("bear", "base", "bull"):
                multiple = manifest.scenario_multiples.get(scenario)
                if multiple is None or multiple <= 0:
                    continue
                scenarios.append(
                    {
                        "case": scenario,
                        "price_endpoint": endpoint,
                        "method": "P/E sensitivity; not a price target",
                        "multiple": str(multiple),
                        "earnings": str(owners[-1].value),
                        "earnings_assumption": "Unchanged latest annual owners PAT",
                        "share_count": str(endpoint_shares.value),
                        "value_per_share": str(owners[-1].value * multiple / endpoint_shares.value),
                        "rationale": manifest.scenario_rationale,
                        "input_ids": [owners[-1].fact_id] + endpoint_shares.input_ids,
                    }
                )
    if not scenarios:
        critical.append(
            "Bear/base/bull values unavailable without justified assumptions and owners earnings/share counts"
        )
    financial = branches.get("financial_quality", Branch(name="financial_quality"))
    cash_metrics = [
        m
        for m in financial.metrics
        if m.name == "cash_conversion" and m.value is not None and m.period and "annual" in m.period
    ]
    if not cash_metrics:
        critical.append("Annual cash conversion evidence unavailable")
    if not unsupported and not critical and len(scenarios) == 6:
        base_value = D(
            next(
                s["value_per_share"]
                for s in scenarios
                if s["case"] == "base" and s["price_endpoint"] == "high"
            )
        )
        high = next((f for f in facts if f.metric == "price_high" and f.validation == "validated"), None)
        favourable = high and high.value <= base_value
        long_term = Conclusion(
            status="supported",
            assessment=(
                "Conditionally attractive under the supplied base multiple and unchanged earnings assumptions; governance requires review"
                if favourable
                else "Offer exceeds the supplied base valuation sensitivity; long-term attractiveness is not established"
            ),
            reasons=[
                "Assessment is conditional on the explicit analyst valuation assumptions, not a universal P/E threshold",
                "Business and governance passages are issuer disclosures, not independent verification",
            ],
            evidence_ids=sorted({e for f in facts for e in f.evidence_ids}),
        )
    all_gaps = critical + [g for b in branches.values() for g in b.gaps]
    stale = []
    for f in facts:
        if f.period_end and (run.as_of.date() - f.period_end).days > 180:
            stale.append(
                f"{f.metric} period {f.period_end}; age {(run.as_of.date() - f.period_end).days} days (descriptive, not a sector threshold)"
            )
    return Report(
        run_id=run.run_id,
        ipo_id=run.ipo_id,
        issuer=manifest.issuer.legal_name,
        as_of=run.as_of,
        long_term=long_term,
        listing=listing,
        branches=branches,
        facts=facts + (peer_facts or []),
        evidence=evidence,
        scenarios=scenarios,
        missing_data=sorted(set(all_gaps)),
        conflicts=conflicts,
        stale_inputs=sorted(set(stale)),
        bull_case=[
            "Sustained comparable growth, cash conversion and evidenced competitive strengths would support the thesis."
        ],
        bear_case=[
            "Weak cash conversion, material governance findings or an unsupported offer premium would weaken the thesis."
        ],
        conditions_for_change=[
            "Resolve missing owners earnings, dilution and proceeds inputs",
            "Validate listed-peer comparability and prices at cutoff",
            "Review governance disclosures and quantified business concentration",
            "Add timely demand, anchors, float and market observations for listing outlook",
        ],
        coverage={
            "validated_facts": sum(f.validation == "validated" for f in facts),
            "evidence_items": len(evidence),
            "annual_revenue_periods": len(annual),
            "branches": list(branches),
        },
        limitations=[
            "Conclusions are qualitative analyst inferences, without investment scores or return probabilities.",
            "Automated citation validation establishes lineage and cutoff eligibility, not semantic truth.",
            "No trade execution; GMP and oversubscription do not guarantee returns.",
        ],
    )
