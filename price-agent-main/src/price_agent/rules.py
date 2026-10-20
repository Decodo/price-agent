"""Decide in code what a rule can decide, so the agent only sees events that need judgment.

Each rule mirrors a line in policy.md. Keep the thresholds here and there in sync.
"""

from .diff import Event

NOISE_PCT = 5.0  # policy.md: "A move under 5% on a single SKU is noise"
MATCH_GAP = -5.0  # policy.md: match a core SKU when gap_pct is below -5
MISQUOTE_ALERT_PCT = 5.0  # policy.md: alert on an AI misquote off by 5% or more


def rule_reason(e: Event) -> str | None:
    """The reason a rule logs this event, or None when it needs the agent."""
    if e.flags:
        return None  # suspected data problems always go to the agent, which reverifies them
    if e.kind == "price_changed" and e.delta_pct is not None and abs(e.delta_pct) < NOISE_PCT:
        if e.core_sku and e.gap_pct is not None and e.gap_pct < MATCH_GAP:
            return None  # a small move that leaves a KVI behind may still need a match
        return f"Moved {e.delta_pct:+.1f}%, under the {NOISE_PCT:.0f}% noise threshold."
    if e.kind == "new_product" and not e.our_sku:
        return "New competitor product with no SKU of ours to compare yet."
    if e.kind == "ai_misquote" and e.gap_pct is not None:
        if e.own_store and abs(e.gap_pct) < MISQUOTE_ALERT_PCT:
            return f"Our price quoted {e.gap_pct:+.1f}% off, under the {MISQUOTE_ALERT_PCT:.0f}% alert threshold."
        if not e.own_store and not (e.gap_pct <= -MISQUOTE_ALERT_PCT and e.new_price < e.our_price):
            return f"{e.retailer} quoted {e.gap_pct:+.1f}% from its live price, not a quote shoppers would cite against us."
    return None


def route(events: list[Event]) -> tuple[list[tuple[Event, str]], list[Event]]:
    """Split events into (rule-decided with reasons, events for the agent)."""
    decided, rest = [], []
    for e in events:
        reason = rule_reason(e)
        if reason:
            decided.append((e, reason))
        else:
            rest.append(e)
    return decided, rest
