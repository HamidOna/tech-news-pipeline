"""Long-running Telegram bot polling server.

Run as a separate process (e.g., systemd service) from the cron pipeline.

Usage:
    python bot_server.py
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from src.db import init_db
from src.telegram_bot import build_application


def setup_logging() -> None:
    """Configure logging for the bot server."""
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=5_242_880,
        backupCount=3,
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)


def main() -> None:
    """Start the Telegram bot polling loop."""
    load_dotenv()
    setup_logging()

    logger = logging.getLogger(__name__)
    logger.info("Initializing database...")
    init_db()

    logger.info("Starting Telegram bot polling...")
    app = build_application()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
