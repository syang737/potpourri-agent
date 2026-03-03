#!/usr/bin/env python3
"""
Potpourri Puzzle Agent – main entrypoint.

Orchestrates the full pipeline:
  1. Discovery  – find promising Wikipedia list pages
  2. Scraping   – extract table data from those pages
  3. Generation – use Claude to produce puzzle JSON
  4. Upload     – create draft puzzles in the Potpourri admin UI

Usage:
    python -m agent.main              # full pipeline
    python -m agent.main --discover   # discovery only (print URLs)
    python -m agent.main --generate   # discover + generate (print JSON, no upload)
    python -m agent.main --upload FILE # upload puzzles from a JSON file
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, timedelta

from playwright.async_api import async_playwright

from agent.config import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    ANTHROPIC_API_KEY,
    OUTPUT_DIR,
    PUZZLES_PER_RUN,
)
from agent.discovery import discover_candidate_urls
from agent.scraper import scrape_all
from agent.llm import generate_puzzle
from agent.admin_uploader import get_max_scheduled_date, login, upload_all

import glob as globmod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _collect_past_topics() -> list[str]:
    """
    Read all previously generated puzzle files from the output directory
    and return a list of topic strings (e.g. "Top 10 Countries by GDP").
    This is fed to the LLM so it avoids repeating the same themes.
    """
    topics: list[str] = []
    pattern = os.path.join(OUTPUT_DIR, "puzzles_*.json")
    for path in globmod.glob(pattern):
        try:
            with open(path) as f:
                puzzles = json.load(f)
            for p in puzzles:
                topic = p.get("topic", "")
                if topic:
                    topics.append(topic)
        except (json.JSONDecodeError, OSError):
            continue
    return topics


def _validate_env() -> list[str]:
    """Return a list of missing environment variable names."""
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    return missing


def _validate_env_for_upload() -> list[str]:
    """Return a list of missing env vars needed for the upload phase."""
    missing = _validate_env()
    if not ADMIN_EMAIL:
        missing.append("ADMIN_EMAIL")
    if not ADMIN_PASSWORD:
        missing.append("ADMIN_PASSWORD")
    return missing


async def run_discovery(headless: bool = True) -> list[dict]:
    """Phase 1: discover candidate URLs from Wikipedia."""
    past_topics = _collect_past_topics()
    if past_topics:
        logger.info("Loaded %d past puzzle topics for dedup.", len(past_topics))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            urls = await discover_candidate_urls(browser, past_topics)
        finally:
            await browser.close()
    return urls


async def run_generate(headless: bool = True) -> list[dict]:
    """Phases 1-3: discover, scrape, and generate puzzles."""
    past_topics = _collect_past_topics()
    if past_topics:
        logger.info("Loaded %d past puzzle topics for dedup.", len(past_topics))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            # Phase 1 – Discovery
            logger.info("=== Phase 1: Discovery ===")
            candidate_urls = await discover_candidate_urls(browser, past_topics)
            if not candidate_urls:
                logger.warning("No candidate URLs found. Exiting.")
                return []
            logger.info("Selected %d URLs for scraping.", len(candidate_urls))

            # Phase 2 – Scraping
            logger.info("=== Phase 2: Scraping ===")
            scraped_pages = await scrape_all(browser, candidate_urls)
            if not scraped_pages:
                logger.warning("No pages scraped successfully. Exiting.")
                return []
            logger.info("Scraped %d pages.", len(scraped_pages))

            # Determine scheduling dates
            # Default: start from tomorrow if we can't read admin
            start_date = date.today() + timedelta(days=1)

            # Phase 3 – Generation
            logger.info("=== Phase 3: Puzzle Generation ===")
            puzzles: list[dict] = []
            for scraped in scraped_pages:
                if len(puzzles) >= PUZZLES_PER_RUN:
                    break
                vertical = scraped.get("vertical_hint", "")
                if not vertical:
                    logger.info("Skipping %s – no vertical hint.", scraped.get("url"))
                    continue
                scheduled_for = start_date + timedelta(days=len(puzzles))
                all_topics = past_topics + [p["topic"] for p in puzzles]
                puzzle = generate_puzzle(scraped, vertical, scheduled_for, all_topics)
                if puzzle:
                    puzzles.append(puzzle)

            logger.info("Generated %d puzzles.", len(puzzles))
        finally:
            await browser.close()

    return puzzles


async def run_upload(puzzles: list[dict], headless: bool = True) -> list[dict]:
    """Phase 4: upload puzzles to the admin UI."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            results = await upload_all(browser, puzzles)
        finally:
            await browser.close()

    return results


