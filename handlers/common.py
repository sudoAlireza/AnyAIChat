"""Shared utilities, decorators, and context helpers for all handlers."""

from __future__ import annotations

import contextvars
import logging
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from config import AUTHORIZED_USER, ALLOW_ALL_USERS
from security.rate_limiter import rate_limiter
from monitoring.metrics import metrics
from providers.base import Capability

# Context variable to propagate user_id into all log records within a handler
_current_user_id = contextvars.ContextVar("current_user_id", default="-")

logger = logging.getLogger(__name__)


# Translation function placeholder
def _(text: str) -> str:
    import builtins
    if "_" in builtins.__dict__:
        return builtins.__dict__["_"](text)
    return text


def _get_pool(context: ContextTypes.DEFAULT_TYPE):
    """Get database pool from bot_data."""
    return context.bot_data["db_pool"]


def restricted(func):
    """Access control + rate limiting decorator."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id

        if not AUTHORIZED_USER:
            if not ALLOW_ALL_USERS:
                logger.warning(f"Access denied for {user_id}: AUTHORIZED_USER not set and ALLOW_ALL_USERS is false")
                if update.message:
                    await update.message.reply_text("Bot is not configured for public access. Contact the administrator.")
                elif update.callback_query:
                    await update.callback_query.answer("Bot not configured for public access.", show_alert=True)
                return
        else:
            authorized_users = [int(u.strip()) for u in AUTHORIZED_USER.split(",") if u.strip()]
            if authorized_users and user_id not in authorized_users:
                logger.info(f"Unauthorized access denied for {user_id}.")
                if update.message:
                    await update.message.reply_text("You are not authorized to use this bot.")
                elif update.callback_query:
                    await update.callback_query.answer("Unauthorized.", show_alert=True)
                return

        if not rate_limiter.is_allowed(user_id):
            wait_time = rate_limiter.get_wait_time(user_id)
            msg = f"Rate limit exceeded. Please wait {int(wait_time)} seconds."
            if update.message:
                await update.message.reply_text(msg)
            elif update.callback_query:
                await update.callback_query.answer(msg, show_alert=True)
            metrics.increment("rate_limit_hits")
            return

        _current_user_id.set(str(user_id))

        # Hydrate user context from DB if not cached (e.g. after bot restart)
        if "active_provider" not in context.user_data:
            try:
                from database.database import get_user, get_user_provider_settings
                pool = _get_pool(context)
                user = await get_user(pool, user_id)
                if user:
                    active_provider = user.get("active_provider") or "gemini"
                    context.user_data["active_provider"] = active_provider
                    context.user_data.setdefault("api_key", user.get("api_key"))  # DEPRECATED: legacy single-key field
                    context.user_data.setdefault("web_search", bool(user.get("grounding")))
                    context.user_data.setdefault("system_instruction", user.get("system_instruction"))
                    # Load per-provider model
                    prov_settings = await get_user_provider_settings(pool, user_id, active_provider)
                    if prov_settings and prov_settings.get("model_name"):
                        context.user_data["model_name"] = prov_settings["model_name"]
                    else:
                        context.user_data.setdefault("model_name", user.get("model_name"))
            except Exception:
                pass

        return await func(update, context, *args, **kwargs)

    return wrapped


async def _clear_last_ai_buttons(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove inline buttons from the previous AI response message."""
    last = context.user_data.pop("last_ai_message_id", None)
    chat = context.user_data.pop("last_ai_chat_id", None)
    if last and chat:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat, message_id=last, reply_markup=None
            )
        except BadRequest:
            pass


def require_capability(capability: Capability, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Check if the user's active provider supports a capability.

    Returns None if the capability is supported, or a user-friendly error message.
    """
    from providers.registry import ProviderRegistry
    provider_name = context.user_data.get("active_provider", "gemini")
    provider = ProviderRegistry().get(provider_name)
    if not provider:
        return f"Provider '{provider_name}' is not available."
    if capability not in provider.capabilities:
        cap_name = capability.name.replace("_", " ").title()
        return f"{cap_name} is not supported by {provider.provider_name}."
    return None


def get_active_provider_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Get the user's active provider name from context."""
    return context.user_data.get("active_provider", "gemini")


async def get_api_key(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    """Get the user's API key for the active provider.

    Lookup order:
    1. context.user_data cache (keyed by provider)
    2. user_api_keys table (per-provider)
    3. Legacy users.api_key column (Gemini only)
    4. GEMINI_API_TOKEN env var (last resort)
    """
    from database.database import get_user_api_key, get_user

    provider = context.user_data.get("active_provider", "gemini")
    cache_key = f"api_key_{provider}"

    # 1. Check per-provider cache
    cached = context.user_data.get(cache_key)
    if cached:
        return cached

    pool = _get_pool(context)

    # 2. Per-provider key from user_api_keys table
    provider_key = await get_user_api_key(pool, user_id, provider)
    if provider_key and provider_key.get("api_key"):
        context.user_data[cache_key] = provider_key["api_key"]
        # Also set generic api_key for backward compat
        context.user_data["api_key"] = provider_key["api_key"]
        return provider_key["api_key"]

    # DEPRECATED fallback 3: Legacy key from users table (works for gemini only)
    user = await get_user(pool, user_id)
    if user and user.get("api_key"):
        context.user_data[cache_key] = user["api_key"]
        context.user_data["api_key"] = user["api_key"]
        return user["api_key"]

    # DEPRECATED fallback 4: Env var fallback (Gemini only)
    from config import GEMINI_API_TOKEN
    return GEMINI_API_TOKEN
