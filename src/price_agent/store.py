"""SQLite persistence. This file keeps every run, so it is the baseline for the next diff."""

import csv
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    url TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crawls (
    crawl_id TEXT PRIMARY KEY, finished_at TEXT NOT NULL, status TEXT NOT NULL,
    discovered INTEGER, completed INTEGER, failed INTEGER, missed_urls TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY, crawl_id TEXT NOT NULL, fetched_at TEXT NOT NULL,
    url TEXT NOT NULL, offer_key TEXT NOT NULL, sku TEXT, name TEXT,
    price TEXT, currency TEXT, availability TEXT NOT NULL, extractor TEXT NOT NULL,
    html_sha256 TEXT, geo TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_key ON snapshots (offer_key, fetched_at);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, shards TEXT NOT NULL, expected TEXT NOT NULL, faulted TEXT NOT NULL DEFAULT '[]',
    unparsed TEXT NOT NULL DEFAULT '[]', capped INTEGER NOT NULL DEFAULT 0, ingested TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS ai_quotes (
    id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, surface TEXT NOT NULL, question TEXT NOT NULL,
    retailer TEXT NOT NULL, price TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_misquote_state (
    key TEXT PRIMARY KEY, quote TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS our_prices (
    sku TEXT PRIMARY KEY, name TEXT, category TEXT, core INTEGER NOT NULL,
    our_price TEXT NOT NULL, floor_price TEXT NOT NULL, competitor_url TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, decided_at TEXT NOT NULL, action TEXT NOT NULL,
    event_ids TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, sku TEXT NOT NULL,
    current_price TEXT NOT NULL, proposed_price TEXT NOT NULL, reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_approval'
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def upsert_catalog(self, urls: list[str], source: str) -> list[str]:
        """Record discovered URLs and return the ones never seen before."""
        ts = now()
        known = {r["url"] for r in self.db.execute("SELECT url FROM catalog")}
        self.db.executemany(
            "INSERT INTO catalog VALUES (?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET last_seen = excluded.last_seen",
            [(u, ts, ts, source) for u in urls],
        )
        self.db.commit()
        return [u for u in urls if u not in known]

    def catalog_urls(self) -> set[str]:
        return {r["url"] for r in self.db.execute("SELECT url FROM catalog")}

    def save_crawl(self, crawl_id: str, status: dict, missed: list[str]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO crawls VALUES (?, ?, ?, ?, ?, ?, ?)",
            (crawl_id, now(), status["status"], status.get("discovered"), status.get("completed"),
             status.get("failed"), json.dumps(missed)),
        )
        self.db.commit()

    def save_snapshot(self, crawl_id: str, fetched_at: str, offer, html_sha256: str | None, geo: str) -> None:
        self.db.execute(
            "INSERT INTO snapshots (crawl_id, fetched_at, url, offer_key, sku, name, price, currency,"
            " availability, extractor, html_sha256, geo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (crawl_id, fetched_at, offer.url, offer.offer_key, offer.sku, offer.name,
             None if offer.price is None else str(offer.price), offer.currency,
             offer.availability.value, offer.extractor, html_sha256, geo),
        )

    def start_run(self, run_id: str, shards: list[str], expected: set[str]) -> None:
        self.db.execute("INSERT INTO runs (run_id, shards, expected) VALUES (?, ?, ?)",
                        (run_id, json.dumps(shards), json.dumps(sorted(expected))))
        self.db.commit()

    def add_shard_result(self, run_id: str, faulted: set[str], unparsed: set[str], capped: bool) -> None:
        run = self.run(run_id)
        self.db.execute("UPDATE runs SET faulted = ?, unparsed = ?, capped = ? WHERE run_id = ?",
                        (json.dumps(sorted(run["faulted"] | faulted)), json.dumps(sorted(run["unparsed"] | unparsed)),
                         int(run["capped"] or capped), run_id))
        self.db.commit()

    def mark_ingested(self, run_id: str, crawl_id: str) -> bool:
        """Record a crawl as ingested. False if it already was: webhooks can arrive twice."""
        done = json.loads(self.db.execute("SELECT ingested FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0])
        if crawl_id in done:
            return False
        self.db.execute("UPDATE runs SET ingested = ? WHERE run_id = ?", (json.dumps(done + [crawl_id]), run_id))
        self.db.commit()
        return True

    def has_run(self, run_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is not None

    def has_map_run(self) -> bool:
        return self.db.execute("SELECT 1 FROM runs WHERE run_id LIKE 'map-%'").fetchone() is not None

    def run(self, run_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return {"shards": json.loads(row["shards"]), "expected": set(json.loads(row["expected"])), "faulted": set(json.loads(row["faulted"])),
                "unparsed": set(json.loads(row["unparsed"])), "capped": bool(row["capped"])}

    def fetched_urls(self, run_id: str) -> set[str]:
        return {r["url"] for r in self.db.execute("SELECT DISTINCT url FROM snapshots WHERE crawl_id = ?", (run_id,))}

    def save_ai_quotes(self, surface: str, question: str, quotes) -> None:
        ts = now()
        self.db.executemany("INSERT INTO ai_quotes (fetched_at, surface, question, retailer, price) VALUES (?, ?, ?, ?, ?)",
                            [(ts, surface, question, r, str(p)) for r, p in quotes])
        self.db.commit()

    def misquote_state(self, key: str) -> str | None:
        row = self.db.execute("SELECT quote FROM ai_misquote_state WHERE key = ?", (key,)).fetchone()
        return row["quote"] if row else None

    def set_misquote_state(self, key: str, quote) -> None:
        """The latest consistent misquote per surface and store, or NULL once the answers are right again."""
        self.db.execute("INSERT OR REPLACE INTO ai_misquote_state VALUES (?, ?, ?)",
                        (key, None if quote is None else str(quote), now()))
        self.db.commit()

    def last_seen(self, urls: set[str]) -> dict[str, datetime]:
        """The latest fetch time per URL. Parsed, because Decodo and local timestamps use different formats."""
        out: dict[str, datetime] = {}
        for r in self.db.execute("SELECT url, fetched_at FROM snapshots"):
            if r["url"] in urls:
                t = datetime.fromisoformat(r["fetched_at"].replace(" ", "T"))
                t = t if t.tzinfo else t.replace(tzinfo=UTC)
                out[r["url"]] = max(out.get(r["url"], t), t)
        return out

    def commit(self) -> None:
        self.db.commit()

    def crawl_ids(self) -> list[str]:
        """Crawl IDs that produced snapshots, oldest first."""
        rows = self.db.execute("SELECT crawl_id, MIN(fetched_at) t FROM snapshots GROUP BY crawl_id ORDER BY t")
        return [r["crawl_id"] for r in rows]

    def snapshot(self, crawl_id: str) -> dict[str, sqlite3.Row]:
        rows = self.db.execute("SELECT * FROM snapshots WHERE crawl_id = ?", (crawl_id,))
        return {r["offer_key"]: r for r in rows}

    def latest_rows(self, exclude_crawl_id: str) -> dict[str, sqlite3.Row]:
        """The most recent known row for every offer, from crawls before this one."""
        rows = self.db.execute(
            "SELECT s.* FROM snapshots s JOIN (SELECT offer_key, MAX(fetched_at) m FROM snapshots"
            " WHERE crawl_id != ? GROUP BY offer_key) t ON s.offer_key = t.offer_key AND s.fetched_at = t.m"
            " WHERE s.crawl_id != ?", (exclude_crawl_id, exclude_crawl_id))
        return {r["offer_key"]: r for r in rows}

    def history(self, offer_key: str, limit: int = 8) -> list[str | None]:
        rows = self.db.execute(
            "SELECT price FROM snapshots WHERE offer_key = ? ORDER BY fetched_at DESC LIMIT ?", (offer_key, limit)
        )
        return [r["price"] for r in rows][::-1]

    def missed_urls(self, crawl_id: str) -> set[str]:
        row = self.db.execute("SELECT missed_urls FROM crawls WHERE crawl_id = ?", (crawl_id,)).fetchone()
        return set(json.loads(row["missed_urls"])) if row else set()

    def load_our_prices(self, csv_path: str | Path) -> None:
        with open(csv_path, newline="") as f:
            rows = [(r["sku"], r["name"], r["category"], int(r["core"]), r["our_price"], r["floor_price"],
                     r["competitor_url"]) for r in csv.DictReader(f)]
        self.db.executemany("INSERT OR REPLACE INTO our_prices VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        self.db.commit()

    def our_prices_by_url(self) -> dict[str, dict]:
        out = {}
        for r in self.db.execute("SELECT * FROM our_prices"):
            out[r["competitor_url"]] = {**dict(r), "our_price": Decimal(r["our_price"]),
                                        "floor_price": Decimal(r["floor_price"])}
        return out

    def log_decision(self, action: str, event_ids: list[str], detail: dict) -> None:
        self.db.execute("INSERT INTO decisions (decided_at, action, event_ids, detail) VALUES (?, ?, ?, ?)",
                        (now(), action, json.dumps(event_ids), json.dumps(detail, default=str)))
        self.db.commit()

    def add_proposal(self, sku: str, current: Decimal, proposed: Decimal, reason: str) -> int:
        cur = self.db.execute(
            "INSERT INTO proposals (created_at, sku, current_price, proposed_price, reason) VALUES (?, ?, ?, ?, ?)",
            (now(), sku, str(current), str(proposed), reason),
        )
        self.db.commit()
        return cur.lastrowid
