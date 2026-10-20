"""Render a static demo store from the fixture, for GitHub Pages or any static host.

Usage: uv run python scripts/build_demo_store.py --base-url https://<you>.github.io/<repo> --day 1
Deploy day 1, run `price-agent track`, then rebuild with --day 6, redeploy and track again:
the second run sees real price changes on a site you control.
"""

import argparse
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<script type="application/ld+json">{ld}</script></head>
<body><h1>{title}</h1><p class="price">{price}</p><p class="stock">{stock}</p><a href="{base}/">All products</a></body></html>
"""


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--day", type=int, default=1, choices=range(1, 7))
    parser.add_argument("--out", default=str(ROOT / "demo-store"))
    args = parser.parse_args()
    base, out = args.base_url.rstrip("/"), Path(args.out)

    days = json.loads((ROOT / "fixtures" / "snapshots.json").read_text())["days"]
    offers = days[args.day - 1]["offers"]
    urls = []
    for o in offers:
        url = f"{base}/products/{slug(o['name'])}/"
        ld = {"@context": "https://schema.org", "@type": "Product", "name": o["name"], "sku": o["sku"],
              "offers": {"@type": "Offer", "price": o["price"] or "0.00", "priceCurrency": o["currency"],
                         "availability": "https://schema.org/" + ("InStock" if o["availability"] == "in_stock"
                                                                  else "OutOfStock")}}
        page = out / "products" / slug(o["name"]) / "index.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(PAGE.format(title=html.escape(o["name"]), ld=json.dumps(ld), base=base,
                                    price=f"£{o['price'] or '0.00'}", stock=o["availability"]))
        urls.append(url)

    links = "".join(f'<li><a href="{u}">{u.rstrip("/").rsplit("/", 1)[1]}</a></li>' for u in reversed(urls))
    (out / "index.html").write_text(f"<!doctype html><title>Demo store</title><ul>{links}</ul>")
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>'
                                     f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>')
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n")
    print(f"day {args.day}: {len(urls)} product pages, flat sitemap.xml, in {out}")


if __name__ == "__main__":
    main()
