import json
from decimal import Decimal
from pathlib import Path

import httpx

from price_agent.decodo import Decodo
from price_agent.diff import diff
from price_agent.extract import Availability, extract_offers, parse_price
from price_agent.store import Store

DATA = Path(__file__).parent / "data"
ROOT = Path(__file__).parents[1]


def test_jsonld_from_real_woocommerce_page():
    html = (DATA / "woocommerce_product.html").read_text()
    [offer] = extract_offers("https://scrapeme.live/shop/Pikachu", html)
    assert (offer.price, offer.currency, offer.sku) == (Decimal("37.00"), "GBP", "1333")
    assert offer.availability is Availability.IN_STOCK


def test_css_fallback_when_jsonld_is_missing():
    html = '<p class="price"><span class="woocommerce-Price-amount">£1,299.00</span></p>'
    [offer] = extract_offers("u", html, {"price": "p.price .woocommerce-Price-amount", "currency": "GBP"})
    assert offer.price == Decimal("1299.00") and offer.extractor == "css"


def test_shopify_style_variants_get_their_own_offer_keys():
    ld = {"@type": "ProductGroup", "name": "Tee", "hasVariant": [
        {"@type": "Product", "sku": "TEE-S", "offers": {"price": "20", "priceCurrency": "EUR",
                                                        "availability": "https://schema.org/InStock"}},
        {"@type": "Product", "sku": "TEE-L", "offers": {"price": "22", "priceCurrency": "EUR",
                                                        "availability": "OutOfStock"}}]}
    html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    offers = extract_offers("u", html)
    assert sorted(o.offer_key for o in offers) == ["u#TEE-L", "u#TEE-S"]
    assert {o.sku: o.availability for o in offers}["TEE-L"] is Availability.OUT_OF_STOCK


def test_zero_and_garbage_prices_are_unknown_not_zero():
    assert parse_price("0.00") is None
    assert parse_price("Call for price") is None
    assert parse_price("1.299,50") == Decimal("1299.50")


def test_fixture_produces_the_nine_planted_events():
    store = Store(":memory:")
    store.load_our_prices(ROOT / "our_prices.csv")
    from price_agent.extract import Offer

    days = json.loads((ROOT / "fixtures" / "snapshots.json").read_text())["days"]
    for day in days:
        for row in day["offers"]:
            store.save_snapshot(day["crawl_id"], day["fetched_at"], Offer(**row), None, "United Kingdom")
    last = days[-1]["crawl_id"]
    events = diff(store.latest_rows(last), store.snapshot(last), store.our_prices_by_url(), store.history)
    by_name = {e.name: e for e in events}
    assert len(events) == 9
    assert by_name["Bulbasaur"].flags == ["extreme_jump"] and by_name["Bulbasaur"].gap_pct is None
    assert by_name["Mew"].flags == ["price_missing_or_zero"]
    assert by_name["Charizard"].below_floor is True and by_name["Growlithe"].below_floor is False
    assert by_name["Squirtle"].kind == "stock_changed"
    assert by_name["Mewtwo"].kind == "new_product"


def test_currency_switch_is_flagged_not_reported_as_a_drop():
    row = lambda p, c: {"url": "u", "name": "n", "price": p, "currency": c, "availability": "in_stock",
                          "extractor": "jsonld"}  # noqa: E731
    [event] = diff({"k": row("66.00", "USD")}, {"k": row("52.00", "GBP")}, {}, lambda k: [])
    assert event.flags == ["currency_changed"] and event.delta_pct is None


