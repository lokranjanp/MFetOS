"""Public contracts. Monetary amounts are decimal INR, never binary floats."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> datetime:
    return datetime.now(timezone.utc)


def digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Issuer(Model):
    ipo_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    legal_name: str
    cin: str | None = None
    isin: str | None = None
    aliases: list[str] = Field(default_factory=list)
    board: Literal["mainboard", "sme", "unknown"] = "unknown"
    sector: str = "unknown"
    status: Literal["upcoming", "open", "closed", "listed", "unknown"] = "unknown"


class DocumentInput(Model):
    location: str
    source_url: str
    kind: Literal["drhp", "rhp", "prospectus", "price_band", "amendment", "annual_report", "other"]
    published_at: AwareDatetime | None = None
    publication_evidence: str | None = None
    publication_status: Literal["verified", "declared", "unknown"] = "declared"
    supersedes: list[str] = Field(default_factory=list)
    # A correction overrides only these metric names; it never replaces the whole RHP.
    amended_metrics: list[str] = Field(default_factory=list)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    parser_backend: Literal["pdfplumber", "docling"] | None = None


class Page(Model):
    pdf_page: int = Field(ge=1)
    printed_page: str | None = None
    text: str
    section: str = "other"
    tables: list[list[list[str | None]]] = Field(default_factory=list)
    method: Literal["text", "ocr", "docling", "unavailable"] = "text"
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class Document(Model):
    document_id: str
    ipo_id: str
    source: DocumentInput
    sha256: str
    retrieved_at: AwareDatetime
    pages: list[Page] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    extractor_version: str = "1"
    extraction_id: str | None = None
    parser_backend: str = "pdfplumber"


class Evidence(Model):
    evidence_id: str
    ipo_id: str
    document_id: str
    source_url: str
    pdf_page: int = Field(ge=1)
    printed_page: str | None = None
    snippet: str
    published_at: AwareDatetime | None
    retrieved_at: AwareDatetime
    validation: Literal["source_matched", "unverified", "conflicting"] = "source_matched"


class Fact(Model):
    fact_id: str
    ipo_id: str
    metric: str
    value: Decimal | str
    unit: str
    currency: str | None = None
    period_end: date | None = None
    period_start: date | None = None
    period_kind: Literal["annual", "interim", "point_in_time", "offer"] = "offer"
    basis: Literal["consolidated", "standalone", "not_applicable", "unknown"] = "unknown"
    status: Literal["reported", "normalised", "projected"] = "reported"
    category: Literal["extracted_fact", "management_claim", "assumption", "analyst_inference"] = (
        "extracted_fact"
    )
    evidence_ids: list[str] = Field(min_length=1)
    validation: Literal["validated", "unverified", "conflicting"] = "validated"

    @field_validator("value", mode="before")
    @classmethod
    def preserve_decimal_json(cls, value):
        # JSON has no Decimal scalar. Restore canonical numeric strings on reload.
        if isinstance(value, str):
            try:
                parsed = Decimal(value)
                if parsed.is_finite():
                    return parsed
            except Exception:
                pass
        return value


class Metric(Model):
    name: str
    value: Decimal | None = None
    unit: str = "ratio"
    period: str | None = None
    formula: str
    input_ids: list[str] = Field(default_factory=list)
    unavailable_reason: str | None = None
    assumptions: list[str] = Field(default_factory=list)


class Subscription(Model):
    snapshot_id: str
    ipo_id: str
    observed_at: AwareDatetime
    published_at: AwareDatetime
    scope: Literal["nse", "bse", "consolidated"]
    category: Literal["qib", "nii", "retail", "employee", "nii_small", "nii_large"]
    bids: Decimal = Field(ge=0)
    offered: Decimal = Field(gt=0)
    unit: Literal["shares", "applications"] = "shares"
    anchor_treatment: Literal["excluded", "included", "unknown"] = "unknown"
    evidence_ids: list[str] = Field(min_length=1)
    disjoint_exchange_scope: bool = False

    @model_validator(mode="after")
    def published_after_observation(self):
        if self.published_at < self.observed_at:
            raise ValueError("Subscription cannot be published before it was observed")
        return self


class SubscriptionSource(Model):
    """User-mapped observation; values must match the quoted original PDF line."""

    source_url: str
    pdf_page: int = Field(ge=1)
    quote: str
    observed_at: AwareDatetime
    scope: Literal["nse", "bse", "consolidated"]
    category: Literal["qib", "nii", "retail", "employee", "nii_small", "nii_large"]
    bids: Decimal = Field(ge=0)
    offered: Decimal = Field(gt=0)
    unit: Literal["shares", "applications"] = "shares"
    anchor_treatment: Literal["excluded", "included", "unknown"] = "unknown"
    disjoint_exchange_scope: bool = False


class Claim(Model):
    text: str
    kind: Literal["fact", "management_claim", "assumption", "inference"]
    evidence_ids: list[str] = Field(default_factory=list)
    input_metric_names: list[str] = Field(default_factory=list)


class Conclusion(Model):
    status: Literal["supported", "insufficient_evidence", "unsupported"]
    assessment: str
    reasons: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class Branch(Model):
    name: str
    claims: list[Claim] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class Report(Model):
    schema_version: str = "1.0"
    run_id: str
    ipo_id: str
    issuer: str
    as_of: AwareDatetime
    generated_at: AwareDatetime = Field(default_factory=now)
    long_term: Conclusion
    listing: Conclusion
    branches: dict[str, Branch]
    facts: list[Fact]
    evidence: list[Evidence]
    scenarios: list[dict[str, Any]] = Field(default_factory=list)
    bull_case: list[str] = Field(default_factory=list)
    bear_case: list[str] = Field(default_factory=list)
    conditions_for_change: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    stale_inputs: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lineage(self):
        evidence = {e.evidence_id: e for e in self.evidence}
        for e in self.evidence:
            if e.published_at is None or e.published_at > self.as_of:
                raise ValueError("Report contains undated or post-cutoff evidence")
        for f in self.facts:
            if not set(f.evidence_ids) <= evidence.keys():
                raise ValueError("Fact references unknown evidence")
        for conclusion in (self.long_term, self.listing):
            if not set(conclusion.evidence_ids) <= evidence.keys():
                raise ValueError("Conclusion references unknown evidence")
        fact_ids = {f.fact_id for f in self.facts}
        for branch in self.branches.values():
            for metric in branch.metrics:
                if not set(metric.input_ids) <= fact_ids:
                    raise ValueError("Calculated metric references unknown input fact")
            for c in branch.claims:
                if c.kind in ("fact", "management_claim") and not c.evidence_ids:
                    raise ValueError("Factual claim lacks a citation")
                if not set(c.evidence_ids) <= evidence.keys():
                    raise ValueError("Claim references unknown evidence")
        return self


class Manifest(Model):
    issuer: Issuer
    documents: list[DocumentInput] = Field(default_factory=list)
    peer_ids: list[str] = Field(default_factory=list)
    peer_rationales: dict[str, str] = Field(default_factory=dict)
    subscriptions: list[Subscription] = Field(default_factory=list)
    subscription_sources: list[SubscriptionSource] = Field(default_factory=list)
    # Analyst choices, not extracted company facts. No growth assumptions are implicit.
    scenario_multiples: dict[str, Decimal] = Field(default_factory=dict)
    scenario_rationale: str | None = None

    @model_validator(mode="after")
    def scenarios_are_explicit(self):
        if self.scenario_multiples:
            if set(self.scenario_multiples) != {"bear", "base", "bull"} or not self.scenario_rationale:
                raise ValueError("Supply all three scenario multiples and an explicit analyst rationale")
            b, m, u = (self.scenario_multiples[n] for n in ("bear", "base", "bull"))
            if not (0 < b <= m <= u):
                raise ValueError("Scenario multiples must be positive and bear <= base <= bull")
        return self


RunStatus = Literal[
    "queued", "running", "awaiting_decision", "completed", "insufficient_evidence", "unsupported", "failed"
]


class Run(Model):
    run_id: str
    ipo_id: str
    objective: Literal["long_term", "listing", "both"] = "both"
    as_of: AwareDatetime
    status: RunStatus = "queued"
    stage: str = "queued"
    created_at: AwareDatetime = Field(default_factory=now)
    updated_at: AwareDatetime = Field(default_factory=now)
    error: str | None = None
    decision: dict[str, Any] | None = None
    trace_id: str | None = None
    trace_url: str | None = None


class ToolResult(Model):
    status: str
    data: Any = None
    warnings: list[str] = Field(default_factory=list)


class SearchResult(Model):
    issuers: list[Issuer]
    warnings: list[str] = Field(default_factory=list)
