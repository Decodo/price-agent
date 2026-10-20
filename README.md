# price-agent

A price-monitoring agent that decides, not just logs. It uses the [Decodo Web Data API](https://decodo.com/web-data-api) to find new products with `/map`, track competitor prices with `/crawl`, and recheck pages and AI answers with `/scrape`. Python extracts prices and finds what changed. Simple rules log routine changes, and an LLM agent decides the rest: log it, alert a person, propose a price change for approval, or recheck the page.

The agent works only with numbers that Python computed in `diff.py`. It never reads raw HTML. `rules.py` logs the events a rule can decide, such as a price change under 5%, so the agent sees only the events that need judgment. The agent proposes, and a person approves. Nothing in this repo writes to a live pricing system.

## Quickstart

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), a [Decodo API key](https://decodo.com/web-data-api/pricing), and an [Anthropic API key](https://platform.claude.com/).

```bash
uv sync
cp .env.example .env   # add DECODO_API_KEY and ANTHROPIC_API_KEY
uv run price-agent demo
```

`demo` replays `fixtures/snapshots.json`: 5 days of real prices for 150 products from a `/crawl` of the [ScrapeMe](https://scrapeme.live/shop/) test store, then a 6th day with deliberate test changes. The diff finds 9 events, and the agent decides on them. The agent can recheck a page through `/scrape`, so the demo needs both keys. To see the events without calling the LLM, run `uv run price-agent demo --no-agent`.

## Run it on a live store

```bash
uv run price-agent discover   # find new products with /map, fetch them with /scrape
uv run price-agent track      # crawl your batches with /crawl, diff, decide
uv run price-agent ai-shelf   # compare the prices in AI answers with live prices
uv run price-agent eval       # test the agent's decisions against expected ones
```

- **`discover`** maps the shop page and the shop page sorted by newest, so it finds new products in any section. The first run only stores the catalog. Later runs fetch each new URL with `/scrape` (up to `new_product_limit` per run) and create a `new_product` event.
- **`track`** crawls each batch of URLs and saves the prices to `prices.db`. The first run stores a baseline, and a run with no changes ends before any LLM call. A page without product data, such as a login page or a redesigned page, counts as a missing page, not a removed product. When too many pages are missing, Python alerts a person.
- **`our_prices.csv`** links your SKUs to competitor URLs, with your price, your price floor, a category, and a `core` column that marks your key value items (KVIs). On a page with several variants, set `competitor_url` to `url#sku` so each variant is compared with the same variant you sell.
- **`policy.md`** is the agent's system prompt. Edit it and `our_prices.csv` to match your business.
- **Alerts** print to stdout, or go to Slack when `SLACK_WEBHOOK_URL` is set. Create the Slack app from [`docs/slack-app-manifest.yml`](docs/slack-app-manifest.yml). Proposals are saved in the `proposals` table with the status `pending_approval`.

Track only public pages, don't log in with an account whose terms you accepted, and stop tracking any site that asks you to.

## Configuration

Settings live in `config.toml`:

| Key | What it sets |
|---|---|
| `seed`, `newest_listing` | The shop page and the shop page sorted by newest, for `/map` |
| `select_paths`, `exclude_paths` | Path regexes for the product pages to keep and the pages to skip |
| `sitemap = "only"` | Take product URLs from the sitemap, so cart and account links aren't crawled |
| `shards`, `limit` | The batches to crawl, one URL-path regex each, and the page limit for each `/crawl` job |
| `geo`, `locale` | The country (full name) and language for every request, including rechecks |
| `new_product_limit` | The most new URLs `discover` fetches per run (default 5) |
| `coverage_alert` | The share of missing pages in a run that triggers an alert (default 0.2) |
| `freshness_alert_hours` | Alert when a page in `our_prices.csv` hasn't been fetched for this long (default 48). A page limit can skip the same pages on every run, and this catches them |
| `[css_fallback]` | A price selector for stores whose pages have no JSON-LD |
| `[extraction] llm_fallback` | Off by default. When on, an LLM extracts the price only if JSON-LD and the CSS selector both fail |

## Check the prices in AI answers

`ai-shelf` asks Google AI Mode, ChatGPT, and Gemini where to buy each product you list under `[[ai_shelf]]` in `config.toml`, through the Decodo `/scrape` templates.

- An LLM extracts each retailer's price, and Python keeps only prices that appear in the answer with a currency symbol.
- `[[ai_shelf.live]]` entries give each store's live price, from a product page with JSON-LD or a Decodo template such as `amazon_product` or `walmart_product`. The run prints every AI price next to the live price.
- Answers change each time you ask, so each AI search tool is asked twice (`samples = 2`). An `ai_misquote` event is created only when every answer prices a store at least 1% off its live price.
- For your own store, the policy alerts the team. For a competitor, it never proposes a match, because a shopper can't buy at a price that only appears in an AI answer.

## Choose the model

`eval` sends 17 events through the same rules and agent as a live run, and compares each decision with `fixtures/expected_decisions.json`. The events cover the demo's test day (9 events, including 2 that need a recheck), 2 new products, and 6 wrong prices in AI answers (4 from live runs and 2 edge cases). Rules decide 6 events, and the agent decides the other 11. If the Claude API is unavailable, `eval` reports `API error` instead of a wrong decision.

In 3 runs each, Claude Haiku 5.5 and Claude Opus 5.5 both made every expected decision, at about $0.0036 and $0.129 a run. Haiku is the default. Set `PRICE_AGENT_MODEL` to change it.

By default, `eval` tests only the configured model, 3 times. Re-run it after every change to `policy.md`. To compare models:

```bash
uv run price-agent eval --models claude-haiku-5-5,claude-sonnet-5-5,claude-opus-5-5
```

## Scheduling

`.github/workflows/track.yml` runs `track` and commits `prices.db` back to the repo, because each new runner starts with no price history. Add `DECODO_API_KEY`, `ANTHROPIC_API_KEY`, and optionally `SLACK_WEBHOOK_URL` as repository secrets, then run it from the Actions tab. To run it daily, uncomment the `schedule` lines in the workflow. To use cron instead:

```text
17 6 * * * cd /path/to/price-agent && uv run price-agent track >> track.log 2>&1
```

For products whose prices change often, add an hourly job that crawls only their batches:

```bash
uv run price-agent track --shards '<regex>,<regex>'
```

**Webhooks instead of polling.** On a host with a public URL, set `[webhook] public_url` and `WEBHOOK_SECRET`, run `uv run price-agent serve`, and start runs with `uv run price-agent track --webhook`. Each `/crawl` callback carries `crawl_id`, `status`, and your `passthrough`. The receiver checks the secret, ignores unknown runs and repeated callbacks, saves the batch, and starts the next one. CI runners can't receive webhooks, so polling is the default.

## MCP server

```bash
uv run --extra mcp python -m price_agent.mcp_server
```

A read-only stdio MCP server over `prices.db` with 4 tools: `pending_proposals`, `recent_decisions`, `price_history`, and `ai_shelf_quotes`. It opens the database read-only and has no approve tool. It needs the MCP Python SDK 2.x.

## Build a demo store you control

`scripts/build_demo_store.py` turns the fixture into a static store, with product pages that carry JSON-LD, a flat `sitemap.xml`, and `robots.txt`, for GitHub Pages or any static host:

```bash
uv run python scripts/build_demo_store.py --base-url https://<you>.github.io/<repo> --day 1
```

Point `config.toml` at your store (`seed`, `select_paths = ["^/products/[^/]+/?$"]`, one batch) and update the URLs in `our_prices.csv`. Deploy day 1 and run `track`. Then rebuild with `--day 6`, redeploy, and run `track` again.

## Tests

```bash
uv run --group dev pytest
```

The tests cover JSON-LD extraction on a real WooCommerce page, Shopify-style variants, the CSS fallback, zero, broken, and range prices, the 9 fixture events, currency changes, LLM-extracted prices, crawl pagination, the AI price checks, `/map` new-product events, rule routing, and the webhook's secret, unknown-run, and repeat handling.

## Project layout

| File | What it does |
|---|---|
| `src/price_agent/decodo.py` | The Decodo calls (`/map`, `/crawl`, `/scrape`, templates) through the official `decodo-sdk` |
| `src/price_agent/extract.py` | JSON-LD and CSS extraction into `Offer` records |
| `src/price_agent/llm.py` | The optional LLM extraction fallback |
| `src/price_agent/store.py` | SQLite: catalog, snapshots, crawls, decisions, proposals |
| `src/price_agent/diff.py` | Change events with `delta_pct`, `gap_pct`, `below_floor`, history, and flags |
| `src/price_agent/rules.py` | Logs the events a rule can decide, so the agent sees only what needs judgment |
| `src/price_agent/agent.py` | The tool-calling agent, its guardrails, and rechecks |
| `src/price_agent/ai_shelf.py` | The AI answer check: ask, extract, verify, `ai_misquote` events |
| `src/price_agent/evals.py` | The decision tests against `fixtures/expected_decisions.json` |
| `src/price_agent/webhook.py` | The `/crawl` callback receiver that runs the batches in order |
| `src/price_agent/mcp_server.py` | The read-only MCP server over `prices.db` |
| `src/price_agent/cli.py` | The `discover`, `track`, `demo`, `ai-shelf`, `eval`, and `serve` commands |
| `scripts/make_fixture.py` | Builds `fixtures/snapshots.json` from a real crawl and adds the test day |
| `scripts/build_demo_store.py` | Builds the static demo store |
| `docs/architecture.d2` | The source of the diagram above, with its render commands |
