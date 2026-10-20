"""Run the demo's test day, a /map day, and an AI answer check, and compare each decision with the expected one."""

import json
from pathlib import Path

from .agent import Agent
from .diff import Event, diff
from .extract import Offer
from .rules import route
from .store import Store

ROOT = Path(__file__).resolve().parents[2]
# List prices per million tokens (input, cache write, cache read, output). Check them before you trust a total.
PRICES = {"claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50), "claude-sonnet-5-5": (2.00, 2.50, 0.20, 10.00),
          "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00)}


def seeded_day(store: Store, geo: str):
    days = json.loads((ROOT / "fixtures" / "snapshots.json").read_text())["days"]
    for day in days:
        for row in day["offers"]:
            store.save_snapshot(day["crawl_id"], day["fetched_at"], Offer(**row), None, geo)
    last = days[-1]["crawl_id"]
    events = diff(store.latest_rows(last), store.snapshot(last), store.our_prices_by_url(), store.history,
                  removable=set(store.snapshot(days[-2]["crawl_id"])))
    return events, store.latest_rows(last)


def cost(model: str, usage: dict) -> float:
    rates = PRICES.get(model)
    if rates is None:
        return float("nan")
    tokens = (usage["input_tokens"], usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"],
              usage["output_tokens"])
    return sum(t * r for t, r in zip(tokens, rates)) / 1_000_000


def scenarios(store: Store, geo: str) -> dict[str, tuple[list[Event], dict]]:
    """The seeded crawl day plus the /map and AI shelf events it can't produce, each decided in its own run."""
    extra = json.loads((ROOT / "fixtures" / "scenarios.json").read_text())
    return {"crawl_day": seeded_day(store, geo),
            "map_day": ([Event(**e) for e in extra["map_day"]], {}),
            "ai_shelf": ([Event(**e) for e in extra["ai_shelf"]], {})}


def run_eval(cfg: dict, decodo, models: list[str], runs: int) -> None:
    golden = json.loads((ROOT / "fixtures" / "expected_decisions.json").read_text())
    fetch = {"geo": cfg["target"]["geo"], "locale": cfg["target"]["locale"]}
    total = sum(len(golden[name]) for name in ("crawl_day", "map_day", "ai_shelf"))
    print(f"{'model':<20} {'run':>3}  {'match':>5}  {'reverify':>8}  {'cost $':>7}  misses")
    for model in models:
        for i in range(1, runs + 1):
            store = Store(":memory:")
            store.load_our_prices(ROOT / cfg["paths"]["our_prices"])
            spent, misses, reverify_ok, error = 0.0, {}, True, None
            for name, (events, baseline) in scenarios(store, cfg["target"]["geo"]).items():
                decided, events = route(events)  # the same split as a live run: rules first, the agent gets the rest
                actions = {e.id: "log_only" for e, _ in decided}
                agent = Agent(store, decodo, fetch, baseline, model=model, quiet=True)
                spent += cost(model, agent.run(events)) if events else 0.0
                if getattr(agent, "error", None):  # an unreachable API is not a wrong decision
                    error = agent.error
                    break
                actions.update(getattr(agent, "actions", {}))
                for event_id, want in golden[name].items():
                    got = actions.get(event_id)
                    if ":" in want and got == "alert_human":
                        got = f"{got}:{getattr(agent, 'urgency', {}).get(event_id)}"
                    if got != want:
                        misses[f"{name}/{event_id}"] = (want, got)
                if name == "crawl_day":
                    reverify_ok = set(golden["reverified"]) <= getattr(agent, "reverified", set())
            if error:
                print(f"{model:<20} {i:>3}  {'n/a':>5}  {'n/a':>8}  {'n/a':>7}  API error: {error}", flush=True)
                continue
            print(f"{model:<20} {i:>3}  {total - len(misses):>2}/{total}  {str(reverify_ok):>8}  "
                  f"{spent:>7.4f}  {misses or ''}", flush=True)
