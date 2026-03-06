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
    POTPOURRI_URL,
    REQUIRED_ANSWERS,
    SEL_ANSWER_SEARCH_INPUT,
    SEL_ANSWER_SEARCH_RESULT_ITEM,
    SEL_CREATE_PUZZLE_SUBMIT,
    SEL_DESCRIPTION_TEXTAREA,
    SEL_LOGIN_EMAIL,
    SEL_LOGIN_PASSWORD,
    SEL_LOGIN_SUBMIT,
    SEL_SCHEDULED_FOR_INPUT,
    SEL_TOPIC_INPUT,
    SEL_VERTICAL_DROPDOWN,
    STORAGE_STATE_PATH,
    VERTICAL_DISPLAY_NAMES,
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


async def _verify_auth(context: BrowserContext) -> bool:
    """
    Verify that the current browser context has a valid admin session
    by hitting the /api/admin/check endpoint.

    Admin pages are client-rendered and load without auth, so checking
    if a page renders is NOT reliable.  This endpoint checks the actual
    server-side session.
    """
    page = await context.new_page()
    try:
        resp = await page.goto(
            f"{POTPOURRI_URL}/api/admin/check",
            wait_until="domcontentloaded",
            timeout=10000,
        )
        if resp and resp.ok:
            logger.info("Stored auth state is valid.")
            return True
        logger.info("Stored auth state is stale (HTTP %s).", resp.status if resp else "?")
    except Exception:
        logger.info("Could not verify stored auth state.")
    finally:
        await page.close()
    return False


