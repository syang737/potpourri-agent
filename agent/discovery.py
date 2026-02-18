"""
Discovery phase: find candidate Wikipedia list pages that align with
approved Potpourri verticals.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from playwright.async_api import Browser, Page

from agent.config import (
    APPROVED_VERTICALS,
    WIKIPEDIA_REQUEST_DELAY_SEC,
    WIKIPEDIA_SEED_URLS,
)
from agent.llm import pick_best_urls

logger = logging.getLogger(__name__)


@dataclass
class CandidateLink:
    url: str
    title: str


# Patterns that hint a link leads to a ranked / "top" list
_LIST_PATTERNS = re.compile(
    r"(list[_ ]of|top[_ ]\d|largest|biggest|most[_ ]|highest|ranking|"
    r"best[_ ]selling|best-selling|gross|fortune|richest|"
    r"populous|popular|rated|award|winner)",
    re.IGNORECASE,
)

# Vertical keyword hints
_VERTICAL_KEYWORDS: dict[str, list[str]] = {
    "countries": [
        "countr", "nation", "sovereign", "land area", "population",
        "gdp", "continent", "capital",
    ],
    "sports": [
        "sport", "athlete", "olympic", "nba", "nfl", "fifa", "football",
        "basketball", "baseball", "soccer", "tennis", "medal",
    ],
    "movies": [
        "movie", "film", "box office", "gross", "oscar", "academy award",
        "director", "actor", "actress", "cinema",
    ],
    "tv": [
        "television", "tv show", "tv series", "emmy", "sitcom",
        "streaming", "netflix", "rated tv",
    ],
    "companies": [
        "compan", "corporation", "fortune", "revenue", "market cap",
        "brand", "employer", "startup", "business",
    ],
    "languages": [
        "language", "spoken", "native speaker", "lingua", "dialect",
        "writing system",
    ],
}


def _infer_vertical(text: str) -> str | None:
    """Return the best-matching approved vertical for *text*, or None."""
    text_lower = text.lower()
    for vertical, keywords in _VERTICAL_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return vertical
    return None


async def _extract_candidate_links(page: Page) -> list[CandidateLink]:
    """Extract links from the current page that look like ranked lists."""
    links: list[CandidateLink] = []
    anchors = await page.query_selector_all("a[href]")
    for anchor in anchors:
        href = await anchor.get_attribute("href") or ""
        title = (await anchor.inner_text()).strip()
        # Only Wikipedia article links
        if not href.startswith("/wiki/") and "en.wikipedia.org/wiki/" not in href:
            continue
        # Skip meta/admin pages
        if any(
            ns in href
            for ns in [
                "Wikipedia:", "Help:", "Template:", "Talk:",
                "Portal:", "Special:", "File:", "Category:",
            ]
        ):
            continue
        full_text = f"{href} {title}"
        if _LIST_PATTERNS.search(full_text) and _infer_vertical(full_text):
            full_url = (
                href
                if href.startswith("http")
                else f"https://en.wikipedia.org{href}"
            )
            links.append(CandidateLink(url=full_url, title=title))
    return links


async def discover_candidate_urls(browser: Browser) -> list[dict]:
    """
    Visit Wikipedia seed pages and return a list of promising URLs
    chosen by the LLM.

    Returns a list of dicts: [{"url": ..., "title": ..., "vertical": ...}, ...]
    """
    all_candidates: list[CandidateLink] = []
    page = await browser.new_page()

    try:
        for seed_url in WIKIPEDIA_SEED_URLS:
            logger.info("Loading seed page: %s", seed_url)
            try:
                await page.goto(seed_url, wait_until="domcontentloaded", timeout=30000)
                candidates = await _extract_candidate_links(page)
                logger.info(
                    "Found %d candidate links on %s", len(candidates), seed_url
                )
                all_candidates.extend(candidates)
            except Exception:
                logger.exception("Failed to load seed page %s", seed_url)
            await asyncio.sleep(WIKIPEDIA_REQUEST_DELAY_SEC)
    finally:
        await page.close()

    # Deduplicate by URL
    seen: set[str] = set()
    unique: list[CandidateLink] = []
    for c in all_candidates:
        if c.url not in seen:
            seen.add(c.url)
            unique.append(c)

    logger.info("Total unique candidates after dedup: %d", len(unique))

    if not unique:
        logger.warning("No candidate links found from seed pages.")
        return []

    # Let the LLM pick the best URLs
    candidate_dicts = [
        {"url": c.url, "title": c.title, "vertical": _infer_vertical(c.title) or ""}
        for c in unique
    ]
    best = await pick_best_urls(candidate_dicts, APPROVED_VERTICALS)
    logger.info("LLM selected %d URLs for scraping.", len(best))
    return best
