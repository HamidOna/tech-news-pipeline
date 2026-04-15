"""Telegram bot: send tweet drafts for approval and handle callbacks."""

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.db import (
    Tweet,
    get_best_article_for_story,
    get_connection,
    get_tweet_by_telegram_message_id,
    set_telegram_message_id,
    update_tweet_draft,
    update_tweet_status,
)
from src.twitter_poster import post_tweet

logger = logging.getLogger(__name__)


def get_chat_id() -> int:
    """Return the authorized Telegram chat ID from environment."""
    return int(os.environ["TELEGRAM_CHAT_ID"])


def _build_approval_keyboard(tweet_id: int) -> InlineKeyboardMarkup:
    """Build inline keyboard with Approve / Edit / Reject buttons."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"approve:{tweet_id}"),
            InlineKeyboardButton("Edit", callback_data=f"edit:{tweet_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject:{tweet_id}"),
        ]
    ])


def _format_draft_message(tweet: Tweet, article_title: str, article_url: str) -> str:
    """Format the draft message to send via Telegram."""
    return (
        f"*New Tweet Draft*\n\n"
        f"Story: {article_title}\n"
        f"Source: {article_url}\n\n"
        f"---\n"
        f"`{tweet.draft_text}`\n"
        f"---\n\n"
        f"({len(tweet.draft_text)} / 280 chars)"
    )


async def send_draft_for_approval(
    application: Application,  # type: ignore[type-arg]
    tweet: Tweet,
) -> int | None:
    """Send a tweet draft to Telegram for human review.

    Returns the Telegram message ID, or None on failure.
    """
    chat_id = get_chat_id()
    conn = get_connection()
    article = get_best_article_for_story(conn, tweet.story_id)
    conn.close()

    if article is None:
        logger.error("No article found for story %d", tweet.story_id)
        return None

    message_text = _format_draft_message(tweet, article.title, article.url)
    keyboard = _build_approval_keyboard(tweet.id)

    try:
        msg = await application.bot.send_message(
            chat_id=chat_id,
            text=message_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        # Store the telegram message ID in the DB
        conn = get_connection()
        set_telegram_message_id(conn, tweet.id, msg.message_id)
        conn.close()

        logger.info("Sent draft tweet %d to Telegram (msg %d)", tweet.id, msg.message_id)
        return msg.message_id

    except Exception as e:
        logger.error("Failed to send draft to Telegram: %s", e)
        return None


async def update_telegram_draft(
    application: Application,  # type: ignore[type-arg]
    tweet: Tweet,
) -> None:
    """Update an existing Telegram message with a new draft (e.g., after richer source)."""
    if tweet.telegram_message_id is None:
        logger.warning("Tweet %d has no Telegram message to update", tweet.id)
        return

    chat_id = get_chat_id()
    conn = get_connection()
    article = get_best_article_for_story(conn, tweet.story_id)
    conn.close()

    if article is None:
        return

    message_text = _format_draft_message(tweet, article.title, article.url)
    keyboard = _build_approval_keyboard(tweet.id)

    try:
        await application.bot.edit_message_text(
            chat_id=chat_id,
            message_id=tweet.telegram_message_id,
            text=message_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        logger.info("Updated Telegram message %d for tweet %d", tweet.telegram_message_id, tweet.id)
    except Exception as e:
        logger.error("Failed to update Telegram message: %s", e)


# ---------------------------------------------------------------------------
# Callback handlers (for bot_server.py polling)
# ---------------------------------------------------------------------------

# Tracks which tweets are awaiting edited text from the user
_awaiting_edit: dict[int, int] = {}  # chat_id -> tweet_id


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data = query.data or ""
    action, _, tweet_id_str = data.partition(":")

    try:
        tweet_id = int(tweet_id_str)
    except ValueError:
        logger.warning("Invalid callback data: %s", data)
        return

    conn = get_connection()

    if action == "approve":
        # Post the tweet
        tweet_row = conn.execute(
            "SELECT draft_text FROM tweets WHERE id = ?", (tweet_id,),
        ).fetchone()
        if tweet_row is None:
            await query.edit_message_text("Tweet not found.")
            conn.close()
            return

        posted_id = await post_tweet(tweet_row["draft_text"])
        if posted_id:
            update_tweet_status(conn, tweet_id, "posted", posted_tweet_id=posted_id)
            await query.edit_message_text(f"Posted! Tweet ID: {posted_id}")
            logger.info("Tweet %d posted: %s", tweet_id, posted_id)
        else:
            await query.edit_message_text("Failed to post tweet. Check logs.")
            logger.error("Failed to post tweet %d", tweet_id)

    elif action == "edit":
        chat_id = query.message.chat_id if query.message else get_chat_id()
        _awaiting_edit[chat_id] = tweet_id
        await query.edit_message_text(
            "Send the replacement tweet text as your next message."
        )

    elif action == "reject":
        update_tweet_status(conn, tweet_id, "rejected")
        await query.edit_message_text("Tweet rejected.")
        logger.info("Tweet %d rejected", tweet_id)

    conn.close()


async def handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages — used for tweet edit flow."""
    if update.message is None:
        return
    chat_id = update.message.chat_id
    tweet_id = _awaiting_edit.pop(chat_id, None)

    if tweet_id is None:
        return  # Not awaiting any edit

    new_text = update.message.text or ""
    if len(new_text) > 280:
        await update.message.reply_text(
            f"Too long ({len(new_text)} chars). Max 280. Try again."
        )
        _awaiting_edit[chat_id] = tweet_id  # Keep waiting
        return

    conn = get_connection()
    update_tweet_draft(conn, tweet_id, new_text)

    # Now post it
    posted_id = await post_tweet(new_text)
    if posted_id:
        update_tweet_status(conn, tweet_id, "posted", posted_tweet_id=posted_id)
        await update.message.reply_text(f"Edited and posted! Tweet ID: {posted_id}")
        logger.info("Tweet %d edited and posted: %s", tweet_id, posted_id)
    else:
        update_tweet_status(conn, tweet_id, "approved")
        await update.message.reply_text("Saved edit but failed to post. Check logs.")

    conn.close()


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command."""
    if update.message:
        await update.message.reply_text(
            "Tech News Pipeline Bot active. Drafts will appear here for approval."
        )


def build_application() -> Application:  # type: ignore[type-arg]
    """Build and configure the Telegram bot Application."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_text))

    return app
