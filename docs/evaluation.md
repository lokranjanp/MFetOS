# Historical evaluation without look-ahead leakage

## Freeze the information set

Choose issuer cohorts and decision times before evaluating results: pre-open after price-band publication, first/second subscription-day closes, and offer close. Keep each run's exact India-time cutoff, original source bytes/hash, public-availability proof, retrieval time, document amendments and configuration/code/model versions.

Use contemporaneous archives or independently verified filing-publication records. Signing dates, fiscal period ends, present-day website timestamps and an LLM's knowledge are not publication proofs. Date-only records become eligible at the end of the publication day. Retrieve-later is acceptable only when earlier availability of those exact contents is established; otherwise mark the case ineligible for a strict historical benchmark. The supplied Waaree example is a reconstruction, not a substitute for this archive.

Filter documents, restatements, peers, corporate actions, demand observations and market prices **before** extraction or model context construction. Later amendments apply only to later cutoffs. Never let today's vendor fundamentals, present-day peer membership, listing outcomes or corrected historical tables leak into an earlier analysis. Keep both fiscal-effective and public-availability dates. A market candle dated on a session is not available before that session closes.

Build chronological development/validation/holdout sets, keeping issuer families and near-duplicate filings together. Select peer candidates using only information available at cutoff and preserve inclusion/exclusion rationales. Pre-register any model/prompt changes and run them on a new holdout rather than repeatedly tuning against known outcomes.

## Measure three research properties independently

**Extraction accuracy:** annotate a stratified set of annual/interim financial cells, offer fields and governance passages, including scanned pages, lakhs/crores/millions, negative cash flows, missing cells, standalone/consolidated tables and restatements. Compare value, scale, sign, period, basis, attributable-versus-total earnings, document and PDF/printed page independently. Report exact-match precision/recall, unit/basis errors, conflict detection and abstention coverage. An unavailable value is neither a correct zero nor an automatic extraction error; report coverage alongside accuracy.

**Citation support:** have reviewers inspect the original page, not just retrieved text. Classify each factual claim as supported, partially supported, unsupported or contradicted. Separately evaluate whether management claims are labelled correctly and whether inferences follow from their cited premises. Measure missing/wrong-page references, source-span support, source currency and cutoff eligibility. Exact quote/ID validation establishes lineage, not semantic entailment. Use independent second review for disputed or material governance claims.

**Numerical correctness:** run deterministic golden cases for unit/sign conversion, calendar CAGR including gaps/leap years, post-issue versus weighted-average EPS, OFS-only/fresh/mixed offers, discounted employee tranches, splits/bonuses, cash/debt/proceeds accounting, leases, tax-sensitive normalisation, average balances, missing/non-meaningful denominators and subscription scope/category deduplication. Recompute report metrics from their recorded input IDs and formulas. Verify that omitted or conflicting inputs produce explicit unavailability and never change another denominator to compensate.

Assess evidence gates separately: unsupported sector/SME detection; material ambiguity interrupts; insufficient-evidence abstention; optional missing demand not blocking fundamental research; and independence of the two final conclusions. Inject hostile document instructions and malformed model responses to verify rejection without tool execution or secret access.

## Evaluate outcomes separately

Only after research-quality measurements are locked, join later outcomes in a separate dataset/process: official listing-day open/close, subsequent operating cash flows, earnings and clearly defined longer-horizon returns. Adjust prices/shares for corporate actions and record benchmark choice, dividends, survivorship, withdrawals, delistings and missing cases. Do not interpret listing returns as validation of governance research or a later price decline as proof an extraction was wrong.

The MVP emits no return probabilities or investment scores. Any future probability model needs a separately versioned target definition, chronological out-of-sample calibration, uncertainty evaluation and an explicit data-availability audit. No claimed confidence level should be inferred from LLM fluency, evidence counts, GMP or subscription multiples.

## Reproducibility record

Retain input manifests, original content hashes, publication proofs, extraction artifacts, fact/evidence IDs, peer decisions, model identity, formula/code versions, graph checkpoints and final JSON. Pin dependencies. Record failed/blocked providers and rights restrictions. Keep originals private unless redistribution is expressly permitted. Publish aggregate evaluation metrics only with the applicable source permissions, alongside error cases and coverage rather than a single opaque score.