def test_client_retries_rate_limits_but_not_bad_requests(monkeypatch):
    import decodo.errors

    monkeypatch.setattr("price_agent.decodo.time.sleep", lambda s: None)
    calls = {"n": 0}

    class FakeApi:
        def get_crawl_results(self, crawl_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise decodo.errors.RateLimitError("slow down")
            return [{"url": "a"}, {"url": "b"}]

        def map(self, body):
            raise decodo.errors.DecodoError("bad request", 400, None)

    client = Decodo(api_key="test")
    client.api = FakeApi()
    assert [p["url"] for p in client.crawl_pages("c1")] == ["a", "b"] and calls["n"] == 2
    try:
        client.map("https://x")
        raise AssertionError("a 400 must not be retried into success")
    except decodo.errors.DecodoError:
        pass


def test_llm_extracted_price_is_flagged_only_when_it_changes():
    row = lambda p, x: {"url": "u", "name": "n", "price": p, "currency": "GBP", "availability": "in_stock",  # noqa: E731
                        "extractor": x}
    assert diff({"k": row("10.00", "jsonld")}, {"k": row("10.00", "llm")}, {}, lambda k: []) == []
    [event] = diff({"k": row("10.00", "jsonld")}, {"k": row("9.00", "llm")}, {}, lambda k: [])
    assert event.flags == ["low_confidence_extraction"]


def test_ai_quote_guard_drops_fragments_and_invented_prices():
    from price_agent.ai_shelf import verified

    answer = "Best Buy : Listed at $398.00. Sony Electronics +3 Sony store: $459.99. Range $458–$489.99."
    quotes = [("Best Buy", "$398.00"), ("Best Buy", "$3"), ("Sony", "$459.99"), ("Target", "$378.00"),
              ("Best Buy", "$458"), ("Sony", "3")]
    assert verified(quotes, answer) == [("Best Buy", Decimal("398.00")), ("Sony", Decimal("459.99")),
                                        ("Best Buy", Decimal("458"))]


def test_price_ranges_take_the_first_number():
    assert parse_price("$458–$489.99") == Decimal("458")


def test_webhook_rejects_wrong_secret_unknown_runs_and_replays(monkeypatch):
    from price_agent import webhook

    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    store = Store(":memory:")
    store.start_run("run-1", ["^/a"], set())
    calls = []
    monkeypatch.setattr("price_agent.cli.ingest", lambda *a: calls.append(a[3]))
    monkeypatch.setattr("price_agent.cli.finish_run", lambda *a, **k: calls.append("finished"))

    class FakeDecodo:
        def crawl_status(self, crawl_id):
            return {"status": "done"}

    cfg = {"target": {"shards": ["^/a"]}, "webhook": {"public_url": "https://x"}}
    body = {"crawl_id": "c1", "status": "done", "passthrough": "s3cret|run-1|0"}
    assert webhook.handle(cfg, store, FakeDecodo(), {**body, "passthrough": "wrong|run-1|0"}) == "ignored"
    assert webhook.handle(cfg, store, FakeDecodo(), {**body, "passthrough": "s3cret|run-9|0"}).startswith("ignored")
    assert webhook.handle(cfg, store, FakeDecodo(), body) == "run finished"
    assert webhook.handle(cfg, store, FakeDecodo(), body).startswith("ignored")  # the same callback again
    assert calls == ["c1", "finished"]


def test_hourly_shard_run_does_not_remove_the_rest_and_removals_fire_once(monkeypatch):
    from price_agent import cli
    from price_agent.extract import Offer

    captured = []
    monkeypatch.setattr(cli, "decide", lambda cfg, store, decodo, events, *a, **k: captured.append(events))
    store = Store(":memory:")
    cfg = {"target": {"shards": ["^/a", "^/b"]}}

    def run(run_id, shards, urls):
        store.start_run(run_id, shards, set())
        for i, u in enumerate(urls):
            store.save_snapshot(run_id, f"{run_id}T{i}", Offer(url=u, offer_key=u, name=u, price=Decimal("10"),
                                                               currency="GBP"), None, "UK")
        store.commit()
        cli.finish_run(cfg, store, None, run_id)

    run("run-1", ["^/a", "^/b"], ["https://x/a1", "https://x/b1", "https://x/b2"])
    run("run-2", ["^/a", "^/b"], ["https://x/a1", "https://x/b1", "https://x/b2"])
    run("run-3", ["^/a"], ["https://x/a1"])                     # hourly hot shard only
    run("run-4", ["^/a", "^/b"], ["https://x/a1", "https://x/b1"])  # b2 delisted
    run("run-5", ["^/a", "^/b"], ["https://x/a1", "https://x/b1"])
    kinds = [[e.kind for e in events] for events in captured]
    assert kinds == [[], [], ["removed_product"], []]


def test_ai_answers_with_bold_math_or_spaced_digits_still_parse():
    from price_agent.ai_shelf import normalize, verified

    answer = "Best Buy lists them for $ \U0001d7d2\U0001d7d3\U0001d7d6. Range about $ 3 9 9 to $ 4 5 8."
    assert normalize(answer) == "Best Buy lists them for $458. Range about $399 to $458."
    assert verified([("Best Buy", "$458"), ("Walmart", "$399")], answer) == [
        ("Best Buy", Decimal("458")), ("Walmart", Decimal("399"))]


def test_ai_quotes_are_gridded_against_live_prices():
    from price_agent.ai_shelf import grid

    live = {"Sony": Decimal("459.99"), "Best Buy": Decimal("458")}
    samples = [("chatgpt 1", [("Best Buy", Decimal("378.00")), ("Sony (official store)", Decimal("459.99")),
                              ("Sweetwater", Decimal("378"))]),
               ("chatgpt 2", [("Best Buy", Decimal("489.99"))])]
    lines = grid(samples, live)
    assert lines[2].split() == ["chatgpt", "1", "459.99", "378.00"]
    assert lines[3].split() == ["chatgpt", "2", "-", "489.99"]
    assert lines[-1] == "1 of 3 quotes for these 2 stores below the live price, 1 above"


def test_map_turns_a_newly_listed_url_into_one_new_product_event(monkeypatch):
    from price_agent import cli
    from price_agent.extract import Offer

    captured = []
    monkeypatch.setattr(cli, "decide", lambda cfg, store, decodo, events, *a, **k: captured.append(events))
    html = (DATA / "woocommerce_product.html").read_text()
    listing = {"urls": ["https://scrapeme.live/shop/Bulbasaur"]}

    class FakeDecodo:
        def map(self, url, **params):
            return listing["urls"], "skip"

        def scrape(self, url, **params):
            return {"content": html}

    store = Store(":memory:")
    cfg = {"target": {"seed": "s", "newest_listing": "n", "select_paths": [], "exclude_paths": [], "geo": "UK",
                      "locale": "en-GB", "shards": ["^/shop/"]}}
    cli.discover(cfg, store, FakeDecodo())  # first /map: baseline, no events
    listing["urls"] = ["https://scrapeme.live/shop/Pikachu", "https://scrapeme.live/shop/Bulbasaur"]
    cli.discover(cfg, store, FakeDecodo())
    [[event]] = captured
    assert (event.kind, event.name, event.new_price) == ("new_product", "Pikachu", Decimal("37.00"))

    def crawl(run_id):
        store.start_run(run_id, ["^/shop/"], set())
        for name, price in [("Bulbasaur", "63"), ("Pikachu", "37.00")]:
            url = f"https://scrapeme.live/shop/{name}"
            key = "https://scrapeme.live/shop/Pikachu#1333" if name == "Pikachu" else url
            store.save_snapshot(run_id, f"{run_id}T", Offer(url=url, offer_key=key, name=name,
                                                           price=Decimal(price), currency="GBP"), None, "UK")
        store.commit()
        cli.finish_run(cfg, store, None, run_id)

    crawl("run-1")  # the first crawl is still the crawl baseline, despite the earlier map snapshot
    crawl("run-2")
    assert len(captured) == 2 and captured[1] == []  # Pikachu is not announced twice


def test_ai_shelf_raises_misquotes_only_when_every_sample_agrees(monkeypatch):
    from price_agent import ai_shelf

    def page(price):
        ld = {"@type": "Product", "name": "H", "offers": {"price": price, "priceCurrency": "USD"}}
        return {"content": f'<script type="application/ld+json">{json.dumps(ld)}</script>'}

    answers = iter(["g1", "g2", "c1", "c2"] * 2 + ["g1", "g2", "c1", "c3"])
    quotes = {"g1": [("Walmart", Decimal("359")), ("Sony", Decimal("459.99"))], "g2": [("Walmart", Decimal("458"))],
              "c1": [("Walmart", Decimal("359")), ("Sony Store", Decimal("378"))],
              "c2": [("Walmart", Decimal("359.00")), ("Sony", Decimal("379.99"))],
              "c3": [("Walmart", Decimal("348.00")), ("Sony", Decimal("379.99"))]}
    monkeypatch.setattr(ai_shelf, "extract_quotes", lambda text: quotes[text])

    class FakeDecodo:
        def scrape(self, url, **params):
            return page("459.99" if "sony" in url else "458.00")

        def ask_ai(self, surface, question, geo):
            return {"response_text": next(answers)}

    cfg = {"ai_shelf": [{"name": "H", "retailer": "Sony", "product_url": "https://sony/p", "question": "q",
                         "geo": "United States", "locale": "en-US", "core": True,
                         "surfaces": ["google_ai_mode", "chatgpt"],
                         "live": [{"retailer": "Walmart", "url": "https://walmart/p"}]}]}
    store = Store(":memory:")
    events = ai_shelf.check_shelf(cfg, FakeDecodo(), store)
    got = [(e.surface, e.retailer, e.own_store, e.new_price, e.live_price, e.gap_pct) for e in events]
    assert got == [("chatgpt", "Sony", True, Decimal("379.99"), Decimal("459.99"), -17.4),
                   ("chatgpt", "Walmart", False, Decimal("359.00"), Decimal("458.00"), -21.6)]
    assert ai_shelf.check_shelf(cfg, FakeDecodo(), store) == []  # same misquotes next week: no repeat alert
    [changed] = ai_shelf.check_shelf(cfg, FakeDecodo(), store)  # Walmart's quote moved: a new event
    assert (changed.retailer, changed.new_price) == ("Walmart", Decimal("348.00"))


def test_a_failed_ai_engine_skips_its_samples_and_raises_no_event(monkeypatch):
    import decodo.errors
    from price_agent import ai_shelf

    ld = {"@type": "Product", "name": "H", "offers": {"price": "459.99", "priceCurrency": "USD"}}

    class FakeDecodo:
        def scrape(self, url, **params):
            return {"content": f'<script type="application/ld+json">{json.dumps(ld)}</script>'}

        def ask_ai(self, surface, question, geo):
            raise decodo.errors.DecodoError("simulated error", 500, None)

    cfg = {"ai_shelf": [{"name": "H", "retailer": "Sony", "product_url": "https://sony/p", "question": "q",
                         "geo": "United States", "locale": "en-US", "surfaces": ["chatgpt"]}]}
    assert ai_shelf.check_shelf(cfg, FakeDecodo(), Store(":memory:")) == []


def test_price_history_reads_as_runs_for_people():
    from price_agent.agent import _runs

    assert _runs(["156.00"] * 5 + ["127.92"]) == "156.00 ×5 → 127.92"
    assert _runs(["136.00", None]) == "136.00 → none"


def test_a_blocked_or_redesigned_store_alerts_instead_of_reading_as_a_quiet_day(monkeypatch):
    from price_agent import cli
    from price_agent.extract import Offer

    alerts, decided = [], []
    monkeypatch.setattr(cli, "notify", lambda text: alerts.append(text))
    monkeypatch.setattr(cli, "decide", lambda cfg, store, decodo, events, *a, **k: decided.append(events))
    store = Store(":memory:")
    cfg = {"target": {"shards": ["^/shop/"], "coverage_alert": 0.2}}
    urls = [f"https://x/shop/p{i}" for i in range(10)]

    store.start_run("run-1", ["^/shop/"], set())
    for i, u in enumerate(urls):
        store.save_snapshot("run-1", f"T{i}", Offer(url=u, offer_key=u, name=u, price=Decimal("10"), currency="GBP"),
                            None, "UK")
    store.commit()
    cli.finish_run(cfg, store, None, "run-1")
    assert alerts == []

    store.start_run("run-2", ["^/shop/"], set(urls))
    store.add_shard_result("run-2", set(), set(urls[:8]), False)  # 8 of 10 pages came back without product data
    for u in urls[8:]:
        store.save_snapshot("run-2", "T9", Offer(url=u, offer_key=u, name=u, price=Decimal("10"), currency="GBP"),
                            None, "UK")
    store.commit()
    cli.finish_run(cfg, store, None, "run-2")
    assert decided[-1] == []  # the diff alone sees nothing: a blind run looks like a quiet day
    [text] = alerts
    assert "8 of 10" in text


def test_a_crawl_capped_by_its_limit_is_not_a_coverage_alert(monkeypatch):
    from price_agent import cli
    from price_agent.extract import Offer

    alerts = []
    monkeypatch.setattr(cli, "notify", lambda text: alerts.append(text))
    monkeypatch.setattr(cli, "decide", lambda *a, **k: None)
    store = Store(":memory:")
    cfg = {"target": {"shards": ["^/products/"], "coverage_alert": 0.2}}
    expected = {f"https://x/products/p{i}" for i in range(57)}  # /map knows 57, the configured page limit is 20
    store.start_run("run-1", ["^/products/"], expected)
    store.add_shard_result("run-1", set(), {"https://x/products/p0"}, True)  # capped, 1 page without product data
    for u in sorted(expected)[1:20]:
        store.save_snapshot("run-1", "T", Offer(url=u, offer_key=u, name=u, price=Decimal("98"), currency="USD"),
                            None, "US")
    store.commit()
    cli.finish_run(cfg, store, None, "run-1")
    assert alerts == []  # 1 gap in 20 fetched: the 37 pages past the limit were skipped on purpose, not a failure


def test_rules_decide_the_clear_cut_events_and_the_agent_gets_the_rest():
    from price_agent.evals import scenarios
    from price_agent.rules import route

    store = Store(":memory:")
    store.load_our_prices(ROOT / "our_prices.csv")
    golden = json.loads((ROOT / "fixtures" / "expected_decisions.json").read_text())
    by_rule, to_agent = set(), set()
    for name, (events, _) in scenarios(store, "United Kingdom").items():
        decided, rest = route(events)
        for e, _reason in decided:
            assert golden[name][e.id] == "log_only", f"a rule logged {name}/{e.id}, golden says otherwise"
            by_rule.add(f"{name}/{e.id}")
        to_agent |= {f"{name}/{e.id}" for e in rest}
    # pure thresholds go to code; flagged data, promotions, stock-outs and big misquotes go to the agent
    assert by_rule == {"crawl_day/e6", "crawl_day/e9", "map_day/e1", "map_day/e2", "ai_shelf/a5", "ai_shelf/a6"}
    assert {"crawl_day/e1", "crawl_day/e5", "crawl_day/e2", "crawl_day/e7", "ai_shelf/a4"} <= to_agent


def test_variant_pages_compare_prices_only_on_an_exact_variant_match():
    row = lambda key, p: {"url": "u", "name": "Tee", "price": p, "currency": "USD",  # noqa: E731
                          "availability": "in_stock", "extractor": "jsonld"}
    prev = {"u#1PK": row("u#1PK", "12.00"), "u#3PK": row("u#3PK", "30.00")}
    curr = {"u#1PK": row("u#1PK", "10.00"), "u#3PK": row("u#3PK", "27.00")}
    ours = {"u": {"sku": "OUR-1", "category": "Tees", "core": 1, "our_price": Decimal("12.00"),
                  "floor_price": Decimal("10.50")}}
    events = {e.offer_key: e for e in diff(prev, curr, ours, lambda k: [])}
    # URL-only mapping on a 2-variant page: identity kept, no gap against the wrong pack size
    assert events["u#3PK"].our_sku == "OUR-1" and events["u#3PK"].gap_pct is None
    ours["u#1PK"] = ours.pop("u")  # map the exact variant
    events = {e.offer_key: e for e in diff(prev, curr, ours, lambda k: [])}
    assert events["u#1PK"].gap_pct == -16.7 and events["u#1PK"].below_floor is True
    assert events["u#3PK"].our_sku is None


def test_a_tracked_page_nobody_refreshed_raises_a_freshness_alert(monkeypatch):
    from price_agent import cli
    from price_agent.extract import Offer

    alerts = []
    monkeypatch.setattr(cli, "notify", lambda text: alerts.append(text))
    monkeypatch.setattr(cli, "decide", lambda *a, **k: None)
    store = Store(":memory:")
    store.db.executemany("INSERT INTO our_prices VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [("OUR-1", "A", "c", 1, "10", "8", "https://x/shop/a"),
                          ("OUR-2", "B", "c", 0, "10", "8", "https://x/shop/b#B1")])
    cfg = {"target": {"shards": ["^/shop/"], "coverage_alert": 0.2, "freshness_alert_hours": 48}}

    def run(run_id, when, urls, capped=False):
        store.start_run(run_id, ["^/shop/"], set())
        store.add_shard_result(run_id, set(), set(), capped)
        for u in urls:
            store.save_snapshot(run_id, when, Offer(url=u, offer_key=u, name=u, price=Decimal("10"), currency="GBP"),
                                None, "UK")
        store.commit()
        cli.finish_run(cfg, store, None, run_id)

    run("run-1", "2026-10-10T06:00:00+00:00", ["https://x/shop/a", "https://x/shop/b"])
    alerts.clear()  # run 1's pages are days old by now, so it alerts too; the point is run 2
    run("run-2", cli.now(), ["https://x/shop/a"], capped=True)  # the page limit keeps skipping b
    [text] = alerts
    assert "1 of 2" in text and "https://x/shop/b" in text


def test_agent_tool_call_without_event_ids_is_rejected():
    from types import SimpleNamespace

    import pytest

    from price_agent.agent import Agent

    agent = SimpleNamespace(events={"e1": object()}, decided=set())
    with pytest.raises(ValueError, match="empty"):
        Agent._check(agent, [])
    Agent._check(agent, ["e1"])  # a real ID still passes
