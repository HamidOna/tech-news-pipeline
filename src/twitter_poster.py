"""Post approved tweets to X/Twitter via tweepy."""

import logging
import os
from typing import Optional

import tweepy

logger = logging.getLogger(__name__)


def _get_client() -> tweepy.Client:
    """Build a tweepy Client using OAuth 1.0a credentials from env."""
    return tweepy.Client(
        consumer_key=os.environ["TWITTER_API_KEY"],
        consumer_secret=os.environ["TWITTER_API_SECRET"],
        access_token=os.environ["TWITTER_ACCESS_TOKEN"],
        access_token_secret=os.environ["TWITTER_ACCESS_SECRET"],
    )


async def post_tweet(text: str) -> Optional[str]:
    """Post a tweet and return the tweet ID string, or None on failure.

    Uses tweepy's sync client wrapped for async compatibility.
    """
    import asyncio

    def _post() -> Optional[str]:
        try:
            client = _get_client()
            response = client.create_tweet(text=text)
            tweet_id = str(response.data["id"])
            logger.info("Tweet posted: %s", tweet_id)
            return tweet_id

        except tweepy.TooManyRequests:
            logger.error("Twitter rate limit hit. Try again later.")
            return None

        except tweepy.Forbidden as e:
            logger.error("Twitter forbidden (possible duplicate?): %s", e)
            return None

        except tweepy.Unauthorized:
            logger.error("Twitter auth failed. Check API credentials.")
            return None

        except Exception as e:
            logger.error("Unexpected error posting tweet: %s", e)
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _post)
