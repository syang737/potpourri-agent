"""
Simple file-based cache for expensive operations (LLM calls, web scraping).

Cached results are stored as JSON files under the ``cache/`` directory,
organised by namespace (e.g. ``cache/pick_best_urls/<hash>.json``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache")


def cache_key(*parts: object) -> str:
    """Return a SHA-256 hex digest of the JSON-serialised *parts*."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def read_cache(namespace: str, key: str) -> object | None:
    path = os.path.join(CACHE_DIR, namespace, f"{key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        logger.info("Cache hit [%s/%s]", namespace, key[:12])
        return data
    except (json.JSONDecodeError, OSError):
        return None


def write_cache(namespace: str, key: str, value: object) -> None:
    dirpath = os.path.join(CACHE_DIR, namespace)
    os.makedirs(dirpath, exist_ok=True)
    path = os.path.join(dirpath, f"{key}.json")
    with open(path, "w") as f:
        json.dump(value, f, indent=2, default=str)
    logger.info("Cached result [%s/%s]", namespace, key[:12])
