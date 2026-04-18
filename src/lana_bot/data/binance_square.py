"""Binance Square scraper.

Fragile by design — the Square page is a client-rendered SPA with no stable
public API, so we render it in a headless browser and regex out cashtags.
If Binance reshuffles the DOM or blocks headless UAs, this silently returns
`{}` and the rest of the pipeline carries on (caller wraps in try/except).

Requires Playwright's Chromium bundle:
    uv run playwright install chromium
"""
from __future__ import annotations

import json
import random
import re
import time
from collections import Counter
from pathlib import Path

from loguru import logger

from lana_bot.config import DATA_DIR

SQUARE_URL = "https://www.binance.com/zh-CN/square/home"
LANAAI_URL = "https://www.binance.com/zh-CN/square/profile/lanaai"

_CASHTAG_RE = re.compile(r"\$([A-Z][A-Z0-9]{1,9})\b")
_STOPWORDS = {"USD", "USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD"}

_NAV_TIMEOUT_MS = 20_000
_RENDER_SETTLE_MS = 4_000   # longer initial settle — reduces early-detection risk
_SCROLL_SETTLE_MS = 1_200   # per-scroll pause (was 800, now longer + fewer scrolls)

_CACHE_FILE = DATA_DIR / "square_cache.json"
_CACHE_TTL_S = 300  # 5 minutes — reuse within same 30-min cycle

# Rotate UAs across common Chrome/Mac and Chrome/Win fingerprints.
_USER_AGENTS = [
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.6312.122 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
]


def _read_cache() -> dict[str, int] | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_CACHE_FILE.read_text())
        age_s = time.time() - data.get("ts", 0)
        if age_s < _CACHE_TTL_S:
            logger.info("square cache hit (age={:.0f}s)", age_s)
            return data["mentions"]
    except Exception:  # noqa: BLE001
        pass
    return None


def _write_cache(mentions: dict[str, int], source: str = "square") -> None:
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps({"ts": time.time(), "mentions": mentions, "source": source}))


def get_square_status() -> dict:
    """Return cookie/scrape status for dashboard display."""
    cookie_ok = bool(_load_cookie())
    if not _CACHE_FILE.exists():
        return {"cookie_set": cookie_ok, "source": "none", "ts": None}
    try:
        data = json.loads(_CACHE_FILE.read_text())
        return {"cookie_set": cookie_ok, "source": data.get("source", "unknown"), "ts": data.get("ts")}
    except Exception:  # noqa: BLE001
        return {"cookie_set": cookie_ok, "source": "error", "ts": None}


def _fallback_from_gainers() -> dict[str, int]:
    """Use top-24h-gain tickers as a soft square-signal substitute."""
    try:
        from lana_bot.data.binance_futures import fetch_all_24h_tickers

        tickers = fetch_all_24h_tickers()
        tickers.sort(key=lambda t: t.price_change_pct, reverse=True)
        # Return top-20 movers with a synthetic mention count of 1.
        return {t.symbol: 1 for t in tickers[:20]}
    except Exception as e:  # noqa: BLE001
        logger.warning("square fallback also failed: {}", e)
        return {}


def fetch_square_mentions() -> dict[str, int]:
    """Return {SYMBOLUSDT: mention_count} scraped from Binance Square.

    Falls back to top-24h gainers if scraping fails. Returns cached result if
    data is fresher than 5 minutes.
    """
    cached = _read_cache()
    if cached is not None:
        return cached

    result = _scrape()
    if not result:
        logger.warning("square scrape empty/failed — using top-gainers fallback")
        result = _fallback_from_gainers()
        if result:
            _write_cache(result, source="fallback")
    else:
        _write_cache(result, source="square")
    return result


def _load_cookie() -> str:
    # Prefer plain-text file (avoids TOML quoting issues with JSON in cookie)
    try:
        from lana_bot.config import ROOT
        cookie_file = ROOT / "config" / "square_cookie.txt"
        if cookie_file.exists():
            return cookie_file.read_text().strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _scrape() -> dict[str, int]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright not installed; skipping square mentions")
        return {}

    ua = random.choice(_USER_AGENTS)
    logger.debug("square scrape UA: {}", ua[:60])
    cookie_str = _load_cookie()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=ua,
                    viewport={"width": 1280, "height": 900},
                )
                page = context.new_page()
                headers: dict[str, str] = {
                    "Referer": "https://www.binance.com/",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }
                if cookie_str:
                    headers["Cookie"] = cookie_str
                page.set_extra_http_headers(headers)
                def _fetch_page(url: str) -> str:
                    page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                    page.wait_for_timeout(_RENDER_SETTLE_MS)
                    for _ in range(2):
                        page.mouse.wheel(0, 3000)
                        page.wait_for_timeout(_SCROLL_SETTLE_MS)
                    return page.inner_text("body")

                body_text = _fetch_page(SQUARE_URL)
                # Also scrape lanaai profile (reference bot account)
                try:
                    body_text += "\n" + _fetch_page(LANAAI_URL)
                    logger.debug("lanaai profile fetched")
                except Exception:  # noqa: BLE001
                    pass
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
