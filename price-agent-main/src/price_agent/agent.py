"""The deciding agent. It sees change events, never raw pages, and acts only through four tools."""

import hashlib
import json
import os
import textwrap
from decimal import Decimal, InvalidOperation
from pathlib import Path

import anthropic
import httpx

from .diff import Event, pct
from .extract import extract_offers

DEFAULT_MODEL = "claude-haiku-5-5"  # picked with `price-agent eval`; override with PRICE_AGENT_MODEL
POLICY = (Path(__file__).resolve().parents[2] / "policy.md").read_text()


def _tool(name: str, description: str, properties: dict) -> dict:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties,
                             "required": list(properties), "additionalProperties": False}}


IDS = {"type": "array", "items": {"type": "string"}}
TOOLS = [
    _tool("log_only", "Record events as noise or as a data issue. No one is notified.",
          {"event_ids": IDS, "reason": {"type": "string"}}),
    _tool("alert_human", "Notify the pricing team about events that need a person's attention.",
          {"event_ids": IDS, "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
           "reason": {"type": "string"}, "suggested_response": {"type": "string"}}),
    _tool("propose_strategy_change", "Write a pricing proposal for a human to approve. Changes nothing live.",
          {"event_ids": IDS, "our_sku": {"type": "string"},
           "change_type": {"type": "string", "enum": ["match_price", "hold_price", "review_rule"]},
           "proposed_price": {"type": "string", "description": "Decimal, e.g. 37.00"},
           "reason": {"type": "string"}}),
    _tool("reverify", "Re-fetch one event's page live and re-run extraction before you act on it.",
          {"event_id": {"type": "string"}, "reason": {"type": "string"}}),
]


def _runs(values: list) -> str:
    """Collapse a price history for people: 156.00 ×5 → 127.92."""
    out = []
    for v in values:
        if out and out[-1][0] == v:
            out[-1][1] += 1
        else:
            out.append([v, 1])
    return " → ".join(f"{v if v is not None else 'none'}" + (f" ×{n}" if n > 1 else "") for v, n in out)


