# Provider capability and rights review

Reviewed against official documentation during implementation on 2026-11-06. This is an engineering capability record, not a redistribution licence. Provider agreements and exchange rights still govern each account.

## Working default adapters

- **Local PDF/URL input:** original bytes, checksums, versions, page text/tables and source metadata. Public HTTPS fetches reject embedded credentials, private destinations and restricted NSE website automation. Redirects are checked; downloads are bounded; denial/authentication failures are explicit.
- **SEBI/issuer/BRLM discovery:** follows public document links, not invented APIs. [SEBI filings](https://www.sebi.gov.in/filings/public-issues.html), [Axis Capital offer documents](https://www.axiscapital.co.in/offer-documents). The Waaree RHP, price band and corrected `/contents/` corrigendum links were downloaded and checksum-verified. An older `/public/contents/` corrigendum URL returned 404; no bypass was attempted.
- **Ollama:** optional local JSON-schema text interpretation through the [documented API](https://docs.ollama.com/capabilities/structured-outputs). Not tested against a running model in this environment. Source quote validation and unavailable-model routing are implemented; deterministic mode is the default.

## Optional read-only data adapters

**Trendlyne MCP:** official [tool documentation](https://trendlyne.com/subscription/mcp/documentation/) describes `search_entities`, `search_financial_parameters`, `get_stock_parameter_values` and `get_document_search_results`. The adapter negotiates the actual tool list and validates arguments against the advertised schema. It exposes no watchlist mutations. Configure the account's actual MCP URL and bearer token; no URL is guessed. The docs contain inconsistent parameter-count limits, so applications should use conservative batches (at most ten stocks/parameters) and the negotiated schema.

Consumer [plans](https://trendlyne.com/subscription/mcp/plans/) restrict Pro/Max to individual use. Historical metric availability does not establish what was known at an old publication cutoff. Returned data remains `unverified` until source/basis/unit/period and availability can be established. No account credentials were available, so authenticated transport, entitlements and response formats remain unverified live.

**Upstox:** the adapter uses the documented [V3 historical daily candle route](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/), `GET /v3/historical-candle/{instrument_key}/days/1/{to_date}/{from_date}`, with a user token. It does not use IPO order APIs or expose broker mutations. Instrument mapping must be supplied from verified instrument metadata. Daily candle timestamps must not be mistaken for publication at midnight; closed-session prices and corporate-action adjustments need validation before research use.

Both adapters return `license_required` until the corresponding licence acknowledgement and `MFETOS_ALLOW_LICENSED_RETENTION` are configured, and `credentials_missing` without account access. This is a user attestation of applicable rights, not a substitute for the contract. They are callable Python adapters; automatic import of arbitrary vendor data into the fact store is intentionally absent until provenance is mapped. For example:

```python
from mfetos.config import Settings
from mfetos.providers import UpstoxPrices, TrendlynePeers

settings = Settings()
# Inside an async function:
# prices = await UpstoxPrices(settings).daily("VERIFIED_INSTRUMENT_KEY", "2024-10-01", "2024-10-17")
# peers = await TrendlynePeers(settings).call("search_entities", {"query": "Premier Energies"})
```

## Restricted, deferred and manual sources

- **NSE:** [website terms](https://www.nseindia.com/static/nse-terms-of-use) prohibit systematic automated data collection. Its offer/IPO website requests also timed out during research. No private browser JSON endpoints, cookie workarounds or access-control bypasses are implemented. User-owned/downloaded filings remain ingestible. A separate official NSE MCP offering was discovered, but it was not integrated without verifying applicable access and licensing.
- **BSE:** the public-issue page returned HTTP 403. No automatic subscription feed is claimed. Use permitted manual exports or licensed sources.
- **SEBI redistribution:** [website policy](https://www.sebi.gov.in/website-policy.html) addresses permission, attribution and third-party copyright. Original filings are retained privately for local research, not committed or packaged for redistribution. Publication and distribution rights for issuer/BRLM documents also require review.
- **Kite Connect:** [historical candles](https://kite.trade/docs/connect/v3/historical/) exist, but [terms](https://kite.trade/terms/) restrict permanent copying, redistribution and public display. Deferred rather than duplicating the Upstox adapter. No trading interfaces are implemented.
- **Tavily:** documented search exists, but [terms, particularly AI functionality](https://www.tavily.com/terms), restrict investment-related uses. Disabled pending clarification of the applicable service/contract. Search snippets would in any event be discovery candidates, not filing evidence.
- **TrueData:** [official site](https://www.truedata.in/) advertises corporate/fundamental/announcement APIs subject to licences. Corporate API links inspected redirected to a generic product page. No corporate endpoint, schema, entitlement or redistribution contract was verified; a live adapter is deferred.
- **Screener:** useful for manual checks; no official API is assumed.
- **IPO Watch GMP:** no live adapter. Any supplied observations must be labelled unofficial and cannot enter fundamental valuation or guaranteed-return claims.
- **Credit raters, MCA, RBI:** accept original PDFs/public URLs through generic ingestion. There are no invented bulk endpoints or claims to bypass login/CAPTCHA. Automated discovery specific to these services is deferred.

No paid subscriptions were purchased, credentials requested, or live paid-provider calls represented as tested.
