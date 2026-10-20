"""Compare two snapshots and emit change events with every number already computed."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

EXTREME_RATIO = Decimal(5)  # a move of 5x either way is treated as a likely data problem


class Event(BaseModel):
    id: str
    kind: Literal["price_changed", "stock_changed", "new_product", "removed_product", "ai_misquote"]
    url: str
    offer_key: str  # URL plus the JSON-LD SKU, so variants diff separately
    name: str | None = None
    category: str | None = None
    our_sku: str | None = None
    core_sku: bool = False
    currency: str | None = None
    old_price: Decimal | None = None
    new_price: Decimal | None = None
    delta_pct: float | None = None
    old_availability: str | None = None
    new_availability: str | None = None
    our_price: Decimal | None = None
    gap_pct: float | None = None  # competitor price vs ours: negative means they are cheaper
    below_floor: bool | None = None  # True if matching their price would breach our floor
    history: list[str | None] = []
    flags: list[str] = []  # suspected data problems: the agent must reverify first
    surface: str | None = None  # ai_misquote only: which AI answer engine
    retailer: str | None = None  # ai_misquote only: the store the answer names
    live_price: Decimal | None = None  # ai_misquote only: that store's live page price
    own_store: bool | None = None  # ai_misquote only: True if the store is ours


def pct(new: Decimal, old: Decimal) -> float:
    return float(round((new - old) / old * 100, 1))


def _price(row) -> Decimal | None:
    return None if row is None or row["price"] is None else Decimal(row["price"])


def diff(prev: dict, curr: dict, ours: dict[str, dict], history, missed_urls: set[str] = frozenset(),
         removable: set[str] | None = None) -> list[Event]:
    """prev/curr map offer_key -> snapshot row. prev is the latest known row per offer.

    missed_urls were expected but not fetched this run, so they are never reported as removed.
    removable limits removed_product to offers seen in the previous crawl.
    """
    events: list[Event] = []
    keys_per_url: dict[str, set] = {}
    for k, r in [*prev.items(), *curr.items()]:
        keys_per_url.setdefault(r["url"], set()).add(k)
    multi_variant = {u for u, ks in keys_per_url.items() if len(ks) > 1}

    def emit(kind, key, row, **fields):
        # Match a variant exactly (competitor_url "url#sku"), or by URL when the page has a single offer.
        # A URL-only mapping on a multi-variant page keeps the identity but never prices the wrong pack size.
        exact = ours.get(key) or (ours.get(row["url"]) if row["url"] not in multi_variant else None)
        mine = exact or ours.get(row["url"], {})
        ev = Event(id=f"e{len(events) + 1}", kind=kind, url=row["url"], offer_key=key, name=row["name"],
                   category=mine.get("category"), our_sku=mine.get("sku"), core_sku=bool(mine.get("core")),
                   currency=row["currency"], history=history(key), **fields)
        price = ev.new_price if ev.new_price is not None else ev.old_price
        if exact:
            ev.our_price = mine["our_price"]
        if exact and price is not None and not ev.flags:  # no gap on a number we don't trust yet
            ev.gap_pct = pct(price, mine["our_price"])
            ev.below_floor = price < mine["floor_price"]
        events.append(ev)

    for key, row in curr.items():
        old = prev.get(key)
        if old is None:
            emit("new_product", key, row, new_price=_price(row), new_availability=row["availability"])
            continue
        old_p, new_p = _price(old), _price(row)
        flags = []
        if old["currency"] != row["currency"]:
            flags.append("currency_changed")  # never compare GBP to EUR as a "price change"
        if old_p is not None and new_p is None:
            flags.append("price_missing_or_zero")
        if "llm" in (old["extractor"], row["extractor"]):
            flags.append("low_confidence_extraction")
        if old_p and new_p and not flags and (new_p / old_p >= EXTREME_RATIO or old_p / new_p >= EXTREME_RATIO):
            flags.append("extreme_jump")
        if new_p != old_p or "currency_changed" in flags:
            delta = pct(new_p, old_p) if old_p and new_p and "currency_changed" not in flags else None
            emit("price_changed", key, row, old_price=old_p, new_price=new_p, delta_pct=delta, flags=flags,
                 old_availability=old["availability"], new_availability=row["availability"])
        elif old["availability"] != row["availability"]:
            emit("stock_changed", key, row, old_price=old_p, new_price=new_p,
                 old_availability=old["availability"], new_availability=row["availability"])

    for key, row in prev.items():
        if key not in curr and row["url"] not in missed_urls and (removable is None or key in removable):
            emit("removed_product", key, row, old_price=_price(row), old_availability=row["availability"])
    return events