async def run_full(headless: bool = True) -> None:
    """Run the full pipeline: discover → scrape → generate → upload."""
    past_topics = _collect_past_topics()
    if past_topics:
        logger.info("Loaded %d past puzzle topics for dedup.", len(past_topics))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            # Phase 1 – Discovery
            logger.info("=== Phase 1: Discovery ===")
            candidate_urls = await discover_candidate_urls(browser, past_topics)
            if not candidate_urls:
                logger.warning("No candidate URLs found. Exiting.")
                return
            logger.info("Selected %d URLs for scraping.", len(candidate_urls))

            # Phase 2 – Scraping
            logger.info("=== Phase 2: Scraping ===")
            scraped_pages = await scrape_all(browser, candidate_urls)
            if not scraped_pages:
                logger.warning("No pages scraped successfully. Exiting.")
                return
            logger.info("Scraped %d pages.", len(scraped_pages))

            # Phase 3 – Generation
            logger.info("=== Phase 3: Puzzle Generation ===")

            puzzles: list[dict] = []
            for scraped in scraped_pages:
                if len(puzzles) >= PUZZLES_PER_RUN:
                    break
                vertical = scraped.get("vertical_hint", "")
                if not vertical:
                    logger.info("Skipping %s – no vertical hint.", scraped.get("url"))
                    continue
                # Use a placeholder date; real dates assigned at upload time
                scheduled_for = date.today() + timedelta(days=len(puzzles) + 1)
                all_topics = past_topics + [p["topic"] for p in puzzles]
                puzzle = generate_puzzle(scraped, vertical, scheduled_for, all_topics)
                if puzzle:
                    puzzles.append(puzzle)

            if not puzzles:
                logger.warning("No valid puzzles generated. Exiting.")
                return
            logger.info("Generated %d puzzles.", len(puzzles))

            # Save puzzles to output
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            output_path = os.path.join(
                OUTPUT_DIR, f"puzzles_{date.today().isoformat()}.json"
            )
            with open(output_path, "w") as f:
                json.dump(puzzles, f, indent=2)
            logger.info("Saved puzzles to %s", output_path)

            # Phase 4 – Upload
            # Dates are assigned here so that only successfully uploaded
            # puzzles consume a date slot (no gaps from upload failures).
            logger.info("=== Phase 4: Upload ===")
            from agent.admin_uploader import login as admin_login, create_puzzle
            context = await admin_login(browser)
            try:
                max_date = await get_max_scheduled_date(context)
                next_date = max_date + timedelta(days=1)
                for puzzle in puzzles:
                    puzzle["scheduledFor"] = next_date.isoformat()
                    success = await create_puzzle(context, puzzle)
                    status = "OK" if success else "FAILED"
                    logger.info(
                        "  [%s] %s (scheduled: %s)",
                        status,
                        puzzle.get("topic", "?"),
                        puzzle["scheduledFor"],
                    )
                    if success:
                        next_date += timedelta(days=1)
            finally:
                await context.close()

        finally:
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Potpourri Puzzle Agent – generate and upload trivia puzzles"
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Run discovery phase only (print candidate URLs)",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Run discovery + scrape + generate (print puzzle JSON, no upload)",
    )
    parser.add_argument(
        "--upload",
        metavar="FILE",
        help="Upload puzzles from a JSON file (skip discovery/scrape/generate)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode (visible window) for debugging",
    )
    args = parser.parse_args()
    headless = not args.headed

    if args.discover:
        missing = _validate_env()
        if missing:
            logger.error("Missing environment variables: %s", ", ".join(missing))
            sys.exit(1)

        urls = asyncio.run(run_discovery(headless=headless))
        print(json.dumps(urls, indent=2))

    elif args.generate:
        missing = _validate_env()
        if missing:
            logger.error("Missing environment variables: %s", ", ".join(missing))
            sys.exit(1)

        puzzles = asyncio.run(run_generate(headless=headless))
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        output_path = os.path.join(
            OUTPUT_DIR, f"puzzles_{date.today().isoformat()}.json"
        )
        with open(output_path, "w") as f:
            json.dump(puzzles, f, indent=2)
        print(f"Saved {len(puzzles)} puzzles to {output_path}")
        print(json.dumps(puzzles, indent=2))

    elif args.upload:
        missing = _validate_env_for_upload()
        if missing:
            logger.error("Missing environment variables: %s", ", ".join(missing))
            sys.exit(1)

        with open(args.upload) as f:
            puzzles = json.load(f)
        logger.info("Loaded %d puzzles from %s", len(puzzles), args.upload)
        results = asyncio.run(run_upload(puzzles, headless=headless))
        for r in results:
            status = "OK" if r["success"] else "FAILED"
            print(f"  [{status}] {r['topic']} (scheduled: {r['scheduledFor']})")

    else:
        # Full pipeline
        missing = _validate_env_for_upload()
        if missing:
            logger.error("Missing environment variables: %s", ", ".join(missing))
            sys.exit(1)

        asyncio.run(run_full(headless=headless))


if __name__ == "__main__":
    main()
