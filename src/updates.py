"""Update detection: classify new articles hitting existing clusters."""

import json
import logging

from src.db import (
    Article,
    assign_article_to_story,
    get_best_article_for_story,
    get_connection,
    get_pending_tweet_for_story,
    update_tweet_draft,
)
from src.drafting import draft_tweet_for_story
from src.llm_client import LLMClient

logger = logging.getLogger(__name__)

CLASSIFICATION_SYSTEM_PROMPT = """\
You are a news update classifier. Given an existing article and a new article
about the same story, classify the new article into one of three categories:

1. "rehash" — The new article covers the same information with no new details
2. "richer_source" — The new article has significantly more detail, better
   sourcing, or important additional context
3. "genuine_update" — The new article covers a genuine new development in the
   ongoing story (e.g., new facts, official response, reversal)

Respond with ONLY valid JSON:
  {"classification": "<rehash|richer_source|genuine_update>", "reason": "<brief explanation>"}
"""


def _build_classification_prompt(existing: Article, new: Article) -> str:
    """Build the user prompt for classifying an article update."""
    return (
        f"Existing best article:\n"
        f"  Headline: {existing.title}\n"
        f"  Summary: {existing.summary or '(no summary)'}\n"
        f"  Source: {existing.source_name or 'unknown'}\n\n"
        f"New article:\n"
        f"  Headline: {new.title}\n"
        f"  Summary: {new.summary or '(no summary)'}\n"
        f"  Source: {new.source_name or 'unknown'}\n\n"
        f"Classify the new article relative to the existing one."
    )


def _parse_classification(response: str) -> dict:
    """Parse classification JSON from LLM response."""
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(
            l for l in lines if not l.strip().startswith("```")
        ).strip()
    return json.loads(text)


async def classify_and_handle_update(
    llm: LLMClient,
    article: Article,
    story_id: int,
) -> str:
    """Classify a new article against its matched story and take action.

    Actions:
    - rehash: No action beyond assignment (already done in clustering)
    - richer_source: Update best source, regenerate draft if tweet is pending
    - genuine_update: Draft a follow-up tweet

    Returns the classification string.
    """
    conn = get_connection()
    existing = get_best_article_for_story(conn, story_id)

    if existing is None:
        logger.info(
            "No existing best source for story %d, treating as richer_source",
            story_id,
        )
        conn.close()
        return "richer_source"

    user_prompt = _build_classification_prompt(existing, article)

    try:
        raw = await llm.complete(CLASSIFICATION_SYSTEM_PROMPT, user_prompt)
        parsed = _parse_classification(raw)
        classification = parsed.get("classification", "rehash")
        reason = parsed.get("reason", "")
    except Exception as e:
        logger.error(
            "Classification failed for article %d: %s. Defaulting to rehash.",
            article.id, e,
        )
        conn.close()
        return "rehash"

    logger.info(
        "Article %d classified as '%s' for story %d: %s",
        article.id, classification, story_id, reason,
    )

    if classification == "rehash":
        # Nothing extra to do — article is already attached
        pass

    elif classification == "richer_source":
        # Swap best source
        conn.execute(
            "UPDATE articles SET is_best_source = 0 WHERE story_id = ? AND is_best_source = 1",
            (story_id,),
        )
        conn.execute(
            "UPDATE articles SET is_best_source = 1 WHERE id = ?",
            (article.id,),
        )
        conn.commit()

        # If there's a pending tweet, regenerate its draft
        pending = get_pending_tweet_for_story(conn, story_id)
        if pending:
            logger.info(
                "Regenerating draft for pending tweet %d (richer source)",
                pending.id,
            )
            from src.drafting import regenerate_draft
            new_draft = await regenerate_draft(llm, pending.id)
            if new_draft:
                logger.info("Updated draft for tweet %d", pending.id)
            else:
                logger.warning("Failed to regenerate draft for tweet %d", pending.id)

    elif classification == "genuine_update":
        # Draft a follow-up tweet
        conn.close()
        tweet_id = await draft_tweet_for_story(llm, story_id, tweet_type="follow_up")
        if tweet_id:
            logger.info("Created follow-up tweet %d for story %d", tweet_id, story_id)
        return classification

    conn.close()
    return classification
