"""Pipeline orchestrator: ingestion -> clustering -> drafting -> updates -> notifications.

Entry point for cron. Runs once per invocation.
"""

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.clustering import cluster_articles
from src.db import Article, get_connection, init_db, mark_stale_stories
from src.drafting import draft_tweet_for_story
from src.ingestion import ingest_feeds
from src.llm_client import LLMClient, LLMConfig
from src.telegram_bot import send_draft_for_approval, update_draft_message
from src.updates import classify_and_handle_update

SETTINGS_PATH = Path(__file__).resolve().parent / "config" / "settings.yaml"


def load_settings() -> dict:
    """Load pipeline settings from config/settings.yaml."""
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(settings: dict) -> None:
    """Configure logging to stdout and a rotating file."""
    log_cfg = settings.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Stdout
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    # Rotating file
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "pipeline.log",
        maxBytes=log_cfg.get("max_bytes", 5_242_880),
        backupCount=log_cfg.get("backup_count", 3),
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)


def build_llm_config(settings: dict) -> LLMConfig:
    """Build LLMConfig from settings."""
    llm_cfg = settings.get("llm", {})
    return LLMConfig(
        primary_model=llm_cfg.get("primary_model", "llama-3.3-70b-versatile"),
        fallback_model=llm_cfg.get("fallback_model", "gemini-2.0-flash"),
        max_retries=llm_cfg.get("max_retries", 3),
        retry_base_delay=llm_cfg.get("retry_base_delay_seconds", 2.0),
        temperature=llm_cfg.get("temperature", 0.7),
        max_tokens=llm_cfg.get("max_tokens", 512),
    )


async def run_pipeline() -> None:
    """Execute the full pipeline once."""
    logger = logging.getLogger(__name__)

    # Load settings
    settings = load_settings()
    setup_logging(settings)
    logger.info("=== Pipeline run starting ===")

    # Init DB
    init_db()

    # Mark stale stories
    pipeline_cfg = settings.get("pipeline", {})
    stale_hours = pipeline_cfg.get("stale_after_hours", 72)
    conn = get_connection()
    stale_count = mark_stale_stories(conn, stale_hours)
    conn.close()
    if stale_count:
        logger.info("Marked %d stories as stale", stale_count)

    # Step 1: Ingest RSS feeds
    logger.info("--- Step 1: Ingestion ---")
    new_article_ids = await ingest_feeds()
    logger.info("Ingested %d new articles", len(new_article_ids))

    if not new_article_ids:
        logger.info("No new articles. Pipeline complete.")
        return

    # Step 2: Cluster articles
    logger.info("--- Step 2: Clustering ---")
    llm = LLMClient(build_llm_config(settings))
    cluster_window = pipeline_cfg.get("cluster_window_hours", 48)
    results = await cluster_articles(llm, cluster_window_hours=cluster_window)

    # Step 3: Draft tweets for undrafted stories (backlog-aware, capped)
    logger.info("--- Step 3: Drafting ---")
    cap = pipeline_cfg.get("max_drafts_per_run", 10)
    conn = get_connection()
    undrafted = conn.execute(
        "SELECT s.id FROM stories s "
        "LEFT JOIN tweets t ON s.id = t.story_id "
        "WHERE t.id IS NULL AND s.status = 'active' "
        "ORDER BY s.created_at ASC"
    ).fetchall()
    conn.close()
    total_undrafted = len(undrafted)
    logger.info("Found %d undrafted stories", total_undrafted)

    to_draft = undrafted[:cap]
    remaining = total_undrafted - len(to_draft)
    if remaining > 0:
        logger.info("Drafting %d this run (cap: %d), %d deferred to next run", len(to_draft), cap, remaining)
    elif to_draft:
        logger.info("Drafting %d this run (cap: %d)", len(to_draft), cap)

    for row in to_draft:
        tweet_id = await draft_tweet_for_story(llm, row["id"])
        if tweet_id:
            logger.info("Draft created: tweet %d for story %d", tweet_id, row["id"])

    # Step 4: Handle updates for matched stories
    logger.info("--- Step 4: Update detection ---")
    matched = [r for r in results if not r.is_new_story]
    conn = get_connection()
    for result in matched:
        row = conn.execute(
            "SELECT id, url, title, summary, source_name, story_id, is_best_source, fetched_at "
            "FROM articles WHERE id = ?",
            (result.article_id,),
        ).fetchone()
        if row:
            article = Article(**dict(row))
            classification = await classify_and_handle_update(llm, article, result.story_id)
            logger.info(
                "Article %d update type: %s", result.article_id, classification,
            )

            if classification == "richer_source":
                # Draft was regenerated in updates.py — update the Telegram message
                from src.db import get_pending_tweet_for_story
                pending = get_pending_tweet_for_story(conn, result.story_id)
                if pending and pending.telegram_message_id:
                    await update_draft_message(pending.id)
                    logger.info("Updated Telegram message for tweet %d", pending.id)

            elif classification == "genuine_update":
                # A follow-up tweet was drafted in updates.py — find and send it
                follow_up = conn.execute(
                    "SELECT id FROM tweets WHERE story_id = ? AND tweet_type = 'follow_up' "
                    "AND status = 'pending' AND telegram_message_id IS NULL "
                    "ORDER BY created_at DESC LIMIT 1",
                    (result.story_id,),
                ).fetchone()
                if follow_up:
                    msg_id = await send_draft_for_approval(follow_up["id"])
                    if msg_id:
                        logger.info("Sent follow-up draft %d to Telegram", follow_up["id"])
    conn.close()

    # Step 5: Send unsent pending drafts to Telegram (capped)
    logger.info("--- Step 5: Telegram notifications ---")
    conn = get_connection()
    total_unsent = conn.execute(
        "SELECT COUNT(*) as c FROM tweets WHERE status = 'pending' AND telegram_message_id IS NULL"
    ).fetchone()["c"]
    unsent = conn.execute(
        "SELECT id FROM tweets "
        "WHERE status = 'pending' AND telegram_message_id IS NULL "
        "ORDER BY created_at ASC LIMIT ?",
        (cap,),
    ).fetchall()
    conn.close()
    unsent_remaining = total_unsent - len(unsent)
    logger.info(
        "Sending %d drafts to Telegram (%d unsent drafts queued for next run)",
        len(unsent), unsent_remaining,
    )
    for row in unsent:
        msg_id = await send_draft_for_approval(row["id"])
        if msg_id:
            logger.info("Sent draft %d to Telegram (msg %d)", row["id"], msg_id)

    logger.info("=== Pipeline run complete ===")


def main() -> None:
    """Entry point."""
    load_dotenv()
    try:
        asyncio.run(run_pipeline())
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger(__name__).exception("Pipeline failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
