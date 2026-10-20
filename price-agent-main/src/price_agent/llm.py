"""Last-resort extraction for pages with no JSON-LD and no matching selector.

This is a separate call from the deciding agent. Its output is stored with extractor="llm",
and diff.py flags any change built on it, so a guessed number never reaches a decision unmarked.
"""

import os

import anthropic
from pydantic import BaseModel
from selectolax.lexbor import LexborHTMLParser

from .extract import Offer, parse_availability, parse_price


class PriceOnPage(BaseModel):
    product_name: str | None
    price: str | None  # as printed on the page, e.g. "£37.00"; parsed in Python, not by the model
    currency: str | None
    in_stock: bool | None


def llm_extract(url: str, html: str) -> list[Offer]:
    tree = LexborHTMLParser(html)
    for tag in tree.css("script, style, noscript, svg"):
        tag.decompose()
    text = tree.body.text(separator="\n", strip=True) if tree.body else ""
    response = anthropic.Anthropic().messages.parse(
        model=os.environ.get("PRICE_AGENT_EXTRACT_MODEL") or "claude-haiku-5-5",
        max_tokens=1024,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": f"Copy the main product's price exactly as printed. "
                                              f"Use null if there is no single product price.\n\n{text}"}],
        output_format=PriceOnPage,
    )
    found = response.parsed_output
    if found is None or found.price is None:
        return []
    availability = parse_availability({True: "InStock", False: "OutOfStock"}.get(found.in_stock))
    return [Offer(url=url, offer_key=f"{url}#llm", name=found.product_name, price=parse_price(found.price),
                  currency=found.currency, availability=availability, extractor="llm")]
