"""
Scraper phase: extract ranking tables from Wikipedia pages using
Playwright (page load) + BeautifulSoup (HTML parsing).
"""

from __future__ import annotations

import asyncio
import logging
import re

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Browser

from agent.cache import cache_key, read_cache, write_cache
from agent.config import WIKIPEDIA_REQUEST_DELAY_SEC

logger = logging.getLogger(__name__)


def _extract_tables(html: str) -> list[list[dict[str, str]]]:
    """
    Parse all wikitables from *html* and return each as a list of row dicts.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables: list[list[dict[str, str]]] = []

    for table_tag in soup.select("table.wikitable, table.sortable"):
        rows: list[dict[str, str]] = []
        headers: list[str] = []

        # Extract headers
        header_row = table_tag.find("tr")
        if header_row and isinstance(header_row, Tag):
            for th in header_row.find_all(["th", "td"]):
                headers.append(th.get_text(strip=True))

        if not headers:
            continue

        # Extract data rows
        for tr in table_tag.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            row: dict[str, str] = {}
            for i, cell in enumerate(cells):
                key = headers[i] if i < len(headers) else f"col_{i}"
                row[key] = cell.get_text(strip=True)
            rows.append(row)

        if rows:
            tables.append(rows)

    return tables


def _extract_ordered_lists(html: str) -> list[list[str]]:
    """
    Extract ordered lists (<ol>) from the page content area as a fallback
    when no wikitable is found.
    """
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("#mw-content-text")
    if not content:
        return []

    results: list[list[str]] = []
    for ol in content.find_all("ol"):
        items = [li.get_text(strip=True) for li in ol.find_all("li", recursive=False)]
        if len(items) >= 5:
            results.append(items)
    return results


def _pick_best_table(tables: list[list[dict[str, str]]]) -> list[dict[str, str]] | None:
    """
    Heuristic: pick the table with the most rows that also has a column
    looking like a name/entity column.
    """
    name_patterns = re.compile(
        r"(name|country|nation|film|movie|show|company|language|team|"
        r"player|athlete|title|brand|city|rank)",
        re.IGNORECASE,
    )
    scored: list[tuple[int, list[dict[str, str]]]] = []
    for table in tables:
        if not table:
            continue
        name_cols = sum(1 for k in table[0] if name_patterns.search(k))
        score = len(table) * 10 + name_cols * 100
        scored.append((score, table))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored else None


async def scrape_wikipedia_page(
    browser: Browser, url: str
) -> dict:
    """
    Load *url* with Playwright and extract structured table data.

    Results are cached by URL so repeated runs skip the network request.

    Returns:
        {
            "url": str,
            "tables": [...],          # list of table row-dicts
            "ordered_lists": [...],    # fallback ordered lists
            "title": str,
        }
    """
    key = cache_key("scrape", url)
    cached = read_cache("scraper", key)
    if cached is not None:
        return cached

    page = await browser.new_page()
    result: dict = {"url": url, "tables": [], "ordered_lists": [], "title": ""}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        result["title"] = await page.title()
        html = await page.content()
        tables = _extract_tables(html)
        best = _pick_best_table(tables)
        result["tables"] = best or (tables[0] if tables else [])
        result["ordered_lists"] = _extract_ordered_lists(html)
        # Only cache successful scrapes that have data
        if result["tables"] or result["ordered_lists"]:
            write_cache("scraper", key, result)
    except Exception:
        logger.exception("Failed to scrape %s", url)
    finally:
        await page.close()
    return result


async def scrape_all(
    browser: Browser, urls: list[dict]
) -> list[dict]:
    """
    Scrape every URL in *urls* (list of {"url", "title", "vertical"} dicts)
    sequentially with polite delays.

    Returns a list of scraped-page dicts augmented with the vertical hint.
    """
    results: list[dict] = []
    for entry in urls:
        url = entry["url"]
        logger.info("Scraping: %s", url)
        try:
            data = await scrape_wikipedia_page(browser, url)
            data["vertical_hint"] = entry.get("vertical", "")
            results.append(data)
        except Exception:
            logger.exception("Skipping %s due to error", url)
        await asyncio.sleep(WIKIPEDIA_REQUEST_DELAY_SEC)
    return results
