# Tech News Pipeline

Automated system that ingests tech news via RSS, clusters related articles, drafts tweets using an LLM, sends them for human approval via Telegram, and posts approved tweets to X/Twitter.

## Quick Start

### 1. Clone and set up virtual environment

```bash
cd tech-news-pipeline
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

Create a `.env` file in the project root with the following keys:

```
GROQ_API_KEY=...
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TWITTER_API_KEY=...
TWITTER_API_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_SECRET=...
```

Where to get them:
- **Groq API key** — primary LLM provider (free tier available at console.groq.com)
- **Google Gemini API key** — fallback LLM (free tier at aistudio.google.com)
- **Telegram Bot Token** — create via @BotFather on Telegram
- **Telegram Chat ID** — your personal chat ID (use @userinfobot)
- **Twitter/X API credentials** — from developer.x.com (Free tier)

### 4. Initialize the database

```bash
python -c "from src.db import init_db; init_db()"
```

This creates `pipeline.db` with the required tables.

### 5. Run the pipeline (one-shot)

```bash
python main.py
```

This executes: RSS ingestion -> article clustering -> tweet drafting -> update detection.

### 6. Start the Telegram bot

```bash
python bot_server.py
```

This runs the long-polling bot that listens for Approve/Edit/Reject actions on tweet drafts. Run this as a persistent process (e.g., systemd service).

### 7. Set up cron (production)

Add to crontab to run the pipeline every 30 minutes:

```bash
crontab -e
# Add:
*/30 * * * * cd /path/to/tech-news-pipeline && /path/to/.venv/bin/python main.py >> /dev/null 2>&1
```

## Architecture

```
Cron (main.py)          Bot Server (bot_server.py)
    |                        |
    v                        v
RSS Feeds --> Ingestion --> Clustering --> Drafting --> Telegram --> Twitter
              (feedparser)  (LLM)         (LLM)       (approve)    (tweepy)
```

- **LLM**: Groq (Llama 3.3 70B) primary, Google Gemini 2.0 Flash fallback
- **Database**: SQLite with WAL mode
- **Hosting**: Oracle Cloud Free Tier (single VM)

## Configuration

- `config/feeds.yaml` — RSS feed URLs
- `config/settings.yaml` — Pipeline intervals, LLM params, rate limits
- `config/style_guide.txt` — Example tweets for LLM few-shot prompting
