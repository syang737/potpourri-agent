# Potpourri Puzzle Agent

A Python agent that discovers Wikipedia list pages, generates ranked "top 10" trivia puzzles using Claude, and uploads them as drafts to the Potpourri admin UI via Playwright browser automation.

## Before First Run

### 1. Install dependencies and Playwright browsers

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Set environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key (from console.anthropic.com) |
| `ADMIN_EMAIL` | For upload | Admin login email for potpourri.lol |
| `ADMIN_PASSWORD` | For upload | Admin login password |
| `POTPOURRI_URL` | No | Base URL (default: `https://potpourri.lol`) |

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export ADMIN_EMAIL="you@example.com"
export ADMIN_PASSWORD="your-password"
export POTPOURRI_URL="https://potpourri.lol"
```

### 3. (Optional) Initialize login session

Run the agent once in headed mode to log in and cache the session:

```bash
python -m agent.main --discover --headed
```

This creates `storage_state.json` so subsequent runs don't need to re-enter credentials (session lasts 24 hours).

## How to Run

### Full pipeline (discover → scrape → generate → upload)

```bash
python -m agent.main
```

### Discovery only (find candidate Wikipedia URLs)

```bash
python -m agent.main --discover
```

Prints a JSON array of candidate URLs and their inferred verticals.

### Generate only (discover + scrape + generate, no upload)

```bash
python -m agent.main --generate
```

Outputs puzzle JSON to `output/puzzles_YYYY-MM-DD.json` and prints to stdout.

### Upload from file

```bash
python -m agent.main --upload output/puzzles_2026-02-18.json
```

Reads puzzles from a JSON file and uploads them to the admin UI as drafts.

### Headed mode (debug)

Add `--headed` to any command to see the browser:

```bash
python -m agent.main --headed
```

## Expected Output

A typical full run:

```
2026-02-18 10:00:00 [INFO] agent.main: === Phase 1: Discovery ===
2026-02-18 10:00:05 [INFO] agent.discovery: Found 42 candidate links on ...
2026-02-18 10:00:08 [INFO] agent.discovery: LLM selected 7 URLs for scraping.
2026-02-18 10:00:08 [INFO] agent.main: === Phase 2: Scraping ===
2026-02-18 10:00:20 [INFO] agent.main: Scraped 7 pages.
2026-02-18 10:00:20 [INFO] agent.main: === Phase 3: Puzzle Generation ===
2026-02-18 10:00:35 [INFO] agent.llm: Generated puzzle: Top 10 countries by land area
...
2026-02-18 10:01:00 [INFO] agent.main: === Phase 4: Upload ===
2026-02-18 10:01:15 [INFO] agent.admin_uploader: Puzzle created successfully: Top 10 ...
  [OK] Top 10 countries by land area (scheduled: 2026-02-23)
  [OK] Top 10 most spoken languages (scheduled: 2026-02-24)
  ...
```

Each puzzle is created in **SCHEDULED** status (unpublished, editable). A human must review and publish from the admin UI.

## Cron Example

Run weekly on Mondays at 9 AM:

```cron
0 9 * * 1 cd /path/to/potpourri-agent && /path/to/venv/bin/python -m agent.main >> /var/log/potpourri-agent.log 2>&1
```

## Project Structure

```
agent/
  __init__.py
  main.py              # Entrypoint: orchestrates all phases
  config.py            # All constants, selectors, and env var reads
  discovery.py         # Phase 1: find Wikipedia list pages
  scraper.py           # Phase 2: extract tables with Playwright + BeautifulSoup
  llm.py               # Phase 3: Claude-powered puzzle generation
  admin_uploader.py    # Phase 4: Playwright admin UI automation
requirements.txt
README.md
```

## Selectors

All Playwright selectors for the admin UI are defined at the top of `agent/config.py`. If the admin UI changes, update them there.

## Guardrails

- **Draft-only**: The agent only clicks "Create Puzzle" (which creates in SCHEDULED status). It never clicks Publish.
- **Approved verticals only**: Only puzzles matching `countries`, `sports`, `movies`, `tv`, `companies`, `languages` are created.
- **Rate limiting**: 2-second delay between Wikipedia requests.
- **No secrets in repo**: Credentials come from environment variables only.
