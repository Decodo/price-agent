"""Read-only MCP server over prices.db, so a pricing team can ask its own assistant what the agent did.

Run: uv run --extra mcp python -m price_agent.mcp_server   (stdio; add it to any MCP client)
There is no approve tool on purpose: approving a proposal stays a human action outside the model.
"""

import json
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

DB = Path(__file__).resolve().parents[2] / "prices.db"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
server = MCPServer("price-agent", instructions="Decisions, proposals and price history from the price agent.")


def query(sql: str, *args) -> list[dict]:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)  # the MCP side can't write, whatever it's asked
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, args)]
    finally:
        db.close()


@server.tool(annotations=READ_ONLY)
def pending_proposals() -> list[dict]:
    """Pricing proposals waiting for a human, with the agent's reason."""
    return query("SELECT id, created_at, sku, current_price, proposed_price, reason FROM proposals "
                 "WHERE status = 'pending_approval' ORDER BY created_at DESC")


@server.tool(annotations=READ_ONLY)
def recent_decisions(limit: int = 20) -> list[dict]:
    """The agent's latest decisions: action, event IDs, reason, and the evidence it cited."""
    rows = query("SELECT decided_at, action, event_ids, detail FROM decisions ORDER BY id DESC LIMIT ?", limit)
    return [{**r, "event_ids": json.loads(r["event_ids"]), "detail": json.loads(r["detail"])} for r in rows]


@server.tool(annotations=READ_ONLY)
def price_history(product: str, limit: int = 30) -> list[dict]:
    """Observed competitor prices for a product name or URL fragment, newest first."""
    return query("SELECT fetched_at, name, url, price, currency, availability, extractor FROM snapshots "
                 "WHERE name LIKE ? OR url LIKE ? ORDER BY fetched_at DESC LIMIT ?",
                 f"%{product}%", f"%{product}%", limit)


@server.tool(annotations=READ_ONLY)
def ai_shelf_quotes(limit: int = 30) -> list[dict]:
    """What Google AI Mode and ChatGPT quoted per retailer in recent AI shelf checks."""
    return query("SELECT fetched_at, surface, retailer, price FROM ai_quotes ORDER BY id DESC LIMIT ?", limit)


if __name__ == "__main__":
    server.run()
