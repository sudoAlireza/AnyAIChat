"""Settings menu, model selection, storage, persona, toggles, shortcuts, pinned context, and language handlers."""

import re
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from handlers.common import restricted, _, _get_pool, get_active_provider_name
from handlers.states import (
    SETTINGS_MENU, MODELS_MENU, STORAGE_MENU, API_KEY_INPUT,
    PERSONA_INPUT, SHORTCUTS_MENU, SHORTCUTS_INPUT, PINNED_CONTEXT_INPUT,
)
from config import GEMINI_MODEL, GEMINI_API_TOKEN
from database.database import (
    get_user, update_user_settings,
    add_shortcut, get_user_shortcuts, delete_shortcut,
    get_user_api_key, set_active_provider, get_user_custom_providers,
)
from providers.registry import ProviderRegistry
from providers.base import Capability
from helpers.helpers import strip_markdown

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings Menu
# ---------------------------------------------------------------------------

@restricted
async def open_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    user_id = update.effective_user.id
    pool = _get_pool(context)
    user = await get_user(pool, user_id)

    logger.info(f"Opening settings menu for user {user_id}")

    current_model = user['model_name'] if user and user.get('model_name') else (context.user_data.get("model_name") or GEMINI_MODEL)
    web_search = bool(user['grounding']) if user and user.get('grounding') is not None else context.user_data.get("web_search", False)

    ws_status = "\u2705 Enabled" if web_search else "\u274c Disabled"

    user_lang = user.get('language', 'auto') if user else 'auto'

    # Thinking mode status
    thinking_mode = user.get('thinking_mode', 'off') if user else 'off'
    thinking_labels = {"off": "\u274c Off", "light": "\U0001f4a1 Light", "medium": "\U0001f9e0 Medium", "deep": "\U0001f52e Deep"}
    thinking_status = thinking_labels.get(thinking_mode, "\u274c Off")

    # Code execution status
    code_exec = user.get('code_execution', False) if user else False
    code_exec_status = "\u2705 Enabled" if code_exec else "\u274c Disabled"

    # Get the active provider to check capabilities
    provider_name = get_active_provider_name(context)
    provider = ProviderRegistry().get(provider_name)

    keyboard = [
        [InlineKeyboardButton(f"\U0001f916 Model: {current_model}", callback_data="open_models_menu")],
        [InlineKeyboardButton(f"\U0001f504 Provider: {provider_name.title()}", callback_data="Provider_Menu")],
        [InlineKeyboardButton(f"\U0001f3ad Custom Persona", callback_data="Persona_Menu")],
        [InlineKeyboardButton(f"\U0001f4cc Pinned Context", callback_data="Pinned_Context_Menu")],
        [InlineKeyboardButton(f"\u26a1 Quick Shortcuts", callback_data="Shortcuts_Menu")],
    ]

    # Only show toggles if the active provider supports them
    if provider and Capability.WEB_SEARCH in provider.capabilities:
        keyboard.append([InlineKeyboardButton(f"\U0001f310 Web Search: {ws_status}", callback_data="TOGGLE_WEB_SEARCH")])
    if provider and Capability.THINKING_MODE in provider.capabilities:
        keyboard.append([InlineKeyboardButton(f"\U0001f4ad Thinking: {thinking_status}", callback_data="TOGGLE_THINKING_MODE")])
    if provider and Capability.CODE_EXECUTION in provider.capabilities:
        keyboard.append([InlineKeyboardButton(f"\U0001f5a5\ufe0f Code Execution: {code_exec_status}", callback_data="TOGGLE_CODE_EXEC")])

    keyboard.extend([
        [InlineKeyboardButton(f"\U0001f30d Language: {user_lang}", callback_data="Language_Menu")],
        [InlineKeyboardButton(_("\U0001f514 Daily Briefing"), callback_data="Briefing_Menu")],
        [InlineKeyboardButton(_("\U0001f517 URL Monitor"), callback_data="URL_Monitor_Menu")],
        [InlineKeyboardButton(_("\U0001f4c1 Storage Management"), callback_data="Storage_Menu")],
        [InlineKeyboardButton(_("\U0001f511 Update API Key"), callback_data="UPDATE_API_KEY")],
        [InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")],
    ])

    try:
        await query.edit_message_text(_("Settings Menu"), reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error editing message: {e}")
            await context.bot.send_message(chat_id=user_id, text=_("Settings Menu"), reply_markup=InlineKeyboardMarkup(keyboard))

    return SETTINGS_MENU


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------

@restricted
async def update_api_key_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    user_id = update.effective_user.id
    logger.info(f"Initiating API key update for user {user_id}")

    provider_name = get_active_provider_name(context)

    # Provider-specific API key instructions
    provider_instructions = {
        "gemini": (
            "\U0001f511 Update API Key (Gemini)\n\n"
            "How to get your API key:\n"
            "1. Go to aistudio.google.com\n"
            "2. Sign in with your Google account\n"
            "3. Click \"Get API Key\" in the left sidebar\n"
            "4. Click \"Create API Key\" and copy it\n\n"
            "Video tutorial: https://youtu.be/RVGbLSVFtIk?t=22\n\n"
            "Please paste your new API Key below:"
        ),
        "openai": (
            "\U0001f511 Update API Key (OpenAI)\n\n"
            "How to get your API key:\n"
            "1. Go to platform.openai.com/api-keys\n"
            "2. Sign in with your OpenAI account\n"
            "3. Click \"Create new secret key\"\n"
            "4. Copy the key (it starts with sk-)\n\n"
            "Please paste your new API Key below:"
        ),
        "anthropic": (
            "\U0001f511 Update API Key (Anthropic)\n\n"
            "How to get your API key:\n"
            "1. Go to console.anthropic.com/settings/keys\n"
            "2. Sign in with your Anthropic account\n"
            "3. Click \"Create Key\"\n"
            "4. Copy the key (it starts with sk-ant-)\n\n"
            "Please paste your new API Key below:"
        ),
    }

    text = provider_instructions.get(provider_name, (
        f"\U0001f511 Update API Key ({provider_name.title()})\n\n"
        f"Please paste your API key for {provider_name.title()} below:"
    ))

    await query.edit_message_text(_(text))
    return API_KEY_INPUT


# ---------------------------------------------------------------------------
# Provider Menu
# ---------------------------------------------------------------------------

@restricted
async def open_provider_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show a menu of all available providers."""
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    user_id = update.effective_user.id
    pool = _get_pool(context)

    current_provider = get_active_provider_name(context)
    registry = ProviderRegistry()
    registered_names = registry.list_providers()

    # Also fetch user's custom providers
    custom_providers = await get_user_custom_providers(pool, user_id)
    custom_names = {cp["name"] for cp in custom_providers}

    # Merge: registered providers + any custom providers not already registered
    all_provider_names = list(registered_names)
    for cp in custom_providers:
        if cp["name"] not in all_provider_names:
            all_provider_names.append(cp["name"])

    keyboard = []
    for name in all_provider_names:
        prefix = "\u2705 " if name == current_provider else ""
        # Use display name from registry if available, otherwise from custom providers
        provider_obj = registry.get(name)
        if provider_obj:
            display = name.title()
        elif name in custom_names:
            cp = next(c for c in custom_providers if c["name"] == name)
            display = cp.get("display_name") or name.title()
        else:
            display = name.title()
        keyboard.append([InlineKeyboardButton(f"{prefix}{display}", callback_data=f"SET_PROVIDER_{name}")])

    keyboard.append([InlineKeyboardButton(_("\U0001f519 Back to Settings"), callback_data="Settings_Menu")])

    await query.edit_message_text(
        _("\U0001f504 Select your AI provider:"),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SETTINGS_MENU


@restricted
async def set_provider_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Switch the user's active provider."""
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    provider_name = query.data.replace("SET_PROVIDER_", "")
    user_id = update.effective_user.id
    logger.info(f"Switching provider to {provider_name} for user {user_id}")

    pool = _get_pool(context)
    await set_active_provider(pool, user_id, provider_name)
    context.user_data["active_provider"] = provider_name

    # Clear chat session so the new provider is used
    context.user_data["chat_session"] = None

    await open_settings_menu(update, context)
    return SETTINGS_MENU


# ---------------------------------------------------------------------------
# Models Menu
# ---------------------------------------------------------------------------

@restricted
async def open_models_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show a menu of all available models from the active provider."""
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return MODELS_MENU

    user_id = update.effective_user.id
    logger.info(f"Opening models menu for user {user_id}")

    provider_name = get_active_provider_name(context)
    provider = ProviderRegistry().get(provider_name)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN

    try:
        models = await provider.list_models(api_key)
    except Exception:
        models = []

    if not models:
        await query.edit_message_text(
            _("Failed to fetch models or no models available."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")]]),
        )
        return SETTINGS_MENU

    current_model = context.user_data.get("model_name") or GEMINI_MODEL

    # Provider-specific filtering
    if provider_name == "gemini":
        # Filter out non-chat models
        skip_patterns = [
            'embedding', 'aqa', 'imagen', 'veo', 'text-',
            'tts', 'image', 'native-audio', 'robotics',
            'computer-use', 'deep-research', 'customtools',
            'nano-banana', 'gemma', 'tuning',
        ]

        def _parse_model_version(name_lower):
            """Extract (major, minor, tier) for sorting. Higher = newer."""
            import re as _re
            # Match gemini-X.Y or gemini-X patterns
            vm = _re.search(r'gemini-(\d+)(?:\.(\d+))?', name_lower)
            if not vm:
                # "latest" aliases without version get sorted high
                if 'latest' in name_lower:
                    return (99, 0, 0)
                return (0, 0, 0)
            major = int(vm.group(1))
            minor = int(vm.group(2)) if vm.group(2) else 0
            # Tier: pro > flash > flash-lite/lite, stable > preview > versioned
            if 'pro' in name_lower:
                tier = 3
            elif 'lite' in name_lower:
                tier = 1
            elif 'flash' in name_lower:
                tier = 2
            else:
                tier = 2  # generic/latest aliases
            # Penalize point-release suffixes like -001
            if _re.search(r'-\d{3}$', name_lower):
                tier -= 0.5
            # Penalize preview
            if 'preview' in name_lower:
                tier -= 0.1
            return (major, minor, tier)

        chat_models = []
        for m in models:
            name_lower = m.id.lower()
            if any(s in name_lower for s in skip_patterns):
                continue
            if not name_lower.startswith('models/gemini'):
                continue
            chat_models.append(m)

        # Sort all chat models: latest version first, then pro > flash > lite
        chat_models.sort(key=lambda m: _parse_model_version(m.id.lower()), reverse=True)
    else:
        # For other providers, show all models
        chat_models = models

    # Split into featured (top 8) and others
    featured = chat_models[:8]
    others = chat_models[8:]

    show_all = context.user_data.get("show_all_models", False)

    # Brief description from API or fallback
    desc_map = {
        'pro': '\U0001f3c6 Pro',
        'flash-lite': '\U0001fab6 Lite',
        'flash': '\u26a1 Flash',
        'lite': '\U0001fab6 Lite',
    }

    text = f"\U0001f916 *Models ({provider_name.title()})*\n\n"
    keyboard = []

    display_models = featured if not show_all else featured + others

    for m in display_models:
        is_current = m.id.endswith(current_model) or m.id == current_model
        prefix = "\u2705 " if is_current else ""

        # Generate short description
        name_lower = m.id.lower()
        desc = ""
        for key, label in desc_map.items():
            if key in name_lower:
                desc = f" \u2014 {label}"
                break

        button_text = f"{prefix}{m.display_name}{desc}"
        if len(button_text) > 60:
            button_text = f"{prefix}{m.display_name}"

        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"SET_MODEL_{m.id}")])

    if others and not show_all:
        keyboard.append([InlineKeyboardButton(f"\U0001f4cb Show all ({len(others)} more)", callback_data="Show_All_Models")])

    keyboard.append([InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error editing message: {e}")

    return MODELS_MENU


@restricted
async def set_model_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return MODELS_MENU

    model_name = query.data.replace("SET_MODEL_", "")
    user_id = update.effective_user.id
    logger.info(f"Setting model to {model_name} for user {user_id}")

    pool = _get_pool(context)
    await update_user_settings(pool, user_id, model_name=model_name)
    context.user_data["model_name"] = model_name

    await open_models_menu(update, context)
    return MODELS_MENU


@restricted
async def show_all_models_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle showing all models."""
    query = update.callback_query
    await query.answer()
    context.user_data["show_all_models"] = True
    return await open_models_menu(update, context)


# ---------------------------------------------------------------------------
# Storage Menu
# ---------------------------------------------------------------------------

@restricted
async def open_storage_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show files currently stored in the provider's temporary storage."""
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return STORAGE_MENU

    user_id = update.effective_user.id
    logger.info(f"Opening storage menu for user {user_id}")

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN

    provider_name = get_active_provider_name(context)
    provider = ProviderRegistry().get(provider_name)

    if provider_name == "gemini" and hasattr(provider, 'list_uploaded_files'):
        files = await provider.list_uploaded_files(api_key=api_key)
    else:
        files = []

    if files is None or (provider_name != "gemini"):
        if provider_name != "gemini":
            content = _("File storage is not available for this provider.")
        else:
            content = _("No files currently stored in Gemini's temporary storage.")
    elif not files:
        content = _("No files currently stored in Gemini's temporary storage.")
    else:
        content = _("Active files in Google's temporary storage (expire after 48h):\n\n")
        for f in files:
            size_mb = f['size_bytes'] / (1024 * 1024)
            content += f"\u2022 `{f['display_name']}` ({f['mime_type']}, {size_mb:.2f} MB)\n"

    keyboard = [[InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")]]

    try:
        await query.edit_message_text(content, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error editing message: {e}")

    return STORAGE_MENU


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------

@restricted
async def open_persona_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    pool = _get_pool(context)
    user = await get_user(pool, user_id)
    current_persona = user.get('system_instruction') or "Default (Female Assistant)"

    text = f"Your Current Persona:\n\n{current_persona}\n\nEnter a new system instruction/persona if you want to change it."
    keyboard = [[InlineKeyboardButton(_("\U0001f519 Back to Settings"), callback_data="Settings_Menu")]]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return PERSONA_INPUT


@restricted
async def handle_persona_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_text = update.message.text
    user_id = update.effective_user.id
    pool = _get_pool(context)
    await update_user_settings(pool, user_id, system_instruction=persona_text)
    context.user_data["system_instruction"] = persona_text

    await update.message.reply_text(_("Persona updated successfully!"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to Settings"), callback_data="Settings_Menu")]]))
    return SETTINGS_MENU


# ---------------------------------------------------------------------------
# Feature Toggles
# ---------------------------------------------------------------------------

@restricted
async def toggle_web_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    current = context.user_data.get("web_search", False)
    new_status = not current

    user_id = update.effective_user.id
    logger.info(f"Toggling web search to {new_status} for user {user_id}")

    pool = _get_pool(context)
    await update_user_settings(pool, user_id, grounding=int(new_status))
    context.user_data["web_search"] = new_status

    await open_settings_menu(update, context)
    return SETTINGS_MENU


@restricted
async def toggle_thinking_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cycle through thinking modes: off -> light -> medium -> deep -> off."""
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    user_id = update.effective_user.id
    pool = _get_pool(context)
    user = await get_user(pool, user_id)

    current = user.get('thinking_mode', 'off') if user else 'off'
    cycle = ["off", "light", "medium", "deep"]
    next_idx = (cycle.index(current) + 1) % len(cycle) if current in cycle else 0
    new_mode = cycle[next_idx]

    await update_user_settings(pool, user_id, thinking_mode=new_mode)
    # Clear active chat so new settings take effect
    context.user_data["chat_session"] = None

    await open_settings_menu(update, context)
    return SETTINGS_MENU


@restricted
async def toggle_code_execution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle code execution on/off."""
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    user_id = update.effective_user.id
    pool = _get_pool(context)
    user = await get_user(pool, user_id)

    current = user.get('code_execution', False) if user else False
    new_status = not current

    await update_user_settings(pool, user_id, code_execution=new_status)
    # Clear active chat so new settings take effect
    context.user_data["chat_session"] = None

    await open_settings_menu(update, context)
    return SETTINGS_MENU


# ---------------------------------------------------------------------------
# Shortcuts
# ---------------------------------------------------------------------------

@restricted
async def open_shortcuts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show user shortcuts."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    shortcuts = await get_user_shortcuts(pool, user_id)

    text = "\u26a1 *Quick Shortcuts*\n\n"
    text += "Create custom commands that auto-send messages to the AI.\n\n"

    keyboard = [[InlineKeyboardButton(_("\u2795 Add Shortcut"), callback_data="Add_Shortcut")]]

    if shortcuts:
        for s in shortcuts[:10]:
            text += f"\u2022 /{s['command']} \u2192 {s['response_text'][:40]}...\n"
            keyboard.append([InlineKeyboardButton(f"\u274c Delete /{s['command']}", callback_data=f"SHORTCUT_DELETE#{s['id']}")])
    else:
        text += "No shortcuts yet. Add one to get started!"

    keyboard.append([InlineKeyboardButton(_("\U0001f519 Back to Settings"), callback_data="Settings_Menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return SHORTCUTS_MENU


@restricted
async def start_add_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt to add a new shortcut."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        _("\u26a1 *Add Shortcut*\n\n"
          "Enter in format: command | prompt\n\n"
          "Example: summarize | Summarize the following text in 3 bullet points\n\n"
          "Then in conversation, type /summarize to auto-send that prompt."),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("\U0001f519 Back"), callback_data="Shortcuts_Menu")]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return SHORTCUTS_INPUT


@restricted
async def handle_shortcut_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save a new shortcut."""
    text = update.message.text
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to Shortcuts"), callback_data="Shortcuts_Menu")]])

    try:
        command, response_text = [s.strip() for s in text.split('|', 1)]
        command = command.lower().replace('/', '').replace(' ', '_')[:20]
        if not command or not response_text:
            raise ValueError("Empty command or response")

        pool = _get_pool(context)
        user_id = update.effective_user.id
        await add_shortcut(pool, user_id, command, response_text)

        await update.message.reply_text(f"\u2705 Shortcut /{command} saved!", reply_markup=back_btn)
    except (ValueError, IndexError):
        await update.message.reply_text(_("Invalid format. Use: command | prompt text"), reply_markup=back_btn)

    return SHORTCUTS_MENU


@restricted
async def delete_shortcut_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Delete a shortcut."""
    query = update.callback_query
    await query.answer()
    shortcut_id = int(query.data.split("#")[1])

    pool = _get_pool(context)
    await delete_shortcut(pool, update.effective_user.id, shortcut_id)
    await open_shortcuts_menu(update, context)
    return SHORTCUTS_MENU


# ---------------------------------------------------------------------------
# Pinned Context
# ---------------------------------------------------------------------------

@restricted
async def open_pinned_context_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show pinned context settings."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    user = await get_user(pool, user_id)
    current_context = user.get('pinned_context') if user else None

    text = "\U0001f4cc *Pinned Context*\n\n"
    text += "This context is included in ALL your conversations automatically.\n"
    text += "Use it for information the AI should always know about you.\n\n"
    if current_context:
        text += f"Current:\n_{current_context}_\n\n"
        text += "Send new text to update, or tap Clear to remove."
    else:
        text += "No pinned context set. Send text to add one.\n\n"
        text += "Examples:\n\u2022 \"I'm a Python developer working on web apps\"\n\u2022 \"Always respond in formal English\"\n\u2022 \"My timezone is EST\""

    keyboard = []
    if current_context:
        keyboard.append([InlineKeyboardButton(_("\U0001f5d1 Clear Context"), callback_data="Clear_Pinned_Context")])
    keyboard.append([InlineKeyboardButton(_("\U0001f519 Back to Settings"), callback_data="Settings_Menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return PINNED_CONTEXT_INPUT


@restricted
async def handle_pinned_context_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save pinned context."""
    pinned_text = update.message.text.strip()[:500]
    user_id = update.effective_user.id
    pool = _get_pool(context)
    await update_user_settings(pool, user_id, pinned_context=pinned_text)

    # Reset current chat so it picks up new context
    context.user_data["chat_session"] = None

    keyboard = [[InlineKeyboardButton(_("\U0001f519 Back to Settings"), callback_data="Settings_Menu")]]
    await update.message.reply_text(_("\u2705 Pinned context updated! It will apply to your next conversation."), reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU


@restricted
async def clear_pinned_context_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Clear pinned context."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    await update_user_settings(pool, user_id, pinned_context="")
    context.user_data["chat_session"] = None

    keyboard = [[InlineKeyboardButton(_("\U0001f519 Back to Settings"), callback_data="Settings_Menu")]]
    await query.edit_message_text(_("\U0001f4cc Pinned context cleared."), reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

@restricted
async def language_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show language selection menu."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    user = await get_user(pool, user_id)
    current_lang = user.get('language', 'auto') if user else 'auto'

    languages = [
        ("auto", "\U0001f310 Auto-detect"),
        ("en", "\U0001f1ec\U0001f1e7 English"),
        ("fa", "\U0001f1ee\U0001f1f7 \u0641\u0627\u0631\u0633\u06cc"),
        ("es", "\U0001f1ea\U0001f1f8 Espa\u00f1ol"),
        ("fr", "\U0001f1eb\U0001f1f7 Fran\u00e7ais"),
        ("de", "\U0001f1e9\U0001f1ea Deutsch"),
        ("zh", "\U0001f1e8\U0001f1f3 \u4e2d\u6587"),
        ("ja", "\U0001f1ef\U0001f1f5 \u65e5\u672c\u8a9e"),
        ("ko", "\U0001f1f0\U0001f1f7 \ud55c\uad6d\uc5b4"),
        ("ar", "\U0001f1f8\U0001f1e6 \u0627\u0644\u0639\u0631\u0628\u064a\u0629"),
        ("ru", "\U0001f1f7\U0001f1fa \u0420\u0443\u0441\u0441\u043a\u0438\u0439"),
        ("pt", "\U0001f1e7\U0001f1f7 Portugu\u00eas"),
        ("tr", "\U0001f1f9\U0001f1f7 T\u00fcrk\u00e7e"),
    ]

    keyboard = []
    for code, name in languages:
        prefix = "\u2705 " if code == current_lang else ""
        keyboard.append([InlineKeyboardButton(f"{prefix}{name}", callback_data=f"SET_LANG_{code}")])
    keyboard.append([InlineKeyboardButton(_("\U0001f519 Back"), callback_data="Settings_Menu")])

    await query.edit_message_text(_("\U0001f30d Select your preferred language:"), reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU


@restricted
async def set_language_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set user language preference."""
    query = update.callback_query
    await query.answer()

    lang = query.data.replace("SET_LANG_", "")
    user_id = update.effective_user.id
    pool = _get_pool(context)
    await update_user_settings(pool, user_id, language=lang)

    # Reset chat to pick up new language
    context.user_data["chat_session"] = None

    await open_settings_menu(update, context)
    return SETTINGS_MENU