async def _do_login(context: BrowserContext) -> None:
    """
    Perform a fresh email/password login and save state for future runs.
    """
    page = await context.new_page()
    try:
        logger.info("Logging in as %s", ADMIN_EMAIL)
        await page.goto(ADMIN_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

        await page.locator(SEL_LOGIN_EMAIL).fill(ADMIN_EMAIL)
        await page.locator(SEL_LOGIN_PASSWORD).fill(ADMIN_PASSWORD)

        # Wait for the login API response (not URL change — the admin page
        # stays at /admin and just re-renders via React state, so
        # wait_for_url resolves immediately and we'd save state before the
        # Set-Cookie header arrives).
        async with page.expect_response(
            lambda r: "/api/admin/login" in r.url, timeout=15000
        ) as resp_info:
            await page.locator(SEL_LOGIN_SUBMIT).click()

        resp = await resp_info.value
        if not resp.ok:
            try:
                body = await resp.json()
                detail = body.get("error", resp.status)
            except Exception:
                detail = resp.status
            raise RuntimeError(f"Login failed (HTTP {resp.status}): {detail}")

        logger.info("Login successful.")

        # Save state for future runs (cookie is now set)
        await context.storage_state(path=STORAGE_STATE_PATH)
        logger.info("Saved auth state to %s", STORAGE_STATE_PATH)
    finally:
        await page.close()


async def login(browser: Browser) -> BrowserContext:
    """
    Log in to the admin UI and persist browser state.

    If a stored session exists we verify it against the server-side API
    (not just by loading a page, which always works because admin pages
    are client-rendered).  If the session is stale we discard it and
    perform a fresh login.

    Returns a BrowserContext that is authenticated.
    """
    # Try reusing stored state
    if os.path.exists(STORAGE_STATE_PATH):
        context = await browser.new_context(storage_state=STORAGE_STATE_PATH)
        if await _verify_auth(context):
            return context
        # Stale – discard and start fresh
        logger.info("Discarding stale stored auth state.")
        await context.close()
        try:
            os.remove(STORAGE_STATE_PATH)
        except OSError:
            pass

    # Fresh login
    context = await browser.new_context()
    await _do_login(context)
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
    Caller should have already waited for the /api/admin/verticals response.
    Returns a dict mapping display name (lowercased) -> option value (ID).
    """
    dropdown = page.locator(SEL_VERTICAL_DROPDOWN)
    # Brief wait for React to finish rendering the options
    try:
        await dropdown.locator("option[value]:not([value=''])").first.wait_for(
            state="attached", timeout=3000
        )
    except Exception:
        logger.warning("Dropdown options still empty after waiting for render.")

    options: dict[str, str] = {}
    option_elements = await dropdown.locator("option").all()
    for opt in option_elements:
        value = await opt.get_attribute("value") or ""
        text = (await opt.inner_text()).strip()
        if value:  # skip placeholder/empty options
            options[text.lower()] = value
    return options


async def _select_vertical(page: Page, vertical_slug: str) -> bool:
    """
    Select the correct vertical from the dropdown.

    The dropdown displays the vertical *display name* (e.g. "Movies / TV Shows")
    while the agent passes a *slug* (e.g. "movies_tv").  We look up the expected
    display name from VERTICAL_DISPLAY_NAMES and match it against the dropdown.

    Returns True if successful.
    """
    display_name = VERTICAL_DISPLAY_NAMES.get(vertical_slug)
    if not display_name:
        logger.error(
            "Vertical slug '%s' not in VERTICAL_DISPLAY_NAMES. Known slugs: %s",
            vertical_slug,
            list(VERTICAL_DISPLAY_NAMES.keys()),
        )
        return False

    vertical_options = await _get_vertical_options(page)
    target_value = vertical_options.get(display_name.lower())

    if not target_value:
        logger.error(
            "Vertical '%s' (display name '%s') not found in dropdown. Available: %s",
            vertical_slug,
            display_name,
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
            # Find the best match – prefer exact match over substring.
            # Without this, searching "India" could match "British Indian
            # Ocean Territory" because it appears first and contains "India"
            # as a substring.
            label_lower = label.lower().strip()
            exact_match = None
            substring_match = None
            for i in range(min(count, 20)):
                item = results.nth(i)
                text = (await item.inner_text()).strip().lower()
                if text == label_lower:
                    exact_match = item
                    break
                if substring_match is None and (
                    label_lower in text or text in label_lower
                ):
                    substring_match = item

            best_match = exact_match or substring_match or results.first
            selected_text = (await best_match.inner_text()).strip()

            await best_match.click()
            added += 1
            logger.info("    Added: %s (selected: '%s')", label, selected_text)
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
        # Navigate and wait for the verticals API response so the dropdown
        # is populated before we try to read it.
        verticals_loaded = False
        try:
            async with page.expect_response(
                lambda r: "/api/admin/verticals" in r.url, timeout=15000
            ) as resp_info:
                await page.goto(
                    ADMIN_PUZZLE_NEW_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            resp = await resp_info.value
            if not resp.ok:
                logger.error(
                    "Verticals API returned HTTP %d – check server logs",
                    resp.status,
                )
            else:
                body = await resp.json()
                count = len(body.get("verticals", []))
                if count == 0:
                    logger.error(
                        "Verticals API returned 0 verticals – "
                        "run 'npx prisma db seed' in the Potpourri app"
                    )
                else:
                    logger.info("Verticals API: %d verticals loaded", count)
                    verticals_loaded = True
        except Exception:
            logger.warning(
                "Did not receive /api/admin/verticals response within timeout. "
                "The page may not have loaded correctly."
            )

        if not verticals_loaded:
            return False

        # Small wait for React to re-render with the fetched data
        await page.wait_for_timeout(500)

        # 1. Select vertical
        if not await _select_vertical(page, vertical):
            return False

        # Small wait for vertical selection to register and answer pool to load
        await page.wait_for_timeout(500)

        # 2. Fill topic
        await page.locator(SEL_TOPIC_INPUT).fill(topic)

        # 3. Fill description / source URL
        source_url = puzzle.get("sourceUrl", "")
        if source_url:
            await page.locator(SEL_DESCRIPTION_TEXTAREA).fill(source_url)

        # 4. Fill scheduled-for date
        await page.locator(SEL_SCHEDULED_FOR_INPUT).fill(scheduled_for)

        # 5. Add answers
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

        # 6. Click "Create Puzzle" button (NOT publish)
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
