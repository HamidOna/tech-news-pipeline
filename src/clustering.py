"""Story clustering: assign new articles to existing clusters or create new ones."""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from src.db import (
    Article,
    Story,
    assign_article_to_story,
    create_story,
    get_connection,
    get_recent_stories,
    get_unassigned_articles,
    update_story_timestamp,
)
from src.llm_client import LLMClient

logger = logging.getLogger(__name__)

CLUSTERING_SYSTEM_PROMPT = """\
You are a news clustering assistant. Given a new article headline and summary,
and a list of existing story clusters, decide whether the article belongs to an
existing cluster or represents a new story.

Respond with ONLY valid JSON in one of these formats:
  {"action": "match", "story_id": <id>}
  {"action": "new_story", "topic_summary": "<brief summary of the new story>"}

Rules:
- Match if the article is about the same event, product, or announcement
- Create a new story if the topic is genuinely different
- When in doubt, create a new story rather than forcing a bad match
"""


@dataclass
class ClusteringResult:
    article_id: int
    action: str  # "match" or "new_story"
    story_id: int
    is_new_story: bool


def _build_cluster_context(stories: list[Story]) -> str:
    """Format existing clusters for the LLM prompt."""
    if not stories:
        return "No existing story clusters."

    lines = ["Existing story clusters:"]
    for s in stories:
        lines.append(f"  - ID {s.id}: {s.topic_summary}")
    return "\n".join(lines)


def _build_article_prompt(article: Article, cluster_context: str) -> str:
    """Build the user prompt for clustering a single article."""
    return (
        f"{cluster_context}\n\n"
        f"New article:\n"
        f"  Headline: {article.title}\n"
        f"  Summary: {article.summary or '(no summary available)'}\n"
        f"  Source: {article.source_name or 'unknown'}\n\n"
        f"Does this article belong to an existing cluster or is it a new story?"
    )


def _parse_llm_response(response: str) -> dict:
    """Parse the JSON response from the LLM. Handles markdown code fences."""
    text = response.strip()
    if text.startswith("```"):
        # Strip ```json ... ``` wrappers
        lines = text.split("\n")
        text = "\n".join(
            l for l in lines if not l.strip().startswith("```")
        ).strip()
    return json.loads(text)


async def cluster_articles(
    llm: LLMClient,
    cluster_window_hours: int = 48,
) -> list[ClusteringResult]:
    """Assign all unassigned articles to story clusters.

    For each unassigned article:
    1. Fetch recent active stories from DB
    2. Ask LLM whether article matches an existing cluster or is new
    3. Update DB accordingly

    Returns a list of ClusteringResult describing what happened.
    """
    conn = get_connection()
    unassigned = get_unassigned_articles(conn)

    if not unassigned:
        logger.info("No unassigned articles to cluster")
        conn.close()
        return []

    results: list[ClusteringResult] = []

    for article in unassigned:
        stories = get_recent_stories(conn, hours=cluster_window_hours)
        cluster_context = _build_cluster_context(stories)
        user_prompt = _build_article_prompt(article, cluster_context)

        try:
            raw = await llm.complete(CLUSTERING_SYSTEM_PROMPT, user_prompt)
            parsed = _parse_llm_response(raw)
        except Exception as e:
            logger.error(
                "LLM clustering failed for article %d (%s): %s",
                article.id, article.title, e,
            )
            # TODO: decide on fallback — skip or create new story?
            continue

        action = parsed.get("action", "")

        if action == "match":
            story_id = parsed["story_id"]
            assign_article_to_story(conn, article.id, story_id)
            update_story_timestamp(conn, story_id)
            logger.info(
                "Article %d matched to story %d: %s",
                article.id, story_id, article.title,
            )
            results.append(ClusteringResult(
                article_id=article.id,
                action="match",
                story_id=story_id,
                is_new_story=False,
            ))

        elif action == "new_story":
            topic = parsed.get("topic_summary", article.title)
            story_id = create_story(conn, topic)
            assign_article_to_story(conn, article.id, story_id, is_best_source=True)
            logger.info(
                "Article %d started new story %d: %s",
                article.id, story_id, topic,
            )
            results.append(ClusteringResult(
                article_id=article.id,
                action="new_story",
                story_id=story_id,
                is_new_story=True,
            ))

        else:
            logger.warning(
                "Unexpected LLM action '%s' for article %d", action, article.id,
            )

    conn.close()
    logger.info("Clustering complete: %d articles processed", len(results))
    return results
