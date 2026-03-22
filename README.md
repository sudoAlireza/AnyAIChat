# AnyAIChat - Multi-Provider AI Telegram Bot

> **Note:** This project has been rebranded from GeminiBot to AnyAIChat to better reflect its multi-provider capabilities. The bot now supports any major AI provider, not just Google Gemini.

AnyAIChat is a feature-rich Telegram bot that supports multiple AI providers including **Google Gemini**, **OpenAI**, **Anthropic (Claude)**, and **OpenAI-compatible endpoints** (OpenRouter, Groq, Together AI). Users can switch providers, bring their own API keys, and enjoy advanced features like streaming responses, vision, knowledge base with RAG, task scheduling, and more.

[Set-up Tutorial on Medium](https://medium.com/@alirezafathi/how-to-use-google-gemini-ai-in-your-personal-telegram-bot-on-your-own-server-b1f0b9de2bdd)

## Getting Started

### Prerequisites

- Python 3.12+
- A [Telegram Bot Token](https://core.telegram.org/bots) from BotFather
- At least one AI provider API key:
  - [Gemini API key](https://makersuite.google.com/app/apikey)
  - [OpenAI API key](https://platform.openai.com/api-keys)
  - [Anthropic API key](https://console.anthropic.com/)
- Your Telegram account ID from [Show Json Bot](https://t.me/ShowJsonBot) (used for access control)

### Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/sudoAlireza/AnyAIChat.git
   cd AnyAIChat
   ```

2. Create and activate a virtual environment:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

### Environment Variables

The bot is configured using environment variables:

**Required:**

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather |
| `AUTHORIZED_USER` | Comma-separated Telegram user IDs for access control |

**AI Providers (at least one required):**

| Variable | Description | Default |
|---|---|---|
| `GEMINI_API_TOKEN` | Google Gemini API key | — |
| `OPENAI_API_KEY` | OpenAI API key | — |
| `ANTHROPIC_API_KEY` | Anthropic (Claude) API key | — |
| `OPENROUTER_API_KEY` | OpenRouter API key | — |
| `GROQ_API_KEY` | Groq API key | — |

**Optional:**

| Variable | Description | Default |
|---|---|---|
| `GEMINI_MODEL` | Default Gemini model | `gemini-1.5-flash` |
| `LANGUAGE` | Bot interface language (`en`, `ru`) | `en` |
| `LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `ENCRYPTION_KEY` | Key for encrypting user-provided API keys in the database | — |
| `ALLOW_ALL_USERS` | Allow any Telegram user to use the bot | `false` |
| `RATE_LIMIT_PER_MINUTE` | Max requests per minute per user | `30` |
| `RATE_LIMIT_PER_HOUR` | Max requests per hour per user | `200` |
| `MAX_HISTORY_MESSAGES` | Max messages kept in conversation history | `100` |
| `DATABASE_PATH` | Path to the SQLite database | `data/gemini_bot.db` |
| `SAFETY_OVERRIDE` | Override Gemini safety settings | — |
| `CACHE_TTL_SECONDS` | Context cache TTL (Gemini) | `3600` |
| `EMBEDDING_MODEL` | Model for RAG embeddings | `text-embedding-004` |

### Running the Bot

```bash
python main.py
```

### Deployment with Docker

```bash
docker-compose up -d --build
```

The `data` directory is mounted as a volume to persist conversation data and the database across container restarts.

```bash
docker-compose logs -f    # View logs
docker-compose down        # Stop the bot
```

## Features

### Multi-Provider AI Support
- **Google Gemini** — text, vision, image generation, code execution, web search, thinking modes, context caching
- **OpenAI (GPT)** — text, vision, image generation (DALL-E), web search, reasoning models (o1/o3/o4)
- **Anthropic (Claude)** — text, vision, extended thinking with configurable budgets
- **OpenAI-compatible** — OpenRouter, Groq, Together AI, or custom endpoints
- Switch providers and models on the fly from the settings menu
- Bring Your Own Key (BYOK) — users can set their own API keys per provider

### Conversation Management
- Streaming responses with real-time updates
- Full conversation history with search, tags, and branching
- Resume previous conversations
- Export conversations
- Per-message feedback (thumbs up/down)
- Automatic conversation length warnings and resets

### Knowledge Base & RAG
- Upload documents to build a personal knowledge base
- RAG-powered context injection for more informed responses
- Per-user knowledge base isolation

### Personalization
- Custom system persona / instructions
- Pinned context (persistent notes injected into every message)
- Quick shortcuts for common prompts
- Prompt library
- Message bookmarks

### Task Automation
- Schedule one-time and recurring tasks
- Reminders with daily/weekly intervals
- Daily briefings at a configurable time
- URL monitoring with change detection

### Media & Output
- Send images with captions for vision analysis
- Image generation (Gemini, DALL-E)
- Voice output via text-to-speech (gTTS)
- Follow-up suggestions

### Security & Access Control
- User restriction by Telegram account ID
- Rate limiting (per-minute and per-hour)
- Encrypted API key storage in the database
- Configurable safety settings

### Internationalization
- Multi-language support via `gettext` and `Babel`
- Currently available: English (`en`), Russian (`ru`)

## Project Structure

```
.
├── main.py                  # Entry point, scheduler setup
├── config.py                # Environment variable configuration
├── core.py                  # Legacy Gemini class (being phased out)
├── providers/               # Multi-provider AI abstraction
│   ├── base.py              # AIProvider protocol & error hierarchy
│   ├── registry.py          # Provider registry (singleton)
│   ├── gemini.py            # Google Gemini provider
│   ├── openai_provider.py   # OpenAI provider
│   ├── anthropic_provider.py # Anthropic (Claude) provider
│   └── openai_compat.py     # OpenAI-compatible endpoints
├── chat/                    # Provider-agnostic chat layer
│   ├── session.py           # ChatSession abstraction
│   └── system_prompt.py     # Composable system prompt builder
├── handlers/                # Telegram bot command handlers
│   ├── common.py            # Shared utilities, auth decorator, rate limiting
│   ├── conversation.py      # Core chat loop, streaming, file handling
│   ├── settings.py          # Settings menu, provider/model switching
│   ├── onboarding.py        # First-time setup, API key input
│   ├── history.py           # Conversation history management
│   ├── knowledge.py         # Knowledge base & RAG
│   ├── tasks.py             # Task scheduling
│   ├── reminders.py         # Reminder management
│   ├── prompts.py           # Prompt library
│   ├── bookmarks.py         # Message bookmarks
│   ├── templates.py         # Template system
│   ├── media.py             # Image generation, voice, suggestions
│   ├── briefing.py          # Daily briefings, URL monitoring
│   └── feedback.py          # User feedback collection
├── database/                # Async SQLite database layer
│   ├── database.py          # Connection pool & migrations
│   └── repositories/        # Data access repositories
├── helpers/                 # Utility functions & inline paginator
├── security/                # API key encryption
├── monitoring/              # Telemetry & metrics
├── locales/                 # Translation files (en, ru)
├── tests/                   # Test suite
├── Dockerfile               # Python 3.12-slim container
├── docker-compose.yml       # Docker Compose configuration
├── requirements.txt         # Python dependencies
└── babel.cfg                # Babel extraction config
```

## Internationalization (i18n)

### Adding a New Language

To add a new language (e.g., Spanish — `es`):

1. Initialize the language catalog:

   ```bash
   pybabel init -i locales/messages.pot -d locales -l es
   ```

2. Translate the strings in `locales/es/LC_MESSAGES/messages.po`.

3. Compile translations:

   ```bash
   pybabel compile -d locales
   ```

### Updating Existing Translations

When new translatable strings are added to the code:

1. Extract strings:

   ```bash
   pybabel extract -F babel.cfg -o locales/messages.pot .
   ```

2. Update catalogs:

   ```bash
   pybabel update -i locales/messages.pot -d locales
   ```

3. Translate new entries in the `.po` files.

4. Compile:

   ```bash
   pybabel compile -d locales
   ```

## To-Do

- [x] Removing Specific Conversation from History
- [x] Multi-provider support (Gemini, OpenAI, Anthropic)
- [x] Streaming responses
- [x] Knowledge base with RAG
- [x] Task scheduling & reminders
- [x] BYOK (Bring Your Own Key) per provider
- [ ] Add Conversation Feature to Images Part
- [ ] Handle Long Responses in Multiple Messages
- [ ] Improve Test Coverage

## Documentation

- [Telegram Bots Documentation](https://core.telegram.org/bots)
- [Gemini API: Quickstart with Python](https://ai.google.dev/tutorials/python_quickstart)
- [OpenAI API Documentation](https://platform.openai.com/docs)
- [Anthropic API Documentation](https://docs.anthropic.com/)

## Security

Ensure the security of your API keys and sensitive information. The bot supports encrypted API key storage when `ENCRYPTION_KEY` is set. Follow best practices for securing API keys and tokens.

## Contributing

Contributions to AnyAIChat are encouraged. Feel free to submit issues and pull requests.
