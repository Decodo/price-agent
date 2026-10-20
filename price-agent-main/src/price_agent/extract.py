"""Deterministic price extraction: schema.org JSON-LD first, a CSS selector second.

Nothing in this module calls an LLM. The optional LLM fallback lives in llm.py and
its results are stored with extractor="llm" so the diff can treat them as low confidence.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import BaseModel
from selectolax.lexbor import LexborHTMLParser as HTMLParser


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    BACKORDER = "backorder"
    LIMITED = "limited"
    DISCONTINUED = "discontinued"
    UNKNOWN = "unknown"


_AVAILABILITY = {
    "instock": Availability.IN_STOCK,
    "instoreonly": Availability.IN_STOCK,
    "onlineonly": Availability.IN_STOCK,
    "limitedavailability": Availability.LIMITED,
    "outofstock": Availability.OUT_OF_STOCK,
    "soldout": Availability.OUT_OF_STOCK,
    "preorder": Availability.PREORDER,
    "presale": Availability.PREORDER,
    "backorder": Availability.BACKORDER,
    "discontinued": Availability.DISCONTINUED,
}


class Offer(BaseModel):
    url: str
    offer_key: str  # URL plus variant SKU, so sizes and pack counts diff separately
    sku: str | None = None
    name: str | None = None
    price: Decimal | None = None  # None means unknown, never "dropped to zero"
    currency: str | None = None
    availability: Availability = Availability.UNKNOWN
    extractor: str = "jsonld"


def parse_price(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    match = re.search(r"\d[\d.,]*", str(value))  # first number only: "$458–$489.99" is 458, not 458489.99
    if match is None or "-" in str(value).split(match.group())[0][-1:]:
        return None
    text = match.group().rstrip(".,")
    if "," in text and "." in text:
        # "1.299,00" (EU) vs "1,299.00" (US): the last separator is the decimal one
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".") if len(text.rsplit(",", 1)[1]) == 2 else text.replace(",", "")
    try:
        price = Decimal(text)
    except InvalidOperation:
        return None
    return price if price > 0 else None


def parse_availability(value) -> Availability:
    if not value:
        return Availability.UNKNOWN
    token = str(value).rstrip("/").rsplit("/", 1)[-1].lower()
    return _AVAILABILITY.get(token, Availability.UNKNOWN)


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return set(t if isinstance(t, list) else [t])


def _walk(data):
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            yield node
            stack.extend(v for k, v in node.items() if k != "offers" and isinstance(v, dict | list))


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _offer_price(offer: dict) -> tuple[Decimal | None, str | None]:
    price = parse_price(offer.get("price") or offer.get("lowPrice"))
    currency = offer.get("priceCurrency")
    for spec in _as_list(offer.get("priceSpecification")):
        if price is None and isinstance(spec, dict):
            price = parse_price(spec.get("price"))
            currency = currency or spec.get("priceCurrency")
    return price, currency


def _jsonld_blocks(tree: HTMLParser) -> list:
    blocks = []
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            blocks.append(json.loads(script.text(), strict=False))
        except json.JSONDecodeError:
            continue
    return blocks


def from_jsonld(url: str, tree: HTMLParser) -> list[Offer]:
    offers: list[Offer] = []
    for block in _jsonld_blocks(tree):
        for node in _walk(block):
            types = _types(node)
            if not types & {"Product", "ProductGroup"} or "hasVariant" in node:
                continue  # A ProductGroup's variants are visited as Products of their own
            for i, offer in enumerate(o for o in _as_list(node.get("offers")) if isinstance(o, dict)):
                price, currency = _offer_price(offer)
                sku = offer.get("sku") or node.get("sku")
                key = sku or node.get("@id") or str(i)
                offers.append(Offer(
                    url=url,
                    offer_key=f"{url}#{key}",
                    sku=str(sku) if sku else None,
                    name=node.get("name"),
                    price=price,
                    currency=currency,
                    availability=parse_availability(offer.get("availability")),
                ))
    return offers


def from_css(url: str, tree: HTMLParser, selectors: dict) -> list[Offer]:
    node = tree.css_first(selectors["price"]) if selectors.get("price") else None
    if node is None:
        return []
    return [Offer(
        url=url,
        offer_key=f"{url}#css",
        price=parse_price(node.text()),
        currency=selectors.get("currency"),
        extractor="css",
    )]


def extract_offers(url: str, html: str, css_selectors: dict | None = None) -> list[Offer]:
    """Return every offer on a product page, or [] for pages with no product data."""
    tree = HTMLParser(html)
    return from_jsonld(url, tree) or from_css(url, tree, css_selectors or {})
