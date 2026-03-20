import os
from dotenv import load_dotenv

load_dotenv()


# Database
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/gemini_bot.db")

# Gemini
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_MAX_WORKERS = int(os.getenv("GEMINI_MAX_WORKERS", "20"))

# Pagination & Limits
ITEMS_PER_PAGE = int(os.getenv("ITEMS_PER_PAGE", "10"))
MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "4000"))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "100"))

# Scheduler
REMINDER_CHECK_INTERVAL_MINUTES = int(os.getenv("REMINDER_CHECK_INTERVAL_MINUTES", "1"))

# Rate Limiting
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "200"))

# Security
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
ALLOW_ALL_USERS = os.getenv("ALLOW_ALL_USERS", "false").lower() == "true"
AUTHORIZED_USER = os.getenv("AUTHORIZED_USER", "")
SAFETY_OVERRIDE = os.getenv("SAFETY_OVERRIDE", "")

# Bot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_TOKEN = os.getenv("GEMINI_API_TOKEN", "")
LANGUAGE = os.getenv("LANGUAGE", "en")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Conversation
CONVERSATION_WARNING_THRESHOLD = int(os.getenv("CONVERSATION_WARNING_THRESHOLD", "500"))
CONVERSATION_AUTO_RESET_THRESHOLD = int(os.getenv("CONVERSATION_AUTO_RESET_THRESHOLD", "750"))

# Temp file cleanup
TEMP_FILE_MAX_AGE_HOURS = int(os.getenv("TEMP_FILE_MAX_AGE_HOURS", "1"))

# Context Caching
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))  # 1 hour
CACHE_MIN_TOKENS = int(os.getenv("CACHE_MIN_TOKENS", "32768"))  # 32K minimum

# Embeddings / RAG
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-004")
RAG_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "2000"))
RAG_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "400"))
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
