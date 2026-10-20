"""price-agent discover | track | demo | ai-shelf | eval | serve"""

import argparse
import hashlib
import json
import os
import re
import tomllib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .decodo import Decodo
from .diff import diff
from .extract import extract_offers
from .store import Store, now

ROOT = Path(__file__).resolve().parents[2]


def discover(cfg: dict, store: Store, decodo: Decodo, agent_enabled: bool = True) -> None:
    """List product URLs with /map, then fetch and decide on the ones never seen before.

    The crawl covers only your shards. The newest-first listing shows a new SKU anywhere in the store.
    """
    t = cfg["target"]
    params = dict(select_paths=t["select_paths"], exclude_paths=t["exclude_paths"], geo=t["geo"], locale=t["locale"])
    urls, _ = decodo.map(t["seed"], **params)
    newest, _ = decodo.map(t["newest_listing"], sitemap="skip", **params)
    first_map = not store.has_map_run()
    new = store.upsert_catalog(sorted(set(urls) | set(newest)), source="map")
    print(f"/map: {len(set(urls) | set(newest))} product URLs on the 2 listing pages, {len(new)} new")
    # Newest-listing order first, so the cap keeps the newest arrivals
    new = sorted(new, key=lambda u: newest.index(u) if u in newest else len(newest))[: t.get("new_product_limit", 5)]
    run_id = f"map-{datetime.now(UTC).isoformat()}"  # microseconds: two maps a second apart stay distinct
    store.start_run(run_id, [], set() if first_map else set(new))  # no shards: never makes an offer "removed"
    if first_map:
        print("First /map stored. URLs new on the next /map become new_product events.")
    if first_map or not new:
        return
    for url in new:
        page = decodo.scrape(url, geo=t["geo"], locale=t["locale"])
        for offer in extract_offers(url, page["content"], cfg.get("css_fallback")):
            store.save_snapshot(run_id, now(), offer, None, t["geo"])
    store.commit()
    snapshot = store.snapshot(run_id)
    print(f"/scrape: {len({r['url'] for r in snapshot.values()})} of {len(new)} new URLs had product data")
    decide(cfg, store, decodo, diff({}, snapshot, store.our_prices_by_url(), store.history), {}, agent_enabled)


def new_run(cfg: dict, store: Store) -> str:
    """Open a run and record its expected URLs: what /map and earlier crawls know, inside the shards."""
    run_id = f"run-{now()}"
    shards = cfg["target"]["shards"]
    store.start_run(run_id, shards,
                    {u for u in store.catalog_urls() if any(re.search(sh, urlsplit(u).path) for sh in shards)})
    return run_id


def start_shard(cfg: dict, decodo: Decodo, shard: str, **callback) -> str:
    t = cfg["target"]
    return decodo.start_crawl(
        t["seed"], sitemap=t["sitemap"], select_paths=[shard], exclude_paths=t["exclude_paths"],
        limit=t["limit"], output="html", geo=t["geo"], locale=t["locale"], **callback,
    )


def ingest(cfg: dict, store: Store, decodo: Decodo, crawl_id: str, run_id: str, status: dict) -> None:
    """Save every page of a finished crawl right away, so each run keeps its own copy of the data."""
    faulted, unparsed, saved = set(), set(), 0
    for page in decodo.crawl_pages(crawl_id):
        if page["status"] != "done":
            faulted.add(page["url"])
            continue
        offers = extract_offers(page["url"], page["content"], cfg.get("css_fallback"))
        if not offers and cfg.get("extraction", {}).get("llm_fallback"):
            from .llm import llm_extract

            offers = llm_extract(page["url"], page["content"])
        if not offers:
            # A login page, a paywall, or a redesigned page has no Product data: a missing page, not a removal
            unparsed.add(page["url"])
            continue
        html_hash = hashlib.sha256(page["content"].encode()).hexdigest()
        for offer in offers:
            store.save_snapshot(run_id, page["updated_at"], offer, html_hash, cfg["target"]["geo"])
        saved += 1
    store.commit()
    store.add_shard_result(run_id, faulted, unparsed, status.get("discovered", 0) >= cfg["target"]["limit"])
    print(f"/crawl {crawl_id}: {status['status']}, {saved} product pages saved, {len(faulted)} failed")


