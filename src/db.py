"""SQLite database setup, schema initialization, and helper queries."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "pipeline.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    source_name TEXT,
    story_id INTEGER REFERENCES stories(id),
    is_best_source BOOLEAN DEFAULT 0,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tweets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER NOT NULL REFERENCES stories(id),
    draft_text TEXT NOT NULL,
    tweet_type TEXT NOT NULL DEFAULT 'original',
    status TEXT NOT NULL DEFAULT 'pending',
    telegram_message_id INTEGER,
    posted_tweet_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    posted_at TIMESTAMP
);
"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Story:
    id: int
    topic_summary: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass
class Article:
    id: int
    url: str
    title: str
    summary: Optional[str]
    source_name: Optional[str]
    story_id: Optional[int]
    is_best_source: bool
    fetched_at: datetime


@dataclass
class Tweet:
    id: int
    story_id: int
    draft_text: str
    tweet_type: str
    status: str
    telegram_message_id: Optional[int]
    posted_tweet_id: Optional[str]
    created_at: datetime
    posted_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Return a connection with WAL mode and row factory enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    """Create all tables if they don't exist."""
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Story helpers
# ---------------------------------------------------------------------------

def create_story(conn: sqlite3.Connection, topic_summary: str) -> int:
    """Insert a new story cluster and return its ID."""
    cur = conn.execute(
        "INSERT INTO stories (topic_summary) VALUES (?)",
        (topic_summary,),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_recent_stories(conn: sqlite3.Connection, hours: int = 48) -> list[Story]:
    """Fetch active stories created within the last `hours` hours."""
    rows = conn.execute(
        """
        SELECT id, topic_summary, status, created_at, updated_at
        FROM stories
        WHERE status = 'active'
          AND created_at >= datetime('now', ?)
        ORDER BY created_at DESC
        """,
        (f"-{hours} hours",),
    ).fetchall()
    return [Story(**dict(r)) for r in rows]


def update_story_timestamp(conn: sqlite3.Connection, story_id: int) -> None:
    """Touch the updated_at timestamp for a story."""
    conn.execute(
        "UPDATE stories SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (story_id,),
    )
    conn.commit()


def mark_stale_stories(conn: sqlite3.Connection, stale_after_hours: int = 72) -> int:
    """Mark stories older than `stale_after_hours` as stale. Return count."""
    cur = conn.execute(
        """
        UPDATE stories
        SET status = 'stale'
        WHERE status = 'active'
          AND updated_at < datetime('now', ?)
        """,
        (f"-{stale_after_hours} hours",),
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Article helpers
# ---------------------------------------------------------------------------

def article_exists(conn: sqlite3.Connection, url: str) -> bool:
    """Check if an article URL is already stored."""
    row = conn.execute("SELECT 1 FROM articles WHERE url = ?", (url,)).fetchone()
    return row is not None


def insert_article(
    conn: sqlite3.Connection,
    url: str,
    title: str,
    summary: Optional[str],
    source_name: Optional[str],
) -> int:
    """Insert a new article (unassigned to any story). Return its ID."""
    cur = conn.execute(
        """
        INSERT INTO articles (url, title, summary, source_name)
        VALUES (?, ?, ?, ?)
        """,
        (url, title, summary, source_name),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def assign_article_to_story(
    conn: sqlite3.Connection,
    article_id: int,
    story_id: int,
    is_best_source: bool = False,
) -> None:
    """Link an article to a story cluster."""
    conn.execute(
        "UPDATE articles SET story_id = ?, is_best_source = ? WHERE id = ?",
        (story_id, int(is_best_source), article_id),
    )
    conn.commit()


def get_best_article_for_story(conn: sqlite3.Connection, story_id: int) -> Optional[Article]:
    """Return the best-source article for a story, or None."""
    row = conn.execute(
        """
        SELECT id, url, title, summary, source_name, story_id,
               is_best_source, fetched_at
        FROM articles
        WHERE story_id = ? AND is_best_source = 1
        """,
        (story_id,),
    ).fetchone()
    if row is None:
        return None
    return Article(**dict(row))


def get_articles_for_story(conn: sqlite3.Connection, story_id: int) -> list[Article]:
    """Return all articles in a story cluster."""
    rows = conn.execute(
        """
        SELECT id, url, title, summary, source_name, story_id,
               is_best_source, fetched_at
        FROM articles
        WHERE story_id = ?
        ORDER BY fetched_at DESC
        """,
        (story_id,),
    ).fetchall()
    return [Article(**dict(r)) for r in rows]


def get_unassigned_articles(conn: sqlite3.Connection) -> list[Article]:
    """Return articles not yet assigned to any story."""
    rows = conn.execute(
        """
        SELECT id, url, title, summary, source_name, story_id,
               is_best_source, fetched_at
        FROM articles
        WHERE story_id IS NULL
        ORDER BY fetched_at ASC
        """,
    ).fetchall()
    return [Article(**dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Tweet helpers
# ---------------------------------------------------------------------------

def create_tweet(
    conn: sqlite3.Connection,
    story_id: int,
    draft_text: str,
    tweet_type: str = "original",
) -> int:
    """Insert a new tweet draft. Return its ID."""
    cur = conn.execute(
        """
        INSERT INTO tweets (story_id, draft_text, tweet_type)
        VALUES (?, ?, ?)
        """,
        (story_id, draft_text, tweet_type),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_pending_tweet_for_story(conn: sqlite3.Connection, story_id: int) -> Optional[Tweet]:
    """Return the pending tweet for a story, if any."""
    row = conn.execute(
        """
        SELECT id, story_id, draft_text, tweet_type, status,
               telegram_message_id, posted_tweet_id, created_at, posted_at
        FROM tweets
        WHERE story_id = ? AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (story_id,),
    ).fetchone()
    if row is None:
        return None
    return Tweet(**dict(row))


def update_tweet_status(
    conn: sqlite3.Connection,
    tweet_id: int,
    status: str,
    posted_tweet_id: Optional[str] = None,
) -> None:
    """Update a tweet's status and optionally store the posted tweet ID."""
    if posted_tweet_id:
        conn.execute(
            """
            UPDATE tweets
            SET status = ?, posted_tweet_id = ?, posted_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, posted_tweet_id, tweet_id),
        )
    else:
        conn.execute(
            "UPDATE tweets SET status = ? WHERE id = ?",
            (status, tweet_id),
        )
    conn.commit()


def update_tweet_draft(conn: sqlite3.Connection, tweet_id: int, new_text: str) -> None:
    """Replace the draft text for a pending tweet."""
    conn.execute(
        "UPDATE tweets SET draft_text = ? WHERE id = ?",
        (new_text, tweet_id),
    )
    conn.commit()


def set_telegram_message_id(
    conn: sqlite3.Connection, tweet_id: int, telegram_message_id: int
) -> None:
    """Store the Telegram message ID associated with a tweet draft."""
    conn.execute(
        "UPDATE tweets SET telegram_message_id = ? WHERE id = ?",
        (telegram_message_id, tweet_id),
    )
    conn.commit()


def get_tweet_by_telegram_message_id(
    conn: sqlite3.Connection, telegram_message_id: int
) -> Optional[Tweet]:
    """Look up a tweet by its Telegram message ID."""
    row = conn.execute(
        """
        SELECT id, story_id, draft_text, tweet_type, status,
               telegram_message_id, posted_tweet_id, created_at, posted_at
        FROM tweets
        WHERE telegram_message_id = ?
        """,
        (telegram_message_id,),
    ).fetchone()
    if row is None:
        return None
    return Tweet(**dict(row))
