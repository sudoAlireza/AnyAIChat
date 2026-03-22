"""Conversation history, search, tags, export, share, usage, and branching handlers."""

import io
import re
import json
import math
import logging
import uuid

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest
from datetime import datetime

from handlers.common import restricted, _, _get_pool, get_active_provider_name
from handlers.states import (
    CHOOSING, CONVERSATION_HISTORY, SEARCH_INPUT, TAGS_INPUT,
)
from config import ITEMS_PER_PAGE
from database.database import (
    get_user_conversation_count, select_conversations_by_user,
    select_conversation_by_id, delete_conversation_by_id,
    search_conversations, add_conversation_tag, get_user_tags,
    get_conversations_by_tag, get_conversation_tags, remove_conversation_tag,
    get_user_stats, get_user_token_stats,
    get_user_token_stats_by_provider, get_user_total_cost,
    update_conversation_resume, create_conversation_branch,
)
from helpers.helpers import conversations_page_content, strip_markdown, split_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversation detail / selection
# ---------------------------------------------------------------------------

@restricted
async def get_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Retrieve a specific conversation via inline button or typed command."""
    try:
        # Support both inline button (callback_query) and typed /convXXX command
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            conv_id = query.data.split("#")[1]
        else:
            conv_id = update.message.text.strip().replace("/", "")

        user_id = update.effective_user.id
        pool = _get_pool(context)

        conversation = await select_conversation_by_id(pool, (user_id, conv_id))
        if not conversation:
            msg = _("Conversation not found.")
            if update.callback_query:
                await update.callback_query.edit_message_text(msg)
            else:
                await update.message.reply_text(msg)
            return CONVERSATION_HISTORY

        context.user_data["conversation_id"] = conv_id

        # Build a detail card
        title = conversation.get('title', 'Untitled')
        msg_count = 0
        last_exchange = ""
        history_raw = conversation.get('history')
        if history_raw:
            try:
                history = json.loads(history_raw)
                msg_count = len(history)
                # Show last 2 exchanges as preview
                recent = []
                # Handle both old and new history formats
                for entry in history[-4:]:
                    role = entry.get('role', '')
                    # New format
                    if 'content' in entry:
                        preview_text = entry['content'][:100]
                        if len(entry['content']) > 100:
                            preview_text += "..."
                    # Old Gemini format
                    elif 'parts' in entry:
                        parts = entry.get('parts', [])
                        text_parts = [p.get('text', '') for p in parts if p.get('text')]
                        if text_parts:
                            preview_text = text_parts[0][:100]
                            if len(text_parts[0]) > 100:
                                preview_text += "..."
                        else:
                            continue
                    else:
                        continue
                    emoji = "\U0001f464" if role == "user" else "\U0001f916"
                    recent.append(f"  {emoji} {preview_text}")
                if recent:
                    last_exchange = "\n".join(recent)
            except (json.JSONDecodeError, TypeError):
                pass

        detail = f"\U0001f4c2 *{title}*\n"
        detail += "\u2501" * 24 + "\n\n"
        detail += f"\U0001f4ca Messages: {msg_count}\n"
        if last_exchange:
            detail += f"\n*Last messages:*\n{last_exchange}\n"
        detail += "\n" + "\u2501" * 24

        # Get tags for this conversation
        tags = await get_conversation_tags(pool, user_id, conv_id)
        if tags:
            detail += f"\n\U0001f3f7 Tags: {', '.join(tags)}"

        keyboard = [
            [InlineKeyboardButton(_("\u25b6\ufe0f Continue Conversation"), callback_data="New_Conversation")],
            [
                InlineKeyboardButton(_("\U0001f3f7 Tag"), callback_data="Tag_Conversation"),
                InlineKeyboardButton(_("\U0001f4e4 Export"), callback_data="Export_Conversation"),
                InlineKeyboardButton(_("\U0001f517 Share"), callback_data="Share_Conversation"),
            ],
            [
                InlineKeyboardButton(_("\U0001f500 Branch"), callback_data="Branch_Conversation"),
                InlineKeyboardButton(_("\U0001f4cd Resume"), callback_data="Set_Resume_Point"),
            ],
            [
                InlineKeyboardButton(_("\U0001f5d1 Delete"), callback_data="Delete_Conversation"),
                InlineKeyboardButton(_("\U0001f4cb Back to List"), callback_data="PAGE#1"),
            ],
            [InlineKeyboardButton(_("\U0001f519 Back to menu"), callback_data="Start_Again")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text=detail, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest:
                await update.callback_query.edit_message_text(
                    text=strip_markdown(detail), reply_markup=reply_markup
                )
        else:
            try:
                await update.message.reply_text(
                    text=detail, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest:
                await update.message.reply_text(
                    text=strip_markdown(detail), reply_markup=reply_markup
                )
    except Exception as e:
        logger.error(f"Error in get_conversation_handler: {e}", exc_info=True)
    return CONVERSATION_HISTORY


@restricted
async def delete_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Delete current conversation."""
    query = update.callback_query
    await query.answer()

    conv_id = context.user_data.get("conversation_id")
    if conv_id:
        pool = _get_pool(context)
        await delete_conversation_by_id(pool, (update.effective_user.id, conv_id))
        await query.edit_message_text(_("Deleted. Back to menu."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Menu"), callback_data="Start_Again")]]))
    return CHOOSING


@restricted
async def get_conversation_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """List conversations with inline selection buttons."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    page_number = int(query.data.split("#")[1])
    pool = _get_pool(context)

    count = await get_user_conversation_count(pool, user_id)
    total_pages = math.ceil(count / ITEMS_PER_PAGE) if count > 0 else 1

    conversations = await select_conversations_by_user(pool, (user_id, (page_number - 1) * ITEMS_PER_PAGE))

    if not conversations:
        keyboard = [
            [InlineKeyboardButton(_("\u2795 Start New Conversation"), callback_data="New_Conversation")],
            [InlineKeyboardButton(_("\U0001f519 Back to menu"), callback_data="Start_Again")],
        ]
        await query.edit_message_text(
            _("\U0001f4ac No conversations yet.\n\nStart a new conversation and save it to see it here."),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CONVERSATION_HISTORY

    content = conversations_page_content(conversations)

    # Build conversation selection buttons
    keyboard = []
    for conv in conversations:
        title = conv.get('title', 'Untitled')
        if len(title) > 35:
            title = title[:32] + "..."
        msg_count = conv.get('message_count', 0)
        keyboard.append([InlineKeyboardButton(
            f"\U0001f4ac {title} ({msg_count} msgs)",
            callback_data=f"CONV_SELECT#{conv['conversation_id']}"
        )])

    # Pagination row
    nav_buttons = []
    if page_number > 1:
        nav_buttons.append(InlineKeyboardButton("\u25c0\ufe0f", callback_data=f"PAGE#{page_number - 1}"))
    nav_buttons.append(InlineKeyboardButton(f"\U0001f4c4 {page_number}/{total_pages}", callback_data="noop"))
    if page_number < total_pages:
        nav_buttons.append(InlineKeyboardButton("\u25b6\ufe0f", callback_data=f"PAGE#{page_number + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(_("\U0001f519 Back to menu"), callback_data="Start_Again")])

    try:
        await query.edit_message_text(
            text=content,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest:
        await query.edit_message_text(
            text=strip_markdown(content),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return CONVERSATION_HISTORY


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@restricted
async def search_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open search input."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(_("\U0001f3f7 Browse by Tag"), callback_data="Browse_Tags")],
        [InlineKeyboardButton(_("\U0001f519 Back to menu"), callback_data="Start_Again")],
    ]
    await query.edit_message_text(
        _("\U0001f50d *Search Conversations*\n\nType a keyword to search across all your conversations, or browse by tag."),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return SEARCH_INPUT


@restricted
async def handle_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process search query."""
    search_query = update.message.text.strip()
    if not search_query or len(search_query) < 2:
        await update.message.reply_text(_("Please enter at least 2 characters to search."))
        return SEARCH_INPUT

    pool = _get_pool(context)
    user_id = update.effective_user.id
    results = await search_conversations(pool, user_id, search_query)

    if not results:
        keyboard = [[InlineKeyboardButton(_("\U0001f519 Back to menu"), callback_data="Start_Again")]]
        await update.message.reply_text(
            _("No conversations found matching your search."),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CHOOSING

    text = f"\U0001f50d *Search Results for \"{search_query}\"*\n\n"
    keyboard = []
    for r in results[:10]:
        title = r['title'][:35] + "..." if len(r['title']) > 35 else r['title']
        keyboard.append([InlineKeyboardButton(
            f"\U0001f4ac {title}",
            callback_data=f"CONV_SELECT#{r['conversation_id']}"
        )])

    keyboard.append([InlineKeyboardButton(_("\U0001f519 Back to menu"), callback_data="Start_Again")])

    try:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await update.message.reply_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return CONVERSATION_HISTORY


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@restricted
async def browse_tags_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Browse conversations by tag."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    tags = await get_user_tags(pool, user_id)

    if not tags:
        keyboard = [[InlineKeyboardButton(_("\U0001f519 Back to menu"), callback_data="Start_Again")]]
        await query.edit_message_text(_("No tags found. Tag conversations from the History detail view."), reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSING

    keyboard = []
    for tag in tags:
        keyboard.append([InlineKeyboardButton(f"\U0001f3f7 {tag}", callback_data=f"TAG_BROWSE#{tag}")])
    keyboard.append([InlineKeyboardButton(_("\U0001f519 Back to menu"), callback_data="Start_Again")])

    await query.edit_message_text(_("\U0001f3f7 *Your Tags*\n\nSelect a tag to see conversations:"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return SEARCH_INPUT


@restricted
async def tag_browse_results_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show conversations with a specific tag."""
    query = update.callback_query
    await query.answer()
    tag = query.data.split("#")[1]

    pool = _get_pool(context)
    user_id = update.effective_user.id
    results = await get_conversations_by_tag(pool, user_id, tag)

    if not results:
        keyboard = [[InlineKeyboardButton(_("\U0001f519 Back"), callback_data="Browse_Tags")]]
        await query.edit_message_text(_(f"No conversations with tag '{tag}'."), reply_markup=InlineKeyboardMarkup(keyboard))
        return SEARCH_INPUT

    text = f"\U0001f3f7 *Conversations tagged \"{tag}\"*\n\n"
    keyboard = []
    for r in results[:10]:
        title = r['title'][:35] + "..." if len(r['title']) > 35 else r['title']
        keyboard.append([InlineKeyboardButton(f"\U0001f4ac {title}", callback_data=f"CONV_SELECT#{r['conversation_id']}")])
    keyboard.append([InlineKeyboardButton(_("\U0001f519 Back to Tags"), callback_data="Browse_Tags")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return CONVERSATION_HISTORY


@restricted
async def tag_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt user to enter a tag for the current conversation."""
    query = update.callback_query
    await query.answer()

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    existing_tags = await get_conversation_tags(pool, user_id, conv_id)

    text = "\U0001f3f7 *Tag this conversation*\n\n"
    if existing_tags:
        text += f"Current tags: {', '.join(existing_tags)}\n\n"
    text += "Type a tag name to add, or tap an existing tag to remove it."

    keyboard = []
    for tag in existing_tags:
        keyboard.append([InlineKeyboardButton(f"\u274c Remove: {tag}", callback_data=f"TAG_REMOVE#{tag}")])
    keyboard.append([InlineKeyboardButton(_("\U0001f519 Back"), callback_data=f"CONV_SELECT#{conv_id}")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return TAGS_INPUT


@restricted
async def handle_tag_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save a new tag for the current conversation."""
    tag = update.message.text.strip().lower()[:30]
    if not tag:
        await update.message.reply_text(_("Please enter a valid tag name."))
        return TAGS_INPUT

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CHOOSING

    pool = _get_pool(context)
    user_id = update.effective_user.id
    await add_conversation_tag(pool, user_id, conv_id, tag)

    keyboard = [[InlineKeyboardButton(_("\U0001f519 Back to Conversation"), callback_data=f"CONV_SELECT#{conv_id}")]]
    await update.message.reply_text(f"\u2705 Tag '{tag}' added!", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONVERSATION_HISTORY


@restricted
async def remove_tag_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Remove a tag from the current conversation."""
    query = update.callback_query
    await query.answer()
    tag = query.data.split("#")[1]

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    await remove_conversation_tag(pool, user_id, conv_id, tag)

    # Refresh the tag view
    return await tag_conversation_handler(update, context)


# ---------------------------------------------------------------------------
# Export & Share
# ---------------------------------------------------------------------------

@restricted
async def export_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Export conversation as a text/markdown file."""
    query = update.callback_query
    await query.answer()

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    conv = await select_conversation_by_id(pool, (user_id, conv_id))
    if not conv:
        await query.edit_message_text(_("Conversation not found."))
        return CONVERSATION_HISTORY

    title = conv.get('title', 'Untitled')
    history = json.loads(conv.get('history', '[]'))

    # Format as markdown
    text = f"# {title}\n\n"
    text += f"Exported on: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n"
    for entry in history:
        role = entry.get('role', 'unknown')
        prefix = "**User:**" if role == "user" else "**Assistant:**"

        # New format
        if 'content' in entry:
            text += f"{prefix}\n{entry['content']}\n\n---\n\n"
        # Old Gemini format
        elif 'parts' in entry:
            parts = entry.get('parts', [])
            for p in parts:
                if p.get('text'):
                    text += f"{prefix}\n{p['text']}\n\n---\n\n"

    file_buf = io.BytesIO(text.encode('utf-8'))
    safe_title = re.sub(r'[^\w\s-]', '', title)[:30].strip()
    file_name = f"{safe_title or 'conversation'}.md"
    file_buf.name = file_name

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=file_buf,
        filename=file_name,
        caption=f"\U0001f4e4 Exported: {title}"
    )

    keyboard = [[InlineKeyboardButton(_("\U0001f519 Back"), callback_data=f"CONV_SELECT#{conv_id}")]]
    await query.edit_message_text(_("Conversation exported!"), reply_markup=InlineKeyboardMarkup(keyboard))
    return CONVERSATION_HISTORY


@restricted
async def share_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Share conversation as formatted text."""
    query = update.callback_query
    await query.answer()

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    conv = await select_conversation_by_id(pool, (user_id, conv_id))
    if not conv:
        return CONVERSATION_HISTORY

    title = conv.get('title', 'Untitled')
    history = json.loads(conv.get('history', '[]'))

    # Format for sharing (compact)
    text = f"\U0001f4ac *{title}*\n\n"
    msg_count = 0
    for entry in history:
        role = entry.get('role', '')
        emoji = "\U0001f464" if role == "user" else "\U0001f916"

        # New format
        if 'content' in entry:
            content_text = entry['content'][:200]
            if len(entry['content']) > 200:
                content_text += "..."
            text += f"{emoji} {content_text}\n\n"
            msg_count += 1
        # Old Gemini format
        elif 'parts' in entry:
            parts = entry.get('parts', [])
            for p in parts:
                if p.get('text'):
                    content_text = p['text'][:200]
                    if len(p['text']) > 200:
                        content_text += "..."
                    text += f"{emoji} {content_text}\n\n"
                    msg_count += 1
                    if msg_count >= 10:
                        break

        if msg_count >= 10:
            remaining = len(history) - 10
            if remaining > 0:
                text += f"_... and {remaining} more messages_\n"
            break

    text += "\n_Shared from AI Chat Bot_"

    parts_list = split_message(text)
    for part in parts_list:
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=part, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=strip_markdown(part))
    return CONVERSATION_HISTORY


# ---------------------------------------------------------------------------
# Usage Dashboard
# ---------------------------------------------------------------------------

@restricted
async def usage_dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show usage statistics."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    stats = await get_user_stats(pool, user_id)

    text = "\U0001f4ca *Usage Dashboard*\n"
    text += "\u2501" * 24 + "\n\n"
    text += f"\U0001f4ac Conversations: {stats['conversations']}\n"
    text += f"\U0001f4cb Active Tasks: {stats['active_tasks']} / {stats['total_tasks']} total\n"
    text += f"\u23f0 Reminders: {stats['completed_reminders']} \u2705 / {stats['total_reminders']} total\n"
    text += f"\U0001f4da Knowledge Docs: {stats['knowledge_docs']}\n"

    if stats.get('member_since'):
        text += f"\n\U0001f4c5 Using since: {str(stats['member_since'])[:10]}"

    # Per-user token usage from database
    token_stats = await get_user_token_stats(pool, user_id)
    if token_stats and token_stats.get("total_tokens"):
        text += "\n\n\U0001f522 *Token Usage (All Time)*\n"
        text += f"Total: {token_stats['total_tokens']:,} tokens ({token_stats['total_requests']} requests)\n"
        text += f"  Input: {token_stats['prompt_tokens']:,}\n"
        text += f"  Output: {token_stats['completion_tokens']:,}\n"

        # Cached tokens with savings estimate
        if token_stats.get('cached_tokens'):
            cached = token_stats['cached_tokens']
            # Cached tokens cost ~75% less than regular input
            estimated_savings = cached * 0.75
            text += f"  \U0001f4be Cached: {cached:,} (saved ~{int(estimated_savings):,} equiv. tokens)\n"

        # Thinking tokens
        if token_stats.get('thinking_tokens'):
            text += f"  \U0001f4ad Thinking: {token_stats['thinking_tokens']:,}\n"

        text += "\n\U0001f4c5 *Today*\n"
        text += f"  Tokens: {token_stats.get('today_tokens', 0):,}\n"
        if token_stats.get('today_cached'):
            text += f"  Cached: {token_stats['today_cached']:,}\n"

        text += "\n\U0001f4c6 *Last 7 Days*\n"
        text += f"  Tokens: {token_stats.get('week_tokens', 0):,}\n"
        if token_stats.get('week_cached'):
            text += f"  Cached: {token_stats['week_cached']:,}\n"

    # Active provider indicator
    provider_name = get_active_provider_name(context)
    text += f"\n\U0001f500 *Active Provider:* {provider_name.title()}\n"

    # Estimated cost
    total_cost = await get_user_total_cost(pool, user_id)
    if total_cost:
        text += "\n\U0001f4b0 *Estimated Cost*\n"
        text += f"  Total: ${total_cost:.4f}\n"

    # Per-provider breakdown
    provider_stats = await get_user_token_stats_by_provider(pool, user_id)
    if provider_stats:
        text += "\n\U0001f4ca *Per-Provider Breakdown*\n"
        for ps in provider_stats:
            name = ps["provider"].title()
            tokens = ps["total_tokens"]
            reqs = ps["requests"]
            cost = ps["estimated_cost"]
            cost_str = f" ~${cost:.3f}" if cost else ""
            text += f"  {name}: {tokens:,} tokens ({reqs} requests){cost_str}\n"

    keyboard = [[InlineKeyboardButton(_("\U0001f519 Back to menu"), callback_data="Start_Again")]]

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING


# ---------------------------------------------------------------------------
# Branch & Resume
# ---------------------------------------------------------------------------

@restricted
async def branch_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Create a branch (copy) of the current conversation."""
    query = update.callback_query
    await query.answer()

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        await query.edit_message_text(_("No conversation selected."))
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    conv = await select_conversation_by_id(pool, (user_id, conv_id))
    if not conv:
        return CONVERSATION_HISTORY

    new_conv_id = f"conv{uuid.uuid4().hex[:6]}"
    title = conv.get('title', 'Untitled')
    history = conv.get('history', '[]')

    await create_conversation_branch(pool, user_id, conv_id, new_conv_id, title, history)

    context.user_data["conversation_id"] = new_conv_id
    context.user_data["chat_session"] = None

    keyboard = [
        [InlineKeyboardButton(_("\u25b6\ufe0f Continue Branch"), callback_data="New_Conversation")],
        [InlineKeyboardButton(_("\U0001f519 Back to menu"), callback_data="Start_Again")],
    ]
    await query.edit_message_text(
        f"\U0001f500 Branch created: *[Branch] {title}*\n\nYou can now continue this conversation independently.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return CONVERSATION_HISTORY


@restricted
async def set_resume_point_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mark current position as resume point."""
    query = update.callback_query
    await query.answer("\U0001f4cd Resume point set!", show_alert=False)

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    conv = await select_conversation_by_id(pool, (user_id, conv_id))
    if not conv:
        return CONVERSATION_HISTORY

    history = json.loads(conv.get('history', '[]'))
    resume_idx = len(history)

    await update_conversation_resume(pool, user_id, conv_id, resume_idx)

    keyboard = [[InlineKeyboardButton(_("\U0001f519 Back"), callback_data=f"CONV_SELECT#{conv_id}")]]
    await query.edit_message_text(
        f"\U0001f4cd Resume point set at message {resume_idx}.\n\nWhen you continue this conversation, you'll see where you left off.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONVERSATION_HISTORY