def finish_run(cfg: dict, store: Store, decodo: Decodo, run_id: str, agent_enabled: bool = True) -> None:
    run, fetched = store.run(run_id), store.fetched_urls(run_id)
    store.upsert_catalog(sorted(fetched), source="crawl")
    # Coverage: anything expected but not fetched is a gap, not a removal.
    missed = ((run["expected"] - fetched) | run["faulted"] if run["capped"] else run["faulted"]) | run["unparsed"]
    store.save_crawl(run_id, {"status": "done", "completed": len(fetched), "failed": len(run["faulted"])},
                     sorted(missed))
    print(f"total: {len(fetched)} product pages saved, {len(run['faulted'])} faulted, "
          f"{len(run['unparsed'])} without product data")
    print(f"coverage: {len(run['expected'] - fetched)} of {len(run['expected'])} expected catalog URLs not fetched")
    # A site that stops serving product data empties the diff, so it would read as a quiet day. This check is a threshold, not a
    # judgment, so Python alerts a human directly.
    # A crawl that hit its page limit skipped the rest by design, so only an uncapped run counts unfetched URLs
    gaps = run["faulted"] | run["unparsed"] | (set() if run["capped"] else run["expected"] - fetched)
    share = len(gaps) / max(len(gaps | fetched), 1)
    if share >= cfg["target"].get("coverage_alert", 0.2):
        text = (f"[high] Coverage alert from the price agent: {len(gaps)} of {len(gaps | fetched)} pages in {run_id} "
                f"came back faulted, without product data, or not at all. Check whether the site changed or stopped serving "
                f"product data before trusting today's diff.")
        store.log_decision("alert_human", [], {"urgency": "high", "reason": text, "gaps": sorted(gaps)[:20]})
        notify(text)

    covers = lambda shards, url: any(re.search(sh, urlsplit(url).path) for sh in shards)  # noqa: E731
    # A page limit can skip the same tracked pages on every run, so alert when a page you price against
    # hasn't been fetched within the window.
    tracked = {u.split("#")[0] for u in store.our_prices_by_url()}
    tracked = {u for u in tracked if covers(run["shards"], u)}
    if tracked:
        hours = cfg["target"].get("freshness_alert_hours", 48)
        seen, now_utc = store.last_seen(tracked), datetime.now(UTC)
        ages = {u: (now_utc - seen[u]).total_seconds() / 3600 for u in seen}
        stale = sorted(u for u in tracked if u not in ages or ages[u] > hours)
        if ages:
            print(f"freshness: {len(ages)} of {len(tracked)} tracked pages seen, oldest {max(ages.values()):.0f}h ago")
        if stale:
            text = (f"[normal] Freshness alert from the price agent: {len(stale)} of {len(tracked)} tracked competitor pages "
                    f"haven't been seen in {hours} hours: {', '.join(stale[:5])}. Add a batch or raise the page limit.")
            store.log_decision("alert_human", [], {"urgency": "normal", "reason": text, "stale": stale[:20]})
            notify(text)

    previous = [c for c in store.crawl_ids() if c != run_id and not c.startswith("map-")]
    if not previous:
        print("First crawl stored. It is tomorrow's baseline.")
        return
    # Compare only offers inside this run's shards, so an hourly hot-SKU run doesn't "remove" the rest
    baseline = {k: r for k, r in store.latest_rows(run_id).items() if covers(run["shards"], r["url"])}
    # An offer can be removed only if the latest earlier run that covered its URL still saw it: fires once
    last_cover = {}
    for k, r in baseline.items():
        last_cover[k] = next((p for p in reversed(previous)
                              if not store.has_run(p) or covers(store.run(p)["shards"], r["url"])), None)
    removable = {k for k, r in baseline.items() if r["crawl_id"] == last_cover[k]}
    events = diff(baseline, store.snapshot(run_id), store.our_prices_by_url(), store.history, missed, removable)
    decide(cfg, store, decodo, events, baseline, agent_enabled)


def track(cfg: dict, store: Store, decodo: Decodo, agent_enabled: bool = True) -> None:
    """Daily, polling: crawl each shard in turn, save it, then check coverage, diff, and decide."""
    run_id = new_run(cfg, store)
    for shard in cfg["target"]["shards"]:
        crawl_id = start_shard(cfg, decodo, shard)
        ingest(cfg, store, decodo, crawl_id, run_id, decodo.wait_for_crawl(crawl_id))
    finish_run(cfg, store, decodo, run_id, agent_enabled)


