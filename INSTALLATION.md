# How to Deploy GeminiBot — Multi-Provider AI Telegram Bot

## Introduction

GeminiBot has evolved from a simple Gemini chatbot into a full-featured multi-provider AI Telegram bot. It supports **Google Gemini**, **OpenAI (GPT)**, **Anthropic (Claude)**, and **OpenAI-compatible endpoints** (OpenRouter, Groq, Together AI). Users can switch between providers, bring their own API keys, and take advantage of features like streaming, vision, knowledge base with RAG, task scheduling, and more.

## Features

- **Multi-provider AI** — Gemini, OpenAI, Anthropic, and OpenAI-compatible endpoints
- Streaming responses with real-time updates
- Vision / image analysis and image generation
- Knowledge base with RAG (Retrieval-Augmented Generation)
- Conversation history with search, tags, branching, and export
- Task scheduling, reminders, and daily briefings
- Custom personas, pinned context, prompt library, and bookmarks
- BYOK (Bring Your Own Key) — users set their own API keys per provider
- Rate limiting, encrypted key storage, and user access control
- Multi-language support (English, Russian)

## Project Structure

```
.
├── main.py                  # Entry point, scheduler setup
├── config.py                # Configuration from environment variables
├── core.py                  # Legacy Gemini class (being phased out)
├── providers/               # Multi-provider AI abstraction
│   ├── base.py              # AIProvider protocol & error hierarchy
│   ├── registry.py          # Provider registry (singleton)
│   ├── gemini.py            # Google Gemini provider
│   ├── openai_provider.py   # OpenAI provider
│   ├── anthropic_provider.py # Anthropic (Claude) provider
│   └── openai_compat.py     # OpenAI-compatible endpoints
├── chat/                    # Provider-agnostic chat session layer
│   ├── session.py           # ChatSession abstraction
│   └── system_prompt.py     # Composable system prompt builder
├── handlers/                # Telegram bot command handlers
│   ├── common.py            # Auth decorator, rate limiting, shared utils
│   ├── conversation.py      # Core chat loop, streaming, file handling
│   ├── settings.py          # Settings menu, provider/model switching
│   ├── onboarding.py        # First-time setup and API key input
│   ├── history.py           # Conversation history management
│   ├── knowledge.py         # Knowledge base & RAG
│   ├── tasks.py             # Task scheduling
│   ├── reminders.py         # Reminder management
│   └── ...                  # Additional feature handlers
├── database/                # Async SQLite with migrations
│   ├── database.py          # Connection pool & migration runner
│   └── repositories/        # Data access repositories
├── helpers/                 # Utility functions
├── security/                # API key encryption
├── monitoring/              # Telemetry & metrics
├── locales/                 # Translation files (en, ru)
├── tests/                   # Test suite
├── Dockerfile               # Python 3.12-slim container
├── docker-compose.yml       # Docker Compose configuration
└── requirements.txt         # Python dependencies
```

The bot uses an async SQLite database (via `aiosqlite`) with an automatic migration system. Conversation data, user settings, provider configurations, and knowledge bases are stored in a single database file at `data/gemini_bot.db`.

## Prerequisites

