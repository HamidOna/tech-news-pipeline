"""LLM-powered tweet drafting from story clusters."""

import logging
from pathlib import Path

from src.db import (
    create_tweet,
    get_best_article_for_story,
    get_connection,
)
from src.llm_client import LLMClient

logger = logging.getLogger(__name__)

STYLE_GUIDE_PATH = Path(__file__).resolve().parent.parent / "config" / "style_guide.txt"
MAX_TWEET_LENGTH = 280


def load_style_guide(path: Path = STYLE_GUIDE_PATH) -> str:
    """Load the style guide for few-shot prompting."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _build_system_prompt(style_guide: str) -> str:
    """Build the system prompt with the style guide embedded."""
    return (
        "You are a tech news tweet writer. Draft a single tweet about the "
        "article provided. Follow the style guide below exactly.\n\n"
        f"{style_guide}\n\n"
        "RULES:\n"
        "- Output ONLY the tweet text, nothing else\n"
        "- MUST be 280 characters or fewer\n"
        "- No quotation marks wrapping the tweet\n"
        "- No explanations or preamble"
    )


def _build_user_prompt(title: str, summary: str | None, source: str | None) -> str:
    """Build the user prompt with article details."""
    parts = [f"Headline: {title}"]
    if summary:
        parts.append(f"Summary: {summary}")
    if source:
        parts.append(f"Source: {source}")
    return "\n".join(parts)


async def draft_tweet_for_story(
    llm: LLMClient,
    story_id: int,
    tweet_type: str = "original",
) -> int | None:
    """Draft a tweet for a story cluster using its best article.

    Returns the tweet ID if successful, None otherwise.
    """
    conn = get_connection()
    article = get_best_article_for_story(conn, story_id)

    if article is None:
        logger.warning("No best article found for story %d, skipping draft", story_id)
        conn.close()
        return None

    style_guide = load_style_guide()
    system_prompt = _build_system_prompt(style_guide)
    user_prompt = _build_user_prompt(article.title, article.summary, article.source_name)

    try:
        draft = await llm.complete(system_prompt, user_prompt)
    except Exception as e:
        logger.error("Failed to draft tweet for story %d: %s", story_id, e)
        conn.close()
        return None

    # Validate length — if too long, ask LLM to shorten
    if len(draft) > MAX_TWEET_LENGTH:
        logger.warning(
            "Draft too long (%d chars), requesting shorter version", len(draft),
        )
        shorten_prompt = (
            f"This tweet is {len(draft)} characters but must be under 280. "
            f"Shorten it while keeping the key info:\n\n{draft}"
        )
        try:
            draft = await llm.complete(system_prompt, shorten_prompt)
        except Exception as e:
            logger.error("Failed to shorten tweet for story %d: %s", story_id, e)
            conn.close()
            return None

    if len(draft) > MAX_TWEET_LENGTH:
        logger.error(
            "Draft still too long after shortening (%d chars), skipping", len(draft),
        )
        conn.close()
        return None

    tweet_id = create_tweet(conn, story_id, draft, tweet_type)
    conn.close()

    logger.info(
        "Drafted tweet %d for story %d (%d chars): %.60s...",
        tweet_id, story_id, len(draft), draft,
    )
    return tweet_id
