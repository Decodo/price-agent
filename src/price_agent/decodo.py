"""The Decodo Web Data API through the official Python SDK (decodo-sdk).

Like any HTTP client in a scheduled job, this wrapper retries transient errors with backoff.
"""

import os
import time

import decodo.errors
import httpx
from decodo import DecodoClient, DecodoConfig, WebScrapingApiConfig


def _retryable(err: Exception) -> bool:
    if isinstance(err, decodo.errors.RateLimitError | decodo.errors.TimeoutError | httpx.TransportError):
        return True
    if not isinstance(err, decodo.errors.DecodoError):
        return False
    return (err.status_code or 0) >= 500 or "try again" in str(err).lower()  # errors a retry can clear


class Decodo:
    def __init__(self, api_key: str | None = None):
        key = api_key or os.environ["DECODO_API_KEY"]
        config = DecodoConfig(web_scraping_api=WebScrapingApiConfig(api_key=key))
        self.api = DecodoClient(config).web_scraping_api

    def _call(self, fn, *args):
        for attempt in range(4):
            try:
                return fn(*args)
            except Exception as err:
                if attempt == 3 or not _retryable(err):
                    raise
                time.sleep(2**attempt)

    def map(self, url: str, **params) -> tuple[list[str], str]:
        """Synchronous URL discovery. Returns (urls, sitemap_status)."""
        result = self._call(self.api.map, {"url": url, **params})["results"][0]
        return [link["url"] for link in result["content"]["links"]], result["sitemap_status"]

    def start_crawl(self, url: str, **params) -> str:
        return self._call(self.api.start_crawl, {"url": url, **params})["crawl_id"]

    def crawl_status(self, crawl_id: str) -> dict:
        return self._call(self.api.get_crawl_status, crawl_id)

    def wait_for_crawl(self, crawl_id: str, poll_seconds: int = 10) -> dict:
        while (status := self.crawl_status(crawl_id))["status"] == "pending":
            time.sleep(poll_seconds)
        return status

    def crawl_pages(self, crawl_id: str) -> list[dict]:
        """Every page of a finished crawl. The SDK follows each `next` link to the last chunk."""
        return self._call(self.api.get_crawl_results, crawl_id)

    def scrape(self, url: str, **params) -> dict:
        """Fetch one page. Used by the agent's reverify tool and the AI shelf check."""
        body = {"target": "universal", "url": url, **params}
        return self._call(self.api.scrape, body)["results"][0]

    def template(self, body: dict) -> dict:
        """Run a Decodo target template (e.g. amazon_product) and return its parsed result."""
        content = self._call(self.api.scrape, body)["results"][0]["content"]
        return content.get("results", content) if isinstance(content, dict) else {}

    def ask_ai(self, target: str, question: str, geo: str) -> dict:
        """Ask an AI answer engine through a /scrape template and return its parsed answer."""
        if target == "google_ai_mode":
            # Markdown keeps AI Mode's price list; the parsed text can stop before it
            body = {"target": target, "query": question, "geo": geo, "markdown": True}
        elif target == "chatgpt":
            body = {"target": target, "prompt": question, "parse": True, "geo": geo,
                    "search": True}  # search: let ChatGPT look up live prices instead of answering from memory
        else:  # gemini, perplexity: prompt plus parse
            body = {"target": target, "prompt": question, "parse": True, "geo": geo}
        response = self._call(self.api.scrape, body)
        if "results" not in response:  # a failed task returns a status and a message instead of results
            raise decodo.errors.DecodoError(response.get("message", "no results"), response.get("status_code"), None)
        content = response["results"][0]["content"]
        return {"response_text": content} if target == "google_ai_mode" else content["results"]
