"""Fetching a watched page. Two strategies, both host-side like http_get (never the sandbox):

- static:   a plain HTTP GET (urllib). Fast; supports ETag conditional requests.
- rendered: a headless Chromium via Playwright, for pages that need JavaScript. Optional
            dependency — install with `uv sync --extra rendered && uv run playwright install chromium`.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass

UA = "Mozilla/5.0 (compatible; sea-watch/0.2; +https://github.com/mopatell/self-extending-agent-2)"
FETCH_LIMIT = 5_000_000

_EXT = {"application/json": "json", "text/html": "html", "text/csv": "csv", "text/plain": "txt"}


@dataclass
class Fetched:
    body: bytes
    content_type: str
    etag: str | None = None
    not_modified: bool = False

    @property
    def ext(self) -> str:
        return _EXT.get(self.content_type, "html")


def fetch_static(url: str, etag: str | None = None, timeout: int = 30) -> Fetched:
    """A conditional GET. Returns not_modified=True (and no body) on an HTTP 304."""
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Fetched(resp.read(FETCH_LIMIT), resp.headers.get_content_type(), resp.headers.get("ETag"))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return Fetched(b"", "text/html", etag, not_modified=True)
        raise


async def fetch_rendered(url: str, timeout: int = 30) -> Fetched:
    """Load the page in headless Chromium and return the rendered HTML."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(
            "rendered fetching needs Playwright. Install it with:\n"
            "  uv sync --extra rendered && uv run playwright install chromium"
        ) from e

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(user_agent=UA)
            await page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
            html = await page.content()
        finally:
            await browser.close()
    return Fetched(html.encode(), "text/html")
