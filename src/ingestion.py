"""RSS feed polling and article ingestion into the database."""

import logging
from pathlib import Path
from typing import Optional

import feedparser
import yaml

from src.db import article_exists, get_connection, insert_article

logger = logging.getLogger(__name__)

FEEDS_PATH = Path(__file__).resolve().parent.parent / "config" / "feeds.yaml"


def load_feeds(feeds_path: Path = FEEDS_PATH) -> list[dict[str, str]]:
    """Load the list of RSS feed configs from feeds.yaml."""
    with open(feeds_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("feeds", [])


def extract_summary(entry: feedparser.FeedParserDict) -> Optional[str]:
    """Pull a plain-text summary from a feed entry, if available."""
    summary = entry.get("summary", "")
    if not summary and hasattr(entry, "description"):
        summary = entry.get("description", "")
    # Strip HTML tags (rough but sufficient for scaffolding)
    if summary:
        import re
        summary = re.sub(r"<[^>]+>", "", summary).strip()
    return summary or None


async def ingest_feeds() -> list[int]:
    """Poll all configured RSS feeds and insert new articles into the DB.

    Returns a list of newly inserted article IDs.
    """
    feeds = load_feeds()
    conn = get_connection()
    new_article_ids: list[int] = []

    for feed_cfg in feeds:
        name = feed_cfg["name"]
        url = feed_cfg["url"]
        logger.info("Polling feed: %s", name)

        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            logger.error("Failed to parse feed %s: %s", name, e)
            continue

        if parsed.bozo and not parsed.entries:
            logger.warning("Feed %s returned errors and no entries", name)
            continue

        for entry in parsed.entries:
            link = entry.get("link")
            title = entry.get("title")

            if not link or not title:
                continue

            if article_exists(conn, link):
                continue

            summary = extract_summary(entry)
            article_id = insert_article(conn, link, title, summary, name)
            new_article_ids.append(article_id)
            logger.debug("Inserted article %d: %s", article_id, title)

        logger.info("Feed %s: %d entries checked", name, len(parsed.entries))

    conn.close()
    logger.info("Ingestion complete: %d new articles", len(new_article_ids))
    return new_article_ids