class Agent:
    def __init__(self, store, decodo, fetch_params: dict, baseline: dict, notify_url: str | None = None,
                 model: str | None = None, quiet: bool = False):
        self.model = model or os.environ.get("PRICE_AGENT_MODEL") or DEFAULT_MODEL
        self.quiet = quiet
        self.store, self.decodo, self.fetch_params = store, decodo, fetch_params
        self.baseline = baseline  # offer_key -> previous snapshot row, for reverify
        self.notify_url = notify_url
        self.client = anthropic.Anthropic(max_retries=4)

    def run(self, events: list[Event]) -> dict:
        self.events = {e.id: e for e in events}
        self.actions: dict[str, str] = {}  # event id -> final action, for evals
        self.urgency: dict[str, str] = {}  # event id -> alert urgency, for evals
        self.error: str | None = None  # set when the LLM API failed, so an eval can tell outage from judgment
        self.decided: set[str] = set()
        self.reverified: set[str] = set()
        payload = json.dumps([e.model_dump(mode="json", exclude_none=True) for e in events], indent=1)
        messages = [{"role": "user", "content": f"Change events from today's crawl:\n{payload}"}]
        nudged = False
        usage = {"calls": 0, "input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                 "output_tokens": 0}

        try:
            for _ in range(12):
                response = self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    system=[{"type": "text", "text": POLICY, "cache_control": {"type": "ephemeral"}}],
                    tools=TOOLS,
                    messages=messages,
                    output_config={"effort": "medium"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
                messages.append({"role": "assistant", "content": response.content})
                usage["calls"] += 1
                for field in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
                    usage[field] += getattr(response.usage, field, 0) or 0
                calls = [b for b in response.content if b.type == "tool_use"]
                pending = sorted(self.events.keys() - self.decided)
                if not calls and (not pending or response.stop_reason == "refusal" or nudged):
                    break
                if not calls:  # tool_choice can't force a call on current models, so ask once
                    nudged = True
                    messages.append({"role": "user", "content": f"Still undecided: {pending}. Call a tool for each."})
                    continue
                messages.append({"role": "user", "content": [self._execute(c) for c in calls]})
        except anthropic.APIError as err:  # an outage must not lose today's events: tomorrow diffs against today
            self.error = type(err).__name__
            self.say(f"[agent] Claude API error, so every undecided event goes to a human: {getattr(err, 'message', err)}")
            if pending := sorted(self.events.keys() - self.decided):
                self.tool_alert_human(pending, "normal", f"Agent unavailable ({type(err).__name__}). Review manually.",
                                      "Check these changes by hand. They won't appear in tomorrow's diff.")

        for event_id in self.events.keys() - self.decided:  # guardrail: nothing is silently dropped
            self._record("log_only", [event_id], {"reason": "No decision from the agent. Logged by default."})
        self.say(f"LLM usage ({self.model}): {usage}")
        return usage

    def say(self, text: str) -> None:
        if not self.quiet:
            print(text)

    def _execute(self, call) -> dict:
        try:
            result = getattr(self, f"tool_{call.name}")(**call.input)
            return {"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result, default=str)}
        except ValueError as err:
            return {"type": "tool_result", "tool_use_id": call.id, "content": str(err), "is_error": True}

    def _check(self, event_ids: list[str]) -> None:
        if not event_ids:
            raise ValueError("event_ids is empty. Pass the IDs of the events this call decides.")
        unknown = [i for i in event_ids if i not in self.events or i in self.decided]
        if unknown:
            raise ValueError(f"Unknown or already decided event IDs: {unknown}")

    def _record(self, action: str, event_ids: list[str], detail: dict) -> dict:
        self.decided.update(event_ids)
        self.actions.update(dict.fromkeys(event_ids, action))
        self.store.log_decision(action, event_ids, detail)
        reason = textwrap.fill(str(detail.get("reason")), width=78, initial_indent="    ", subsequent_indent="    ")
        self.say(f"[{action}] {', '.join(event_ids)}\n{reason}")
        return {"recorded": action, "event_ids": event_ids}

    def tool_log_only(self, event_ids, reason):
        self._check(event_ids)
        return self._record("log_only", event_ids, {"reason": reason})

    def evidence(self, event_ids: list[str], links: bool = False) -> list[str]:
        """The facts behind a decision, so a reviewer checks numbers instead of trusting the agent."""
        lines = []
        for e in (self.events[i] for i in event_ids):
            name = f"<{e.url}|{e.name}>" if links else e.name
            if e.kind == "ai_misquote":
                lines.append(f"{name}: {e.surface} quoted {e.retailer} at {e.new_price} {e.currency or ''} "
                             f"(samples {_runs(e.history)}), {e.retailer} live page {e.live_price} ({e.gap_pct}%), "
                             f"ours {e.our_price}")
                continue
            lines.append(f"{name}: {e.old_price} → {e.new_price} {e.currency or ''} ({e.delta_pct}%), "
                         f"ours {e.our_price}, history {_runs(e.history)}" + ("" if links else f", {e.url}"))
        return lines

    def tool_alert_human(self, event_ids, urgency, reason, suggested_response):
        self._check(event_ids)
        self.urgency.update(dict.fromkeys(event_ids, urgency))
        detail = {"urgency": urgency, "reason": reason, "suggested_response": suggested_response,
                  "evidence": self.evidence(event_ids)}
        if self.notify_url:  # Slack mrkdwn: bold header, one bullet per event, product names link to the page
            text = "\n".join([f"*[{urgency}] Preliminary signal from the price agent*", reason, "",
                              *(f"• {line}" for line in self.evidence(event_ids, links=True)), "",
                              f"*Suggested:* {suggested_response}"])
            httpx.post(self.notify_url, json={"text": text})
        return self._record("alert_human", event_ids, detail)

    def tool_propose_strategy_change(self, event_ids, our_sku, change_type, proposed_price, reason):
        self._check(event_ids)
        mine = next((m for m in self.store.our_prices_by_url().values() if m["sku"] == our_sku), None)
        if mine is None:
            raise ValueError(f"{our_sku} is not one of our SKUs")
        try:
            price = Decimal(proposed_price)
        except InvalidOperation:
            raise ValueError(f"proposed_price must be a decimal, got {proposed_price!r}") from None
        if price < mine["floor_price"]:  # strict schemas can't enforce numeric bounds, so Python does
            raise ValueError(f"{price} is below the floor of {mine['floor_price']} for {our_sku}")
        proposal_id = self.store.add_proposal(our_sku, mine["our_price"], price, reason)
        return self._record("propose_strategy_change", event_ids, {
            "proposal_id": proposal_id, "our_sku": our_sku, "change_type": change_type,
            "proposed_price": price, "reason": reason, "status": "pending_approval",
            "evidence": self.evidence(event_ids)})

    def tool_reverify(self, event_id, reason):
        self._check([event_id])
        if event_id in self.reverified:
            raise ValueError(f"{event_id} was already reverified. Decide on it now.")
        self.reverified.add(event_id)
        event = self.events[event_id]
        page = self.decodo.scrape(event.url, **self.fetch_params)  # same geo and locale as the crawl
        live = next((o for o in extract_offers(event.url, page["content"]) if o.offer_key == event.offer_key), None)
        base = self.baseline.get(event.offer_key)
        base_price = Decimal(base["price"]) if base and base["price"] else None
        result = {
            "event_id": event_id,
            "http_status": page.get("status_code"),
            "html_sha256": hashlib.sha256(page["content"].encode()).hexdigest()[:16],
            "baseline_price": base_price,
            "snapshot_price": event.new_price,
            "live_price": live.price if live else None,
            "live_currency": live.currency if live else None,
            "live_availability": live.availability if live else None,
        }
        result["reproduced"] = live is not None and live.price == event.new_price
        if live and live.price and base_price:
            result["live_vs_baseline_pct"] = pct(live.price, base_price)
        self.say(f"[reverify] {event_id} {event.name}: snapshot {event.new_price}, live {result['live_price']}")
        return result
