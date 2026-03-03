"""
LLM integration: use Claude to curate URLs and generate puzzle JSON
from scraped Wikipedia data.

Includes a file-based cache so repeated runs with the same inputs skip
the API call entirely.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import anthropic

from agent.cache import cache_key, read_cache, write_cache
from agent.config import ANTHROPIC_API_KEY, APPROVED_VERTICALS, LLM_MODEL, REQUIRED_ANSWERS

logger = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None

# ---------------------------------------------------------------------------
# Claude API helpers
# ---------------------------------------------------------------------------


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _call_llm(system: str, user: str, max_tokens: int = 4096) -> str:
    """Send a single message to Claude and return the text response."""
    client = _get_client()
    response = client.messages.create(
        model=LLM_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text


# ---- URL selection --------------------------------------------------------

async def pick_best_urls(
    candidates: list[dict],
    approved_verticals: list[str],
    past_topics: list[str] | None = None,
) -> list[dict]:
    """
    Ask Claude to choose 5-10 of the best candidate URLs for puzzle
    generation, avoiding themes already covered by *past_topics*.

    Each candidate dict has keys: url, title, vertical.
    Returns a filtered list with the same structure.
    """
    truncated = candidates[:80]  # keep prompt size reasonable

    # Cache key includes past_topics so a new run with different history
    # doesn't reuse stale selections.
    key = cache_key(
        sorted([c["url"] for c in truncated]),
        sorted(approved_verticals),
        sorted(past_topics or []),
    )
    cached = read_cache("pick_best_urls", key)
    if cached is not None:
        return cached

    avoid_section = ""
    if past_topics:
        avoid_section = (
            "\n\nIMPORTANT – The following puzzle topics have ALREADY been "
            "created. Pick URLs that lead to DIFFERENT themes, angles, or "
            "metrics. Be creative and explore less obvious rankings:\n"
            + "\n".join(f"- {t}" for t in past_topics)
            + "\n"
        )

    system = (
        "You are a puzzle-content curator for a trivia game called Potpourri. "
        "Your job is to pick Wikipedia list pages that will produce fun, "
        "unambiguous 'top 10' ranking puzzles.\n\n"
        "Good lists have:\n"
        "- A clear, objective ranking (by area, population, revenue, etc.)\n"
        "- At least 10 items\n"
        "- A structured table on the page\n"
        "- Mapping to one of these verticals: "
        f"{', '.join(approved_verticals)}\n\n"
        "Bad lists:\n"
        "- Subjective rankings (opinion-based 'best of')\n"
        "- Lists with fewer than 10 items\n"
        "- Lists about obscure topics most people wouldn't know\n"
        + avoid_section
    )

    user = (
        "Below are candidate Wikipedia list URLs. Pick 5-10 of the best ones "
        "for generating Potpourri puzzles. Return ONLY a JSON array of objects "
        'with keys "url", "title", "vertical". No markdown fences.\n\n'
        f"{json.dumps(truncated, indent=2)}"
    )

    raw = _call_llm(system, user)
    # Strip markdown fences if present
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()

    try:
        result = json.loads(raw)
        if not isinstance(result, list):
            raise ValueError("Expected a JSON array")
        # Filter to approved verticals only
        result = [
            r for r in result
            if r.get("vertical", "").lower() in approved_verticals
        ]
        result = result[:10]
        write_cache("pick_best_urls", key, result)
        return result
    except (json.JSONDecodeError, ValueError):
        logger.error("LLM returned invalid JSON for URL selection:\n%s", raw)
        return []


# ---- Puzzle generation ----------------------------------------------------

def generate_puzzle(
    scraped_data: dict,
    vertical_slug: str,
    scheduled_for: date,
    past_topics: list[str] | None = None,
) -> dict | None:
    """
    Given scraped Wikipedia data, produce a puzzle dict in the agent's
    internal representation.

    *past_topics* is a list of previously generated topic strings so the
    LLM can choose a fresh angle if the same source data could yield
    multiple puzzles.

    Returns None if the LLM output is invalid after one retry.
    Results are cached by (source URL, table data, vertical) so that
    re-runs with different scheduled dates don't re-call the API.
    """
    # Build a compact text representation of the scraped data
    if scraped_data.get("tables"):
        table_text = json.dumps(scraped_data["tables"][:50], indent=1)
    elif scraped_data.get("ordered_lists"):
        table_text = "\n".join(
            f"{i+1}. {item}"
            for ol in scraped_data["ordered_lists"]
            for i, item in enumerate(ol[:30])
        )
    else:
        logger.warning("No table or list data for %s", scraped_data.get("url"))
        return None

    source_url = scraped_data.get("url", "")
    page_title = scraped_data.get("title", "")

    # Check cache (keyed on content, NOT on scheduled_for)
    key = cache_key(source_url, vertical_slug, table_text)
    cached = read_cache("generate_puzzle", key)
    if cached is not None:
        # Patch the scheduled date to the requested value
        cached["scheduledFor"] = scheduled_for.isoformat()
        return cached

    avoid_section = ""
    if past_topics:
        avoid_section = (
            "\nIMPORTANT – These puzzle topics already exist. Choose a "
            "DIFFERENT angle, metric, or framing for your puzzle:\n"
            + "\n".join(f"- {t}" for t in past_topics)
            + "\n"
        )

    system = (
        "You are a puzzle generator for a trivia game called Potpourri.\n"
        "Given Wikipedia data, produce a top-10 ranking puzzle.\n\n"
        "Rules:\n"
        f"- The vertical is '{vertical_slug}'.\n"
        f"- You must produce EXACTLY {REQUIRED_ANSWERS} answers, ranked 1-10.\n"
        "- Each label must be a well-known, canonical English name "
        "(e.g., 'Russia' not 'Russian Federation', 'United States' not 'USA').\n"
        "- Apply fuzzy mapping where needed (e.g., USSR → Russia for modern "
        "continuity). Document any such mapping in fuzzyNotes.\n"
        "- The topic string should be concise and start with 'Top 10 ...'.\n"
        "- No duplicate labels.\n"
        "- Think outside the box! Use creative, surprising, or less obvious "
        "metrics when the data supports it.\n\n"
        + avoid_section
        + "Return ONLY valid JSON (no markdown fences) with this exact schema:\n"
        "{\n"
        '  "sourceUrl": "...",\n'
        '  "verticalSlug": "...",\n'
        '  "topic": "Top 10 ...",\n'
        '  "answers": [\n'
        '    {"rank": 1, "label": "..."},\n'
        "    ...\n"
        "  ],\n"
        '  "scrapedAt": "YYYY-MM-DD",\n'
        '  "scheduledFor": "YYYY-MM-DD",\n'
        '  "fuzzyNotes": "..."\n'
        "}\n"
    )

    user = (
        f"Page title: {page_title}\n"
        f"Source URL: {source_url}\n"
        f"Vertical: {vertical_slug}\n"
        f"Scheduled for: {scheduled_for.isoformat()}\n\n"
        f"Data:\n{table_text}"
    )

    for attempt in range(2):  # one retry
        raw = _call_llm(system, user)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

        try:
            puzzle = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Attempt %d: invalid JSON from LLM for %s", attempt + 1, source_url
            )
            continue

        # Validate
        answers = puzzle.get("answers", [])
        if len(answers) != REQUIRED_ANSWERS:
            logger.warning(
                "Attempt %d: expected %d answers, got %d for %s",
                attempt + 1,
                REQUIRED_ANSWERS,
                len(answers),
                source_url,
            )
            continue

        labels = [a["label"] for a in answers]
        if len(set(labels)) != REQUIRED_ANSWERS:
            logger.warning(
                "Attempt %d: duplicate labels in puzzle for %s",
                attempt + 1,
                source_url,
            )
            continue

        # Ensure required fields
        puzzle.setdefault("sourceUrl", source_url)
        puzzle.setdefault("verticalSlug", vertical_slug)
        puzzle.setdefault("scrapedAt", date.today().isoformat())
        puzzle.setdefault("scheduledFor", scheduled_for.isoformat())
        puzzle.setdefault("fuzzyNotes", "")

        logger.info("Generated puzzle: %s", puzzle.get("topic", "?"))
        write_cache("generate_puzzle", key, puzzle)
        return puzzle

    logger.error("Failed to generate valid puzzle for %s after retries.", source_url)
    return None
