"""The AI shelf: what AI shopping answers tell shoppers your product costs.

Shoppers increasingly ask Google AI Mode or ChatGPT where to buy. This step asks the same question
through Decodo's AI templates, extracts the (retailer, price) pairs, and compares the price quoted
for your store with your live product page. An LLM does the extraction, so Python keeps only prices
that appear verbatim in the answer text.
"""

import os
import re
import unicodedata
from decimal import Decimal

import anthropic
import httpx
from decodo.errors import DecodoError
from pydantic import BaseModel

from .diff import Event, pct
from .extract import extract_offers, parse_price

SURFACES = ("google_ai_mode", "chatgpt", "gemini")  # override per product with `surfaces`
MISQUOTE_PCT = 1.0  # quotes within 1% of the live price count as correct


class Quote(BaseModel):
    retailer: str
    price_text: str  # copied from the answer, e.g. "$378.00"


class Quotes(BaseModel):
    quotes: list[Quote]


def normalize(answer: str) -> str:
    """AI answers can print prices as bold math digits ("$ 𝟒𝟓𝟖") or spaced digits ("$ 3 9 9")."""
    text = unicodedata.normalize("NFKC", answer)
    return re.sub(r"([$£€])\s?(\d(?: \d){1,7}\b|\d)", lambda m: m.group(1) + m.group(2).replace(" ", ""), text)


def extract_quotes(answer: str) -> list[tuple[str, Decimal]]:
    answer = normalize(answer)
    response = anthropic.Anthropic(max_retries=4).messages.parse(
        model=os.environ.get("PRICE_AGENT_EXTRACT_MODEL") or "claude-haiku-5-5",
        max_tokens=2048,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": "List every retailer and the price this answer quotes for it. "
                                              "Copy each price exactly as written, with its currency symbol. "
                                              f"Skip numbers without a currency symbol.\n\n{answer}"}],
        output_format=Quotes,
    )
    found = response.parsed_output.quotes if response.parsed_output else []
    return verified([(q.retailer, q.price_text) for q in found], answer)


def verified(quotes: list[tuple[str, str]], answer: str) -> list[tuple[str, Decimal]]:
    """Keep a quote only if the answer shows that exact number as a price with a currency symbol."""
    text = re.sub(r"\s+", " ", normalize(answer))
    kept = []
    for retailer, price_text in quotes:
        number = re.search(r"\d[\d.,]*\d|\d", price_text)
        price = parse_price(price_text)
        pattern = rf"[$£€]\s?{re.escape(number.group())}(?![.,]?\d)" if number else None
        if price and pattern and re.search(pattern, text):
            kept.append((retailer, price))
    return kept


def live_price(decodo, source: dict, geo: str, locale: str) -> Decimal | None:
    """A retailer's live price: JSON-LD from its product page, or a Decodo template's parsed price."""
    if "url" in source:
        page = decodo.scrape(source["url"], geo=geo, locale=locale)
        offer = next(iter(extract_offers(source["url"], page["content"])), None)
        return offer.price if offer else None
    body = {k: v for k, v in source.items() if k != "retailer"} | {"parse": True}
    price = decodo.template(body).get("price")
    return parse_price(price.get("price") if isinstance(price, dict) else price)


def grid(samples: list[tuple[str, list[tuple[str, Decimal]]]], live: dict[str, Decimal]) -> list[str]:
    """One row per AI answer, one column per retailer with a live price, and a below/above count."""
    stores = [r for r, p in live.items() if p]
    cell = lambda v: f"{v:>9.2f}" if isinstance(v, Decimal) else f"{v:>9}"  # noqa: E731
    lines = [f"{'':<16}" + "".join(cell(r) for r in stores), f"{'live':<16}" + "".join(cell(live[r]) for r in stores)]
    below = above = total = 0
    for label, quotes in samples:
        row = []
        for store in stores:
            quote = next((p for r, p in quotes if store.lower() in r.lower()), None)
            row.append(cell(quote if quote is not None else "-"))
            if quote is not None:
                total += 1
                below += quote < live[store] * Decimal("0.99")
                above += quote > live[store] * Decimal("1.01")
        lines.append(f"{label:<16}" + "".join(row))
    lines.append(f"{below} of {total} quotes for these {len(stores)} stores below the live price, {above} above")
    return lines


def check_shelf(cfg: dict, decodo, store) -> list[Event]:
    events: list[Event] = []
    for item in cfg.get("ai_shelf", []):
        page = decodo.scrape(item["product_url"], geo=item["geo"], locale=item["locale"])
        live = next(iter(extract_offers(item["product_url"], page["content"])), None)
        if live is None or live.price is None:
            print(f"[ai-shelf] no live price on {item['product_url']}, skipping")
            continue
        # Live prices for the retailers the answers name, so every AI quote is checked against a real page
        live_prices = {item["retailer"]: live.price}
        for source in item.get("live", []):
            try:
                live_prices[source["retailer"]] = live_price(decodo, source, item["geo"], item["locale"])
            except Exception as err:  # one unreachable retailer shouldn't stop the check
                print(f"[ai-shelf] live price for {source['retailer']} unavailable ({type(err).__name__})")
        samples = []
        for surface in item.get("surfaces", SURFACES):
            # AI answers vary between asks, so sample each surface and act only on a consistent misquote
            answers = []
            for n in range(1, item.get("samples", 2) + 1):
                try:
                    answer = decodo.ask_ai(surface, item["question"], item["geo"])
                    quotes = extract_quotes(answer.get("response_text", ""))
                except (anthropic.APIError, httpx.HTTPError, DecodoError) as err:
                    print(f"[ai-shelf] {surface}: sample skipped ({type(err).__name__})")
                    answers.append(None)  # an incomplete sample set never raises an event
                    continue
                store.save_ai_quotes(surface, item["question"], quotes)
                samples.append((f"{surface} {n}", quotes))
                answers.append(quotes)
            # Our store and each competitor with a live price: every sample must quote it, and every quote miss
            for retailer, live_p in live_prices.items():
                seen = [None if a is None else next((p for r, p in a if retailer.lower() in r.lower()), None)
                        for a in answers]
                if not live_p or None in seen:  # an incomplete sample set changes nothing
                    continue
                key = f"{surface}:{retailer}:{item['product_url']}"
                misquoted = all(abs(pct(q, live_p)) >= MISQUOTE_PCT for q in seen)
                previous = store.misquote_state(key)
                store.set_misquote_state(key, seen[-1] if misquoted else None)
                if not misquoted:
                    continue
                if previous is not None and Decimal(previous) == seen[-1]:  # like a diff: only a change is an event
                    print(f"[ai-shelf] {surface} {retailer} {seen[-1]}: same misquote as the last check, not re-sent")
                    continue
                own = retailer == item["retailer"]
                events.append(Event(
                    id=f"a{len(events) + 1}", kind="ai_misquote", url=item["product_url"],
                    offer_key=key, name=item["name"], surface=surface,
                    retailer=retailer, own_store=own, currency=live.currency, our_price=live.price,
                    live_price=live_p, new_price=seen[-1], gap_pct=pct(seen[-1], live_p),
                    history=[str(q) for q in seen], core_sku=item.get("core", False)))
        print("\n".join(grid(samples, live_prices)))
    return events
