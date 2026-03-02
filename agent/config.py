"""
Configuration and constants for the Potpourri Puzzle Agent.

All Playwright selectors are defined here so they can be quickly updated
if the admin UI changes.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root (one level above this file)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
POTPOURRI_URL = os.getenv("POTPOURRI_URL", "https://potpourri.lol")

# ---------------------------------------------------------------------------
# Admin URL paths
# ---------------------------------------------------------------------------
ADMIN_LOGIN_URL = f"{POTPOURRI_URL}/admin"
ADMIN_PUZZLES_URL = f"{POTPOURRI_URL}/admin/puzzles"
ADMIN_PUZZLE_NEW_URL = f"{POTPOURRI_URL}/admin/puzzles/new"

# ---------------------------------------------------------------------------
# Playwright selectors – Admin Login
# ---------------------------------------------------------------------------
SEL_LOGIN_EMAIL = 'input[type="email"]'
SEL_LOGIN_PASSWORD = 'input[type="password"]'
SEL_LOGIN_SUBMIT = 'button:has-text("Login")'
SEL_LOGIN_ERROR = 'text="Invalid credentials"'

# ---------------------------------------------------------------------------
# Playwright selectors – Puzzle Creation Form (/admin/puzzles/new)
# ---------------------------------------------------------------------------
SEL_VERTICAL_DROPDOWN = "select"  # first <select> on the page
SEL_TOPIC_INPUT = 'input[placeholder*="Top 10"]'
SEL_DESCRIPTION_TEXTAREA = "textarea"
SEL_SCHEDULED_FOR_INPUT = 'input[type="date"]'
SEL_ANSWER_SEARCH_INPUT = 'input[placeholder="Search answer pool..."]'
SEL_ANSWER_SEARCH_RESULT_ITEM = "ul li"  # search dropdown list items
SEL_CREATE_PUZZLE_SUBMIT = 'button:has-text("Create Puzzle")'

# ---------------------------------------------------------------------------
# Playwright selectors – Puzzle List (/admin/puzzles)
# ---------------------------------------------------------------------------
SEL_PUZZLES_CREATE_LINK = 'a:has-text("Create Puzzle")'
SEL_PUZZLE_SCHEDULED_DATE = 'text=/Scheduled: /'
SEL_PUZZLE_STATUS_BADGE = 'span'  # look for text content DRAFT/SCHEDULED/etc.

# ---------------------------------------------------------------------------
# Answer search debounce (ms) – the admin UI debounces 300ms; we wait longer
# ---------------------------------------------------------------------------
ANSWER_SEARCH_DEBOUNCE_MS = 500

# ---------------------------------------------------------------------------
# Approved verticals
# ---------------------------------------------------------------------------
APPROVED_VERTICALS: list[str] = [
    "countries",
    "sports",
    "movies_tv",
    "fortune_500",
    "languages",
]

# ---------------------------------------------------------------------------
# Wikipedia seed pages for discovery
# ---------------------------------------------------------------------------
WIKIPEDIA_SEED_URLS: list[str] = [
    "https://en.wikipedia.org/wiki/Category:Top_lists",
    "https://en.wikipedia.org/wiki/List_of_lists_of_lists",
    "https://en.wikipedia.org/wiki/List_of_international_rankings",
    "https://en.wikipedia.org/wiki/Wikipedia:Contents/Lists",
]

# ---------------------------------------------------------------------------
# Rate limiting – seconds between Wikipedia requests
# ---------------------------------------------------------------------------
WIKIPEDIA_REQUEST_DELAY_SEC = 2.0

# ---------------------------------------------------------------------------
# Puzzle generation
# ---------------------------------------------------------------------------
PUZZLES_PER_RUN = 7  # target 5-10 puzzles per run
REQUIRED_ANSWERS = 10

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
STORAGE_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "storage_state.json"
)
OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "output"
)

# ---------------------------------------------------------------------------
# LLM model
# ---------------------------------------------------------------------------
LLM_MODEL = "claude-sonnet-4-20250514"
