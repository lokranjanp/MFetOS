"""Source support checks for manually mapped exchange observations."""

from .models import Subscription


def subscription_matches(observation: Subscription, evidence: list[dict]) -> bool:
    expected = (
        f"{observation.category} scope {observation.scope} bids {observation.bids} offered {observation.offered} "
        f"{observation.unit} anchors {observation.anchor_treatment} observed {observation.observed_at.isoformat()}"
    )
    return any(e and expected.lower() in " ".join(e["snippet"].lower().split()) for e in evidence)
