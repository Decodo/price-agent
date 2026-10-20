"""Event-driven tracking: /crawl calls back when a shard finishes, and the receiver starts the next one.

Use this on a long-running host with a public URL. A CI runner can't receive webhooks, so
`price-agent track` (polling) stays the default for cron and GitHub Actions.
"""

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


def passthrough(run_id: str, index: int) -> str:
    return f"{os.environ['WEBHOOK_SECRET']}|{run_id}|{index}"


def parse_callback(body: dict) -> tuple[str, str, int] | None:
    """Return (crawl_id, run_id, shard index) for a genuine callback, or None to ignore it."""
    secret, _, rest = str(body.get("passthrough", "")).partition("|")
    if not hmac.compare_digest(secret, os.environ["WEBHOOK_SECRET"]) or "|" not in rest:
        return None
    run_id, _, index = rest.rpartition("|")
    return body["crawl_id"], run_id, int(index)


def start(cfg, store, decodo) -> None:
    """Open a run and start shard 0. The callbacks drive everything after that."""
    from .cli import new_run, start_shard

    run_id = new_run(cfg, store)
    crawl_id = start_shard(cfg, decodo, cfg["target"]["shards"][0], callback_url=cfg["webhook"]["public_url"],
                           passthrough=passthrough(run_id, 0))
    print(f"{run_id}: shard 0 started as {crawl_id}, waiting for the callback")


def handle(cfg, store, decodo, body: dict, agent_enabled: bool = True) -> str:
    from .cli import finish_run, ingest, start_shard

    parsed = parse_callback(body)
    if parsed is None:
        return "ignored"
    crawl_id, run_id, index = parsed
    if not store.has_run(run_id):
        return f"ignored: unknown run {run_id}"
    if not store.mark_ingested(run_id, crawl_id):
        return f"ignored: {crawl_id} already ingested"  # a retried or replayed callback
    ingest(cfg, store, decodo, crawl_id, run_id, decodo.crawl_status(crawl_id))
    shards = store.run(run_id)["shards"]  # the run's own shard list, not whatever config.toml says now
    if index + 1 < len(shards):
        start_shard(cfg, decodo, shards[index + 1], callback_url=cfg["webhook"]["public_url"],
                    passthrough=passthrough(run_id, index + 1))
        return f"shard {index} saved, shard {index + 1} started"
    finish_run(cfg, store, decodo, run_id, agent_enabled)
    return "run finished"


def serve(cfg, store, decodo, port: int) -> None:
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            self.send_response(200)  # answer first: ingesting a shard takes longer than a webhook timeout
            self.end_headers()
            print(handle(cfg, store, decodo, body))

    print(f"Listening on :{port} for /crawl callbacks")
    HTTPServer(("", port), Receiver).serve_forever()  # one callback at a time: SQLite and shard order stay safe
