"""Build fixtures/snapshots.json from a real crawl, then plant the demo scenarios on the last day.

Usage: uv run python scripts/make_fixture.py crawl_pages.json
crawl_pages.json maps URL -> crawl page object (status, content, ...) from GET /v1/crawl/{id}?type=content.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

from price_agent.extract import Availability, extract_offers

ROOT = Path(__file__).resolve().parents[1]
NEW_ON_LAST_DAY = "Mewtwo"
PLANTED = {  # name -> (new price or None, new availability or None)
    "Pikachu": (Decimal("35.89"), None),       # 1. a 3% drop on one SKU
    "Charmander": (Decimal("39.36"), None),    # 2. an 18% drop across the Fire category
    "Charizard": (Decimal("127.92"), None),
    "Vulpix": (Decimal("70.55"), None),
    "Growlithe": (Decimal("24.90"), None),
    "Squirtle": (None, Availability.OUT_OF_STOCK),  # 3. out of stock where they were cheaper
    "Bulbasaur": (Decimal("630.00"), None),    # 4a. a misplaced decimal on the page, not a market move
    "Mew": (Decimal("0"), None),               # 4b. a page that shows a price of 0.00
}


def main(dump: str) -> None:
    pages = json.loads(Path(dump).read_text())
    offers = [o for url, page in sorted(pages.items()) if page["status"] == "done"
              for o in extract_offers(url, page["content"])]
    new_product = next(o for o in offers if o.name == NEW_ON_LAST_DAY)
    ours = {line.split(",")[1] for line in (ROOT / "our_prices.csv").read_text().splitlines()[1:]}
    rest = [o for o in offers if o.name != NEW_ON_LAST_DAY]
    baseline = ([o for o in rest if o.name in ours | set(PLANTED)] + [o for o in rest if o.name not in ours | set(PLANTED)])[:150]
    assert all(any(o.name == name for o in baseline) for name in PLANTED), "planted SKU missing"

    days = []
    for d in range(5):  # five real, unchanged days of history
        days.append({"crawl_id": f"fixture-day{d + 1}", "fetched_at": f"2026-10-0{d + 5}T06:00:00+00:00",
                     "offers": [o.model_dump(mode="json") for o in baseline]})
    last = []
    for o in baseline + [new_product]:
        row = o.model_dump(mode="json")
        if o.name in PLANTED:
            price, availability = PLANTED[o.name]
            if price is not None:
                row["price"] = str(price) if price > 0 else None  # extraction maps 0.00 to unknown
            if availability is not None:
                row["availability"] = availability.value
        last.append(row)
    days.append({"crawl_id": "fixture-day6", "fetched_at": "2026-10-10T06:00:00+00:00", "offers": last})

    out = ROOT / "fixtures" / "snapshots.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"source": "scrapeme.live crawl, planted changes on day 6", "days": days}, indent=1))
    print(f"wrote {out}: {len(baseline)} products x 5 days + day 6 with {len(PLANTED) + 1} planted changes")


if __name__ == "__main__":
    main(sys.argv[1])