def notify(text: str) -> None:
    """Print an alert, and post it to Slack when SLACK_WEBHOOK_URL is set."""
    print(text)
    if url := os.environ.get("SLACK_WEBHOOK_URL"):
        import httpx

        httpx.post(url, json={"text": text})


def decide(cfg, store, decodo, events, baseline, agent_enabled=True) -> None:
    print(f"{len(events)} change events: {dict(Counter(e.kind for e in events))}")
    if not events:
        print("Nothing changed. No LLM call.")
        return
    if not agent_enabled:
        print(json.dumps([e.model_dump(mode="json", exclude_none=True) for e in events], indent=1))
        return
    from .rules import route

    decided, events = route(events)  # what a rule can decide never reaches the LLM
    for e, reason in decided:
        store.log_decision("log_only", [e.id], {"reason": reason, "decided_by": "rule"})
        print(f"[rule: log_only] {e.id} {e.name}: {reason}")
    if not events:
        print("No event needs judgment. No LLM call.")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set. Add it to .env, or rerun with --no-agent to print the events.")
    from .agent import Agent

    fetch_params = {"geo": cfg["target"]["geo"], "locale": cfg["target"]["locale"]}
    Agent(store, decodo, fetch_params, baseline, os.environ.get("SLACK_WEBHOOK_URL")).run(events)


def demo(cfg: dict, store: Store, decodo: Decodo, agent_enabled: bool = True) -> None:
    """Replay the test fixture: 5 days of real prices plus a day with deliberate test changes."""
    from .evals import seeded_day

    events, baseline = seeded_day(store, cfg["target"]["geo"])
    decide(cfg, store, decodo, events, baseline, agent_enabled)


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


def ai_shelf(cfg: dict, store: Store, decodo: Decodo, agent_enabled: bool = True) -> None:
    """Weekly: check what Google AI Mode and ChatGPT quote for the products in [[ai_shelf]]."""
    from .ai_shelf import check_shelf

    decide(cfg, store, decodo, check_shelf(cfg, decodo, store), {}, agent_enabled)


def main() -> None:
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(prog="price-agent")
    parser.add_argument("command", choices=["discover", "track", "demo", "ai-shelf", "eval", "serve"])
    parser.add_argument("--config", default=str(ROOT / "config.toml"))
    parser.add_argument("--no-agent", action="store_true", help="print events instead of calling the LLM")
    parser.add_argument("--models", help="eval: comma-separated model IDs to compare (default: the agent's model only, "
                                          "e.g. claude-haiku-5-5,claude-sonnet-5-5,claude-opus-5-5)")
    parser.add_argument("--runs", type=int, default=3, help="eval: runs per model")
    parser.add_argument("--shards", help="track: comma-separated shard regexes, overriding config.toml")
    parser.add_argument("--webhook", action="store_true", help="track: start shard 0 and let callbacks drive the run")
    parser.add_argument("--port", type=int, default=8080, help="serve: port for /crawl callbacks")
    args = parser.parse_args()

    cfg = tomllib.loads(Path(args.config).read_text())
    if args.shards:  # e.g. an hourly cron for the few SKUs that move most
        cfg["target"]["shards"] = args.shards.split(",")
    db = ":memory:" if args.command in ("demo", "eval") else ROOT / cfg["paths"]["db"]
    store = Store(db)
    store.load_our_prices(ROOT / cfg["paths"]["our_prices"])
    if not os.environ.get("DECODO_API_KEY"):
        raise SystemExit("DECODO_API_KEY is not set. Copy .env.example to .env and add your Web Data API key.")
    decodo = Decodo()
    if args.command == "discover":
        discover(cfg, store, decodo, not args.no_agent)
    elif args.command == "track" and args.webhook:
        from .webhook import start

        start(cfg, store, decodo)
    elif args.command == "track":
        track(cfg, store, decodo, not args.no_agent)
    elif args.command == "serve":
        from .webhook import serve

        serve(cfg, store, decodo, args.port)
    elif args.command == "eval":
        from .evals import run_eval

        from .agent import DEFAULT_MODEL

        # After a policy change, check the model you run. Compare models only when you choose one.
        models = args.models or os.environ.get("PRICE_AGENT_MODEL") or DEFAULT_MODEL
        run_eval(cfg, decodo, models.split(","), args.runs)
    elif args.command == "ai-shelf":
        ai_shelf(cfg, store, decodo, not args.no_agent)
    else:
        demo(cfg, store, decodo, not args.no_agent)


if __name__ == "__main__":
    main()
