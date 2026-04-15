"""LLM-powered tweet drafting from story clusters."""

import logging
from pathlib import Path

from src.db import (
    create_tweet,
    get_best_article_for_story,
    get_connection,
    update_tweet_draft,
)
from src.llm_client import LLMClient

logger = logging.getLogger(__name__)

STYLE_GUIDE_PATH = Path(__file__).resolve().parent.parent / "config" / "style_guide.txt"
MAX_TWEET_LENGTH = 280

DRAFTING_SYSTEM_PROMPT = """\
You are a tech enthusiast who tweets about the latest in tech, AI, gadgets, and gaming. Your style is:

VOICE & TONE:
- Conversational and opinionated, not robotic or news-anchor formal
- You have hot takes and aren't afraid to share them
- Use humour, wit, and personality — imagine you're telling a friend about this news
- Express genuine excitement, skepticism, or surprise where appropriate
- Occasionally rhetorical questions to spark engagement

STRUCTURE:
- Lead with the interesting angle, NOT the company name
- Don't just summarise — add a take, a reaction, or context
- Use line breaks for readability when needed
- Hashtags: 1-2 max, only if genuinely relevant, at the end
- Stay under 280 characters

AVOID:
- Starting with "Breaking:" or "Just in:" — overdone
- Starting with the company/brand name — boring
- Generic phrases like "exciting news" or "game-changer"
- Ending with "What do you think?" — lazy engagement bait
- Emojis at the start of tweets
- Exclamation marks on every sentence

Write ONE tweet. Output ONLY the tweet text — no quotation marks, no preamble, no explanation, no options.
The tweet MUST be 280 characters or fewer.

EXAMPLES OF GOOD TWEETS (match this tone):
"""


def load_style_guide(path: Path = STYLE_GUIDE_PATH) -> str:
    """Load the style guide for few-shot prompting. Read at call time so edits take effect."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _build_system_prompt() -> str:
    """Build the full system prompt with style guide appended."""
    style_guide = load_style_guide()
    return DRAFTING_SYSTEM_PROMPT + style_guide


def _build_user_prompt(title: str, summary: str | None, source: str | None) -> str:
    """Build the user prompt with article details."""
    parts = [f"Headline: {title}"]
    if summary:
        parts.append(f"Summary: {summary}")
    if source:
        parts.append(f"Source: {source}")
    return "\n".join(parts)


async def _generate_draft(llm: LLMClient, system_prompt: str, user_prompt: str) -> str | None:
    """Generate a tweet draft, retrying once if too long. Returns text or None."""
    try:
        draft = await llm.complete(system_prompt, user_prompt)
    except Exception as e:
        logger.error("LLM draft generation failed: %s", e)
        return None

    if len(draft) > MAX_TWEET_LENGTH:
        logger.warning("Draft too long (%d chars), requesting shorter version", len(draft))
        shorten_prompt = (
            f"This tweet is {len(draft)} characters but must be under 280. "
            f"Shorten it while keeping the key info:\n\n{draft}"
        )
        try:
            draft = await llm.complete(system_prompt, shorten_prompt)
        except Exception as e:
            logger.error("Failed to shorten tweet: %s", e)
            return None

    if len(draft) > MAX_TWEET_LENGTH:
        logger.error("Draft still too long after shortening (%d chars), giving up", len(draft))
        return None

    return draft


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

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(article.title, article.summary, article.source_name)

    draft = await _generate_draft(llm, system_prompt, user_prompt)
    if draft is None:
        conn.close()
        return None

    tweet_id = create_tweet(conn, story_id, draft, tweet_type)
    conn.close()

    logger.info(
        "Drafted tweet %d for story %d (%d chars): %.60s...",
        tweet_id, story_id, len(draft), draft,
    )
    return tweet_id


async def regenerate_draft(llm: LLMClient, tweet_id: int) -> str | None:
    """Regenerate a tweet draft for an existing tweet (regen button).

    Updates the draft_text in DB and returns the new text, or None on failure.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT story_id FROM tweets WHERE id = ?", (tweet_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return None

    story_id = row["story_id"]
    article = get_best_article_for_story(conn, story_id)
    if article is None:
        conn.close()
        return None

    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(article.title, article.summary, article.source_name)

    draft = await _generate_draft(llm, system_prompt, user_prompt)
    if draft is None:
        conn.close()
        return None

    update_tweet_draft(conn, tweet_id, draft)
    conn.close()

    logger.info("Regenerated tweet %d (%d chars): %.60s...", tweet_id, len(draft), draft)
    return draft
