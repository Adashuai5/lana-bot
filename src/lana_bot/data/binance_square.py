"""Binance Square scraper.

Fragile by design — the Square page is a client-rendered SPA with no stable
public API, so we render it in a headless browser and regex out cashtags.
If Binance reshuffles the DOM or blocks headless UAs, this silently returns
`{}` and the rest of the pipeline carries on (caller wraps in try/except).

Requires Playwright's Chromium bundle:
    uv run playwright install chromium
"""
from __future__ import annotations

import re
from collections import Counter

from loguru import logger

SQUARE_URL = "https://www.binance.com/en/square/home"

# $BTC, $DOGE1000, etc. Cashtags are the most reliable ticker signal on Square.
_CASHTAG_RE = re.compile(r"\$([A-Z][A-Z0-9]{1,9})\b")

# Tokens that look like cashtags but aren't tradable perps on their own.
# USDT/USDC are quote currencies; keeping them would self-match every post.
_STOPWORDS = {"USD", "USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD"}

_NAV_TIMEOUT_MS = 20_000
_RENDER_SETTLE_MS = 3_000


def fetch_square_mentions() -> dict[str, int]:
    """Return {SYMBOLUSDT: mention_count} scraped from Binance Square.

    Keys are suffixed with ``USDT`` so they line up with the perp symbols used
    by :mod:`lana_bot.data.binance_futures`. Empty dict on any failure — this
    is an enrichment signal, never load-bearing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright not installed; skipping square mentions")
        return {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 2000},
                )
                page = context.new_page()
                page.goto(SQUARE_URL, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                # Let lazy-loaded feed settle; Square hydrates well after DOMContentLoaded.
                page.wait_for_timeout(_RENDER_SETTLE_MS)
                # Scroll a bit to pull in more posts.
                for _ in range(3):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(800)
                body_text = page.inner_text("body")
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("square scrape failed: {}", e)
        return {}

    counts: Counter[str] = Counter()
    for raw in _CASHTAG_RE.findall(body_text):
        tag = raw.upper()
        if tag in _STOPWORDS:
            continue
        counts[f"{tag}USDT"] += 1

    logger.info("square mentions: {} distinct tickers", len(counts))
    return dict(counts)


if __name__ == "__main__":
    from pprint import pprint

    pprint(fetch_square_mentions())
