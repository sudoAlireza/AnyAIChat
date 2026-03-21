"""Knowledge base (RAG) management handlers — extracted from bot/conversation_handlers.py."""

import os
import json
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.common import restricted, _, _get_pool
from handlers.states import KNOWLEDGE_MENU, KNOWLEDGE_INPUT
from config import GEMINI_API_TOKEN, RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP
from database.database import (
    add_knowledge_with_content, get_user_knowledge, delete_knowledge,
    save_knowledge_chunks, delete_chunks_by_knowledge_id,
)
from helpers.sanitize import safe_filename

logger = logging.getLogger(__name__)


@restricted
async def open_knowledge_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    knowledge = await get_user_knowledge(pool, update.effective_user.id)

    text = "📚 Your Knowledge Base (RAG):\n\n"
    keyboard = [[InlineKeyboardButton(_("➕ Add Document"), callback_data="Add_Knowledge")]]

    if knowledge:
        for doc in knowledge:
            text += f"• {doc['file_name']}\n"
            keyboard.append([InlineKeyboardButton(_(f"Delete {doc['file_name']}"), callback_data=f"KNOWLEDGE_DELETE#{doc['id']}")])
    else:
        text += "No documents uploaded. These documents will be used as context for all your conversations."

    keyboard.append([InlineKeyboardButton(_("🔙 Back to Main Menu"), callback_data="Start_Again")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return KNOWLEDGE_MENU


@restricted
async def start_add_knowledge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(_("Please upload a document (PDF or Text) that you want to add to your knowledge base."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Knowledge_Menu")]]))
    return KNOWLEDGE_INPUT


@restricted
async def handle_knowledge_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.document:
        await update.message.reply_text(_("Please upload a document."))
        return KNOWLEDGE_INPUT

    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    file_path = safe_filename(doc.file_id, doc.file_name, prefix="rag")
    await file.download_to_drive(file_path)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
    model_name = context.user_data.get("model_name")
    provider_name = context.user_data.get("active_provider", "gemini")

    try:
        pool = _get_pool(context)
        user_id = update.effective_user.id

        # File upload and processing is Gemini-specific for now
        if provider_name == "gemini":
            from providers.registry import ProviderRegistry
            provider = ProviderRegistry().get("gemini")
            uploaded_file = await provider.upload_file(api_key, file_path, doc.mime_type)
            preview = await provider.generate_content_with_file(api_key, model_name or "gemini-1.5-flash", "Summarize this document in 2-3 sentences.", uploaded_file)

            full_content = None
            try:
                full_content = await provider.generate_content_with_file(api_key, model_name or "gemini-1.5-flash", "Extract and return the full text content.", uploaded_file)
            except Exception:
                pass

            knowledge_id = await add_knowledge_with_content(pool, user_id, doc.file_name, doc.file_id, preview, full_content)

            # RAG chunk creation
            if full_content and len(full_content) > 100:
                try:
                    from helpers.embeddings import chunk_text
                    chunks = chunk_text(full_content, chunk_size=RAG_CHUNK_SIZE, overlap=RAG_CHUNK_OVERLAP)
                    embeddings = await provider.embed(api_key, chunks)
                    chunks_data = [(idx, chunk, json.dumps(emb)) for idx, (chunk, emb) in enumerate(zip(chunks, embeddings))]
                    await save_knowledge_chunks(pool, user_id, knowledge_id, chunks_data)
                    logger.info(f"Created {len(chunks)} RAG chunks for knowledge doc {knowledge_id}")
                except Exception as rag_err:
                    logger.warning(f"RAG chunk creation failed: {rag_err}")
        else:
            # For non-Gemini providers, read the file locally
            full_content = None
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    full_content = f.read()
                preview = full_content[:200] + "..." if len(full_content) > 200 else full_content
            except Exception:
                preview = "Document uploaded (preview not available)"
                full_content = None

            knowledge_id = await add_knowledge_with_content(pool, user_id, doc.file_name, doc.file_id, preview, full_content)

        await update.message.reply_text(_("Document added to Knowledge Base!"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to Knowledge Menu"), callback_data="Knowledge_Menu")]]))
    except Exception as e:
        logger.error(f"Failed to process RAG document: {e}")
        await update.message.reply_text(_("Failed to process document. Make sure it's a valid text or PDF."))
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning(f"Failed to clean up RAG temp file: {e}")

    return KNOWLEDGE_MENU


@restricted
async def delete_knowledge_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    doc_id = int(query.data.split("#")[1])

    pool = _get_pool(context)
    await delete_chunks_by_knowledge_id(pool, doc_id)
    await delete_knowledge(pool, update.effective_user.id, doc_id)
    await open_knowledge_menu(update, context)
    return KNOWLEDGE_MENU