- **Python 3.12+**
- A [Telegram Bot Token](https://core.telegram.org/bots) from BotFather
- At least one AI provider API key:
  - [Gemini API key](https://makersuite.google.com/app/apikey) from Google AI Studio
  - [OpenAI API key](https://platform.openai.com/api-keys)
  - [Anthropic API key](https://console.anthropic.com/)
- Your Telegram account ID from [Show Json Bot](https://t.me/ShowJsonBot) (different from your username — used for access control)

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather |
| `AUTHORIZED_USER` | Comma-separated Telegram user IDs for access control |

### AI Providers (at least one required)

| Variable | Description |
|---|---|
| `GEMINI_API_TOKEN` | Google Gemini API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `ANTHROPIC_API_KEY` | Anthropic (Claude) API key |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `GROQ_API_KEY` | Groq API key |

### Optional

| Variable | Description | Default |
|---|---|---|
| `GEMINI_MODEL` | Default Gemini model | `gemini-1.5-flash` |
| `LANGUAGE` | Bot interface language (`en`, `ru`) | `en` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `ENCRYPTION_KEY` | Key for encrypting user API keys in the database | — |
| `ALLOW_ALL_USERS` | Allow all Telegram users (no restriction) | `false` |
| `RATE_LIMIT_PER_MINUTE` | Max requests per minute per user | `30` |
| `RATE_LIMIT_PER_HOUR` | Max requests per hour per user | `200` |
| `MAX_HISTORY_MESSAGES` | Max messages in conversation history | `100` |
| `DATABASE_PATH` | SQLite database file path | `data/gemini_bot.db` |
| `SAFETY_OVERRIDE` | Override Gemini safety settings | — |
| `CACHE_TTL_SECONDS` | Context cache TTL for Gemini | `3600` |
| `EMBEDDING_MODEL` | Model for RAG embeddings | `text-embedding-004` |
| `RAG_CHUNK_SIZE` | RAG document chunk size | `2000` |
| `RAG_TOP_K` | Number of RAG chunks to retrieve | `5` |

## Local Development

1. **Clone and set up the environment:**

   ```bash
   git clone https://github.com/sudoAlireza/GeminiBot.git
   cd GeminiBot
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Set environment variables:**

   ```bash
   export TELEGRAM_BOT_TOKEN=<Your Telegram Bot Token>
   export AUTHORIZED_USER="<your_user_id_1>,<your_user_id_2>"
   export LANGUAGE=en

   # Set at least one provider key:
   export GEMINI_API_TOKEN=<Your Gemini API key>
   # export OPENAI_API_KEY=<Your OpenAI API key>
   # export ANTHROPIC_API_KEY=<Your Anthropic API key>

   # Optional: enable encrypted API key storage
   # export ENCRYPTION_KEY=<a random secret string>
   ```

3. **Run the bot:**

   ```bash
   python main.py
   ```

## Deployment with Docker

The recommended way to deploy the bot is using Docker and Docker Compose.

**Data Persistence:** The bot stores all data (database, knowledge base) in the `data` directory, which is mounted as a Docker volume.

**Configuration:** Set environment variables through your orchestration platform (Portainer, Kubernetes, etc.) or export them in your shell before running `docker-compose`.

**Build and Run:**

```bash
docker-compose up -d --build
```

**View logs:**

```bash
docker-compose logs -f
```

**Stop:**

```bash
docker-compose down
```

The container includes a health check that runs every 60 seconds.

## Deploy on a Linux Server with Supervisor

1. **Clone and install:**

   ```bash
   git clone https://github.com/sudoAlireza/GeminiBot.git
   cd GeminiBot
   python3 -m venv venv --prompt GeminiBot
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Set environment variables in your shell** (see the Environment Variables section above).

3. **Install Supervisor:**

   ```bash
   sudo apt update
   sudo apt install supervisor -y
   ```

4. **Create a Supervisor config file:**

   ```bash
   sudo vim /etc/supervisor/conf.d/gemini_bot.conf
   ```

   Use this template (replace `<PROJECT_DIR>` with the actual path):

   ```ini
   [program:gemini_bot]
   command=<PROJECT_DIR>/venv/bin/python3 <PROJECT_DIR>/main.py
   directory=<PROJECT_DIR>
   restarts=4
   autostart=true
   autorestart=true
   log_stderr=true
   log_stdout=true
   stderr_logfile=/var/log/supervisor/gemini_bot.err.log
   stdout_logfile=/var/log/supervisor/gemini_bot.out.log
   environment=TELEGRAM_BOT_TOKEN="...",GEMINI_API_TOKEN="...",AUTHORIZED_USER="...",LANGUAGE="en"
   ```

5. **Start the bot:**

   ```bash
   sudo supervisorctl reread
   sudo supervisorctl update
   sudo supervisorctl start gemini_bot
   ```

## Running Tests

```bash
pip install pytest pytest-asyncio pytest-cov
pytest
```

## Ideas for Further Development

- **Conversation feature for images** — maintain multi-turn conversations about images across providers
- **Long response handling** — split responses exceeding Telegram's 4096-character limit into multiple messages
- **Expanded test coverage** — integration and unit tests for all providers and handlers
- **More providers** — add support for additional AI providers via the plugin-based registry
- **Group chat support** — allow the bot to participate in Telegram group conversations

Feel free to share ideas or open issues and pull requests in the [GeminiBot](https://github.com/sudoAlireza/GeminiBot) GitHub repository.
