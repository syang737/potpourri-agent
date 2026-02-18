"""
Admin UI uploader: use Playwright to log into the Potpourri admin,
read existing scheduled dates, and create draft puzzles via the form.

IMPORTANT: This module NEVER publishes. It only clicks "Create Puzzle"
which creates the puzzle in SCHEDULED status (the admin UI default).
Both DRAFT and SCHEDULED are unpublished and editable.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta

from playwright.async_api import Browser, BrowserContext, Page, expect

from agent.config import (
    ADMIN_EMAIL,
    ADMIN_LOGIN_URL,
    ADMIN_PASSWORD,
    ADMIN_PUZZLE_NEW_URL,
    ADMIN_PUZZLES_URL,
    ANSWER_SEARCH_DEBOUNCE_MS,
    REQUIRED_ANSWERS,
    SEL_ANSWER_SEARCH_INPUT,
    SEL_ANSWER_SEARCH_RESULT_ITEM,
    SEL_CREATE_PUZZLE_SUBMIT,
    SEL_LOGIN_EMAIL,
    SEL_LOGIN_PASSWORD,
    SEL_LOGIN_SUBMIT,
    SEL_SCHEDULED_FOR_INPUT,
    SEL_TOPIC_INPUT,
    SEL_VERTICAL_DROPDOWN,
    STORAGE_STATE_PATH,
)

logger = logging.getLogger(__name__)


async def _get_context(browser: Browser) -> BrowserContext:
    """
    Create a browser context, reusing stored auth state if available.
    """
    if os.path.exists(STORAGE_STATE_PATH):
        logger.info("Reusing stored auth state from %s", STORAGE_STATE_PATH)
        return await browser.new_context(storage_state=STORAGE_STATE_PATH)
    return await browser.new_context()


async def login(browser: Browser) -> BrowserContext:
    """
    Log in to the admin UI and persist browser state.

    Returns a BrowserContext that is authenticated.
    """
    context = await _get_context(browser)
    page = await context.new_page()

    try:
        await page.goto(ADMIN_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

        # Check if already logged in (redirected to dashboard or puzzles)
        if "/admin/puzzles" in page.url or page.url.rstrip("/").endswith("/admin"):
            # Try navigating to puzzles to verify auth
            await page.goto(ADMIN_PUZZLES_URL, wait_until="domcontentloaded", timeout=15000)
            # If we see the puzzles page, we're logged in
            try:
                await page.wait_for_selector(
                    'text="Manage Puzzles"', timeout=5000
                )
                logger.info("Already logged in via stored state.")
                await page.close()
                return context
            except Exception:
                pass  # Not logged in, continue with login

        # Perform login
        logger.info("Logging in as %s", ADMIN_EMAIL)
        await page.goto(ADMIN_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

        await page.locator(SEL_LOGIN_EMAIL).fill(ADMIN_EMAIL)
        await page.locator(SEL_LOGIN_PASSWORD).fill(ADMIN_PASSWORD)
        await page.locator(SEL_LOGIN_SUBMIT).click()

        # Wait for navigation after login
        await page.wait_for_url("**/admin/**", timeout=15000)
        logger.info("Login successful. Current URL: %s", page.url)

        # Save state for future runs
        await context.storage_state(path=STORAGE_STATE_PATH)
        logger.info("Saved auth state to %s", STORAGE_STATE_PATH)

    finally:
        await page.close()

    return context


async def get_max_scheduled_date(context: BrowserContext) -> date:
    """
    Navigate to the puzzles list page and find the latest scheduledFor date.

    Returns the max date found, or today if no puzzles exist.
    """
    page = await context.new_page()
    max_date = date.today()

    try:
        await page.goto(ADMIN_PUZZLES_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector('text="Manage Puzzles"', timeout=10000)

        # Read all text content looking for date patterns
        content = await page.content()
        # Match dates in format YYYY-MM-DD or M/D/YYYY or similar
        date_patterns = re.findall(
            r"Scheduled:\s*(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})", content
        )
        for d_str in date_patterns:
            try:
                if "-" in d_str:
                    parsed = datetime.strptime(d_str, "%Y-%m-%d").date()
                else:
                    parsed = datetime.strptime(d_str, "%m/%d/%Y").date()
                if parsed > max_date:
                    max_date = parsed
            except ValueError:
                continue

        logger.info("Max scheduled date found: %s", max_date.isoformat())
    except Exception:
        logger.exception("Failed to read puzzle list; defaulting to today.")
    finally:
        await page.close()

    return max_date


async def _get_vertical_options(page: Page) -> dict[str, str]:
    """
    Read the vertical dropdown options from the create-puzzle form.
    Returns a dict mapping slug/name (lowercased) -> option value (ID).
    """
    options: dict[str, str] = {}
    option_elements = await page.locator(f"{SEL_VERTICAL_DROPDOWN} option").all()
    for opt in option_elements:
        value = await opt.get_attribute("value") or ""
        text = (await opt.inner_text()).strip()
        if value:  # skip placeholder/empty options
            options[text.lower()] = value
    return options


async def _select_vertical(page: Page, vertical_slug: str) -> bool:
    """
    Select the correct vertical from the dropdown.
    Returns True if successful.
    """
    vertical_options = await _get_vertical_options(page)

    # Try exact match on slug, then partial match
    target_value = None
    for label, value in vertical_options.items():
        if vertical_slug.lower() in label or label in vertical_slug.lower():
            target_value = value
            break

    if not target_value:
        logger.error(
            "Vertical '%s' not found in dropdown. Available: %s",
            vertical_slug,
            list(vertical_options.keys()),
        )
        return False

    await page.locator(SEL_VERTICAL_DROPDOWN).first.select_option(target_value)
    logger.info("Selected vertical: %s (value=%s)", vertical_slug, target_value)
    return True


async def _add_answers(page: Page, answers: list[dict]) -> int:
    """
    Search for and add each answer label via the answer search autocomplete.
    Returns the number of answers successfully added.
    """
    added = 0
    for answer in answers:
        label = answer["label"]
        logger.info("  Adding answer rank %d: %s", answer["rank"], label)

        # Wait for search input to be visible
        search_input = page.locator(SEL_ANSWER_SEARCH_INPUT)
        try:
            await search_input.wait_for(state="visible", timeout=5000)
        except Exception:
            logger.warning("Answer search input not visible; stopping at %d answers.", added)
            break

        await search_input.fill(label)
        # Wait for debounce + API response
        await page.wait_for_timeout(ANSWER_SEARCH_DEBOUNCE_MS + 300)

        # Try to click the first matching result
        results = page.locator(SEL_ANSWER_SEARCH_RESULT_ITEM)
        count = await results.count()
        if count == 0:
            # Try a shorter search term (first word)
            short_label = label.split()[0] if " " in label else label[:4]
            logger.info("    No results for '%s', trying '%s'", label, short_label)
            await search_input.fill(short_label)
            await page.wait_for_timeout(ANSWER_SEARCH_DEBOUNCE_MS + 300)
            count = await results.count()

        if count > 0:
            # Find the best match
            best_match = None
            label_lower = label.lower()
            for i in range(min(count, 20)):
                item = results.nth(i)
                text = (await item.inner_text()).strip().lower()
                if label_lower in text or text in label_lower:
                    best_match = item
                    break
            if best_match is None:
                # Fall back to first result
                best_match = results.first

            await best_match.click()
            added += 1
            logger.info("    Added: %s", label)
            # Small delay between answers
            await page.wait_for_timeout(200)
        else:
            logger.warning("    No search results for '%s'; skipping.", label)

    return added


async def create_puzzle(
    context: BrowserContext, puzzle: dict
) -> bool:
    """
    Navigate to the puzzle creation form and fill it out.

    GUARDRAIL: Only clicks "Create Puzzle" (save). Never clicks Publish.

    Returns True if the puzzle was created successfully.
    """
    page = await context.new_page()
    topic = puzzle.get("topic", "Untitled")
    vertical = puzzle.get("verticalSlug", "")
    scheduled_for = puzzle.get("scheduledFor", date.today().isoformat())
    answers = puzzle.get("answers", [])

    logger.info("Creating puzzle: %s [%s] for %s", topic, vertical, scheduled_for)

    try:
        await page.goto(ADMIN_PUZZLE_NEW_URL, wait_until="domcontentloaded", timeout=30000)

        # 1. Select vertical
        if not await _select_vertical(page, vertical):
            return False

        # Small wait for vertical selection to register and answer pool to load
        await page.wait_for_timeout(500)

        # 2. Fill topic
        await page.locator(SEL_TOPIC_INPUT).fill(topic)

        # 3. Fill scheduled-for date
        await page.locator(SEL_SCHEDULED_FOR_INPUT).fill(scheduled_for)

        # 4. Add answers
        added = await _add_answers(page, answers)
        if added < REQUIRED_ANSWERS:
            logger.warning(
                "Only added %d/%d answers for '%s'. "
                "Puzzle will be created without full answers; "
                "you will need to complete them manually in admin.",
                added,
                REQUIRED_ANSWERS,
                topic,
            )

        # 5. Click "Create Puzzle" button (NOT publish)
        submit = page.locator(SEL_CREATE_PUZZLE_SUBMIT)
        if added == REQUIRED_ANSWERS:
            await submit.click()
            # Wait for redirect to puzzle list
            try:
                await page.wait_for_url("**/admin/puzzles", timeout=10000)
                logger.info("Puzzle created successfully: %s", topic)

                # Verify it appears in the list as SCHEDULED (unpublished)
                content = await page.content()
                if "PUBLISHED" in content and topic in content:
                    logger.error(
                        "GUARDRAIL VIOLATION: Puzzle '%s' appears as PUBLISHED!", topic
                    )
                    return False

                return True
            except Exception:
                logger.exception("Timeout waiting for redirect after puzzle creation.")
                return False
        else:
            logger.warning(
                "Skipping submit for '%s' – not enough answers (%d/%d). "
                "Fill remaining answers manually.",
                topic,
                added,
                REQUIRED_ANSWERS,
            )
            return False

    except Exception:
        logger.exception("Failed to create puzzle: %s", topic)
        return False
    finally:
        await page.close()


async def upload_all(
    browser: Browser, puzzles: list[dict]
) -> list[dict]:
    """
    Log in and upload every puzzle in *puzzles* as a draft.

    Returns a list of results: [{"topic": ..., "success": bool}, ...]
    """
    context = await login(browser)
    results: list[dict] = []

    try:
        for puzzle in puzzles:
            success = await create_puzzle(context, puzzle)
            results.append({
                "topic": puzzle.get("topic", "?"),
                "scheduledFor": puzzle.get("scheduledFor", "?"),
                "success": success,
            })
    finally:
        await context.close()

    return results
