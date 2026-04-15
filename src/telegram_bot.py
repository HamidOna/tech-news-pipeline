"""Telegram bot: send tweet drafts for approval and handle callbacks."""

import logging
import os
import re
from pathlib import Path

import yaml
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
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
    set_telegram_message_id,
    update_tweet_draft,
    update_tweet_status,
)

logger = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


def get_chat_id() -> int:
    """Return the authorized Telegram chat ID from environment.

    Supports both personal chats (positive) and group chats (negative).
    """
    return int(os.environ["TELEGRAM_CHAT_ID"])


def _escape_md2(text: str) -> str:
    """Escape special characters for MarkdownV2 parse mode."""
    special = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(special)}])", r"\\\1", text)


def _build_approval_keyboard(tweet_id: int) -> InlineKeyboardMarkup:
    """Build inline keyboard with Approve / Edit / Regen / Reject buttons."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2705 Approve", callback_data=f"approve:{tweet_id}"),
            InlineKeyboardButton("\u270f\ufe0f Edit", callback_data=f"edit:{tweet_id}"),
            InlineKeyboardButton("\U0001f504 Regen", callback_data=f"regen:{tweet_id}"),
            InlineKeyboardButton("\u274c Reject", callback_data=f"reject:{tweet_id}"),
        ]
    ])


def _format_draft_message(
    story_summary: str,
    article_url: str,
    draft_text: str,
) -> str:
    """Format the draft message for Telegram using MarkdownV2."""
    char_count = len(draft_text)
    return (
        f"\U0001f4f0 *{_escape_md2(story_summary)}*\n\n"
        f"\U0001f517 Source: {_escape_md2(article_url)}\n\n"
        f"\U0001f4dd Draft tweet:\n"
        f"```\n{_escape_md2(draft_text)}\n```\n\n"
        f"_{_escape_md2(f'{char_count}/280 chars')}_"
    )


def _format_updated_draft_message(
    story_summary: str,
    article_url: str,
    draft_text: str,
) -> str:
    """Format an updated draft message (richer source found)."""
    char_count = len(draft_text)
    return (
        f"\U0001f504 *Draft updated \u2014 richer source found*\n\n"
        f"\U0001f4f0 *{_escape_md2(story_summary)}*\n\n"
        f"\U0001f517 Source: {_escape_md2(article_url)}\n\n"
        f"\U0001f4dd Draft tweet:\n"
        f"```\n{_escape_md2(draft_text)}\n```\n\n"
        f"_{_escape_md2(f'{char_count}/280 chars')}_"
    )


def _format_regen_draft_message(
    story_summary: str,
    article_url: str,
    draft_text: str,
) -> str:
    """Format a regenerated draft message."""
    char_count = len(draft_text)
    return (
        f"\U0001f504 *Draft regenerated*\n\n"
        f"\U0001f4f0 *{_escape_md2(story_summary)}*\n\n"
        f"\U0001f517 Source: {_escape_md2(article_url)}\n\n"
        f"\U0001f4dd Draft tweet:\n"
        f"```\n{_escape_md2(draft_text)}\n```\n\n"
        f"_{_escape_md2(f'{char_count}/280 chars')}_"
    )


def _get_llm_client():
    """Create an LLM client using settings from config."""
    from src.llm_client import LLMClient, LLMConfig

    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            settings = yaml.safe_load(f)
        llm_cfg = settings.get("llm", {})
        config = LLMConfig(
            primary_model=llm_cfg.get("primary_model", "llama-3.3-70b-versatile"),
            fallback_model=llm_cfg.get("fallback_model", "gemini-2.0-flash"),
            max_retries=llm_cfg.get("max_retries", 3),
            retry_base_delay=llm_cfg.get("retry_base_delay_seconds", 2.0),
            temperature=llm_cfg.get("temperature", 0.7),
            max_tokens=llm_cfg.get("max_tokens", 512),
        )
    except Exception:
        config = LLMConfig()

    return LLMClient(config)


# ---------------------------------------------------------------------------
# Pipeline-side functions (called from main.py, no Application needed)
# ---------------------------------------------------------------------------

async def send_draft_for_approval(tweet_id: int) -> int | None:
    """Send a tweet draft to Telegram for approval.

    Fetches tweet + story/article from DB, sends a formatted message with
    inline Approve/Edit/Regen/Reject buttons, stores the telegram_message_id.

    Returns the Telegram message ID, or None on failure.
    """
    conn = get_connection()

    row = conn.execute(
        "SELECT id, story_id, draft_text, tweet_type, status, "
        "telegram_message_id, posted_tweet_id, created_at, posted_at "
        "FROM tweets WHERE id = ?",
        (tweet_id,),
    ).fetchone()
    if row is None:
        logger.error("Tweet %d not found in DB", tweet_id)
        conn.close()
        return None

    tweet = Tweet(**dict(row))
    story_row = conn.execute(
        "SELECT topic_summary FROM stories WHERE id = ?",
        (tweet.story_id,),
    ).fetchone()
    article = get_best_article_for_story(conn, tweet.story_id)

    if article is None or story_row is None:
        logger.error("No article/story found for tweet %d (story %d)", tweet_id, tweet.story_id)
        conn.close()
        return None

    story_summary = story_row["topic_summary"]
    message_text = _format_draft_message(story_summary, article.url, tweet.draft_text)
    keyboard = _build_approval_keyboard(tweet.id)

    try:
        bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
        async with bot:
            msg = await bot.send_message(
                chat_id=get_chat_id(),
                text=message_text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        set_telegram_message_id(conn, tweet.id, msg.message_id)
        conn.close()
        logger.info("Sent draft tweet %d to Telegram (msg %d)", tweet.id, msg.message_id)
        return msg.message_id

    except Exception as e:
        logger.error("Failed to send draft %d to Telegram: %s", tweet_id, e)
        conn.close()
        return None


async def update_draft_message(tweet_id: int) -> None:
    """Update an existing Telegram message when a richer source replaces the draft."""
    conn = get_connection()

    row = conn.execute(
        "SELECT id, story_id, draft_text, tweet_type, status, "
        "telegram_message_id, posted_tweet_id, created_at, posted_at "
        "FROM tweets WHERE id = ?",
        (tweet_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return

    tweet = Tweet(**dict(row))
    if tweet.telegram_message_id is None:
        logger.warning("Tweet %d has no Telegram message to update", tweet_id)
        conn.close()
        return

    story_row = conn.execute(
        "SELECT topic_summary FROM stories WHERE id = ?",
        (tweet.story_id,),
    ).fetchone()
    article = get_best_article_for_story(conn, tweet.story_id)
    conn.close()

    if article is None or story_row is None:
        return

    story_summary = story_row["topic_summary"]
    message_text = _format_updated_draft_message(story_summary, article.url, tweet.draft_text)
    keyboard = _build_approval_keyboard(tweet.id)

    try:
        bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
        async with bot:
            await bot.edit_message_text(
                chat_id=get_chat_id(),
                message_id=tweet.telegram_message_id,
                text=message_text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        logger.info("Updated Telegram message %d for tweet %d", tweet.telegram_message_id, tweet_id)
    except Exception as e:
        logger.error("Failed to update Telegram message for tweet %d: %s", tweet_id, e)


# ---------------------------------------------------------------------------
# Callback handlers (for bot_server.py polling)
# ---------------------------------------------------------------------------

# Tracks which tweets are awaiting edited text from the user
_awaiting_edit: dict[int, int] = {}  # chat_id -> tweet_id


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses (Approve/Edit/Regen/Reject)."""
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
        update_tweet_status(conn, tweet_id, "approved")
        conn.close()
        await query.edit_message_text("\u2705 Tweet approved\\! Queued for posting\\.")
        logger.info("Tweet %d approved", tweet_id)

    elif action == "edit":
        conn.close()
        chat_id = query.message.chat_id if query.message else get_chat_id()
        _awaiting_edit[chat_id] = tweet_id
        await query.edit_message_text(
            "\u270f\ufe0f Send the replacement tweet text as your next message\\."
        )

    elif action == "regen":
        # Regenerate the draft using LLM
        conn.close()
        await query.edit_message_text("\U0001f504 Regenerating draft\\.\\.\\.")

        from src.drafting import regenerate_draft
        llm = _get_llm_client()
        new_draft = await regenerate_draft(llm, tweet_id)

        if new_draft is None:
            await query.edit_message_text(
                "\u274c Regeneration failed\\. Try again or edit manually\\."
            )
            return

        # Fetch story/article info for the updated message
        conn = get_connection()
        tweet_row = conn.execute(
            "SELECT story_id, telegram_message_id FROM tweets WHERE id = ?",
            (tweet_id,),
        ).fetchone()
        if tweet_row is None:
            conn.close()
            return

        story_row = conn.execute(
            "SELECT topic_summary FROM stories WHERE id = ?",
            (tweet_row["story_id"],),
        ).fetchone()
        article = get_best_article_for_story(conn, tweet_row["story_id"])
        conn.close()

        if story_row and article:
            message_text = _format_regen_draft_message(
                story_row["topic_summary"], article.url, new_draft,
            )
            keyboard = _build_approval_keyboard(tweet_id)
            await query.edit_message_text(
                text=message_text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        logger.info("Tweet %d regenerated", tweet_id)

    elif action == "reject":
        update_tweet_status(conn, tweet_id, "rejected")
        conn.close()
        await query.edit_message_text("\u274c Tweet rejected\\.")
        logger.info("Tweet %d rejected", tweet_id)

    else:
        conn.close()


async def handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages — used for the tweet edit flow."""
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
    conn.close()

    # Show the edited draft with full button row
    keyboard = _build_approval_keyboard(tweet_id)
    escaped = _escape_md2(new_text)
    char_count = len(new_text)
    await update.message.reply_text(
        f"\u270f\ufe0f *Edited draft:*\n\n"
        f"```\n{escaped}\n```\n\n"
        f"_{_escape_md2(f'{char_count}/280 chars')}_",
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )
    logger.info("Tweet %d draft updated via edit flow", tweet_id)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command."""
    if update.message:
        await update.message.reply_text(
            "Tech News Pipeline Bot active. Drafts will appear here for approval.\n\n"
            "Commands:\n"
            "/pending — list pending drafts\n"
            "/stats — pipeline statistics\n"
            "/help — show this message"
        )


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command."""
    if update.message:
        await update.message.reply_text(
            "Available commands:\n\n"
            "/pending — list all pending tweet drafts\n"
            "/stats — show pipeline statistics (stories, drafts, posts)\n"
            "/help — show this message\n\n"
            "When a draft appears, use the buttons:\n"
            "\u2705 Approve — mark for posting\n"
            "\u270f\ufe0f Edit — replace the tweet text\n"
            "\U0001f504 Regen — generate a fresh draft\n"
            "\u274c Reject — discard the draft"
        )


async def handle_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /pending command — list all pending tweet drafts."""
    if update.message is None:
        return

    conn = get_connection()
    rows = conn.execute(
        "SELECT t.id, t.draft_text, s.topic_summary "
        "FROM tweets t JOIN stories s ON t.story_id = s.id "
        "WHERE t.status = 'pending' "
        "ORDER BY t.created_at DESC"
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No pending drafts.")
        return

    lines = [f"*Pending drafts \\({_escape_md2(str(len(rows)))}\\):*\n"]
    for row in rows[:20]:  # Cap at 20 to avoid message length limits
        tid = row["id"]
        topic = _escape_md2(row["topic_summary"][:50])
        draft_preview = _escape_md2(row["draft_text"][:60]) + "\\.\\.\\."
        lines.append(f"*\\#{tid}* — {topic}\n`{draft_preview}`\n")

    if len(rows) > 20:
        lines.append(f"_\\.\\.\\.and {_escape_md2(str(len(rows) - 20))} more_")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /stats command — show pipeline statistics."""
    if update.message is None:
        return

    conn = get_connection()

    stories_today = conn.execute(
        "SELECT COUNT(*) as c FROM stories WHERE created_at >= date('now')"
    ).fetchone()["c"]

    total_stories = conn.execute(
        "SELECT COUNT(*) as c FROM stories"
    ).fetchone()["c"]

    pending = conn.execute(
        "SELECT COUNT(*) as c FROM tweets WHERE status = 'pending'"
    ).fetchone()["c"]

    approved = conn.execute(
        "SELECT COUNT(*) as c FROM tweets WHERE status = 'approved'"
    ).fetchone()["c"]

    posted = conn.execute(
        "SELECT COUNT(*) as c FROM tweets WHERE status = 'posted'"
    ).fetchone()["c"]

    rejected = conn.execute(
        "SELECT COUNT(*) as c FROM tweets WHERE status = 'rejected'"
    ).fetchone()["c"]

    total_articles = conn.execute(
        "SELECT COUNT(*) as c FROM articles"
    ).fetchone()["c"]

    conn.close()

    await update.message.reply_text(
        f"*Pipeline Stats*\n\n"
        f"\U0001f4f0 Stories today: {_escape_md2(str(stories_today))}\n"
        f"\U0001f4da Total stories: {_escape_md2(str(total_stories))}\n"
        f"\U0001f4c4 Total articles: {_escape_md2(str(total_articles))}\n\n"
        f"*Tweets:*\n"
        f"\u23f3 Pending: {_escape_md2(str(pending))}\n"
        f"\u2705 Approved: {_escape_md2(str(approved))}\n"
        f"\U0001f4e8 Posted: {_escape_md2(str(posted))}\n"
        f"\u274c Rejected: {_escape_md2(str(rejected))}",
        parse_mode="MarkdownV2",
    )


def build_application() -> Application:  # type: ignore[type-arg]
    """Build and configure the Telegram bot Application for polling."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("pending", handle_pending))
    app.add_handler(CommandHandler("stats", handle_stats))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_text))

    return app
