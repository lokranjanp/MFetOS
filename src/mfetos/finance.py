"""Deterministic formula library. None is never converted to zero."""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from decimal import Decimal, InvalidOperation

from .models import Metric

D = Decimal
SCALES = {
    "inr": D(1),
    "rupees": D(1),
    "lakh": D(100000),
    "lakhs": D(100000),
    "crore": D(10000000),
    "crores": D(10000000),
    "million": D(1000000),
    "millions": D(1000000),
    "billion": D(1000000000),
    "shares": D(1),
    "percent": D(1),
}


def number(raw: str) -> Decimal | None:
    s = raw.strip().replace(",", "").replace("−", "-")
    if s.lower() in {"", "-", "—", "na", "n/a", "nil", "[●]", "[•]"}:
        return None
    s = re.sub(r"[₹%*]", "", s).strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        v = D(s)
        return v if v.is_finite() else None
    except InvalidOperation:
        return None


def convert(value: Decimal, scale: str) -> Decimal:
    return value * SCALES[scale.lower()]


def adjust_share_count(shares: Decimal, split_factor: Decimal, bonus_new_per_old: Decimal) -> Decimal:
    """Caller must provide evidenced, not-already-applied corporate action terms."""
    if shares <= 0 or split_factor <= 0 or bonus_new_per_old < 0:
        raise ValueError("Invalid split/bonus terms")
    return shares * split_factor * (1 + bonus_new_per_old)


def normalised_earnings(
    reported: Decimal | None, after_tax_adjustments: list[Decimal | None]
) -> Decimal | None:
    """Explicit signed AFTER-tax adjustments only; unknown tax effects block normalisation."""
    if reported is None or any(a is None for a in after_tax_adjustments):
        return None
    return reported + sum(after_tax_adjustments, D(0))


def shares_from_tranches(
    pre: Decimal, price: Decimal, tranches: list[tuple[Decimal, Decimal]], incremental_diluted: Decimal
) -> Decimal:
    """(gross primary proceeds, discount per share); OFS is never a primary tranche."""
    if pre <= 0 or incremental_diluted < 0 or not tranches:
        raise ValueError("Invalid or missing issuance tranches")
    total = pre + incremental_diluted
    for proceeds, discount in tranches:
        if proceeds < 0 or discount < 0 or price <= discount:
            raise ValueError("Invalid discounted issuance tranche")
        total += proceeds / (price - discount)
    return total


def ratio(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return a / b if a is not None and b is not None and b > 0 else None


def cagr(first: Decimal | None, last: Decimal | None, start: date, end: date) -> Decimal | None:
    if first is None or last is None or first <= 0 or last <= 0 or end <= start:
        return None
    years = D(end.year - start.year)
    anniversary = date(end.year, start.month, min(start.day, monthrange(end.year, start.month)[1]))
    years += D((end - anniversary).days) / D("365.25")
    return (last / first) ** (D(1) / years) - 1 if years > 0 else None


def proceeds_bridge(
    cash: Decimal, debt: Decimal, fresh: Decimal, expenses: Decimal, repayment: Decimal, other_uses: Decimal
) -> tuple[Decimal, Decimal]:
    if min(cash, debt, fresh, expenses, repayment, other_uses) < 0 or repayment > debt:
        raise ValueError("Invalid proceeds/debt bridge")
    if expenses + repayment + other_uses > fresh + cash:
        raise ValueError("Uses exceed available cash")
    return cash + fresh - expenses - repayment - other_uses, debt - repayment


def post_issue_shares(
    pre: Decimal,
    price: Decimal,
    fresh: Decimal | None = None,
    new_shares: Decimal | None = None,
    incremental_diluted: Decimal = D(0),
) -> Decimal:
    if pre <= 0 or price <= 0 or incremental_diluted < 0:
        raise ValueError("Invalid share count or price")
    if fresh is None and new_shares is None:
        raise ValueError("Fresh issuance amount or share count required, including explicit zero for OFS")
    issued = new_shares if new_shares is not None else fresh / price
    if issued < 0:
        raise ValueError("Negative issuance")
    return pre + issued + incremental_diluted


def metric(
    name: str,
    value: Decimal | None,
    formula: str,
    inputs: list[str],
    unit: str = "ratio",
    period: str | None = None,
    reason: str | None = None,
) -> Metric:
    return Metric(
        name=name,
        value=value,
        formula=formula,
        input_ids=inputs,
        unit=unit,
        period=period,
        unavailable_reason=(reason or "Missing input or non-meaningful denominator")
        if value is None
        else None,
    )
