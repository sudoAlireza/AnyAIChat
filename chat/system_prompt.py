"""System instruction builder — extracted from core.py."""

from __future__ import annotations

import os
from typing import Optional


def build_system_instruction(
    language: str | None = None,
    system_instruction: str | None = None,
    pinned_context: str | None = None,
    knowledge_base: list[dict] | None = None,
    rag_context: str | None = None,
) -> str:
    """Build a system instruction string from user settings.

    This is provider-agnostic — the resulting string works with any LLM.
    """
    lang = language if language and language != "auto" else os.getenv("LANGUAGE", "en")
    if lang != "auto":
        lang_instruction = f"Please respond in {lang} language. "
    else:
        lang_instruction = "Respond in the same language the user writes in. "

    instruction = (
        f"{lang_instruction}"
        "Format text using only bold (wrap with single asterisks), italic (wrap with underscores), "
        "inline code (wrap with backticks), and code blocks (wrap with triple backticks). "
        "Do NOT use headers, horizontal rules, or complex tables. "
        "Do NOT escape special characters with backslashes. "
        "Do NOT demonstrate or showcase formatting at the end of your response. Just write naturally."
    )

    if system_instruction:
        instruction += f"\n\nUser-defined persona instructions: {system_instruction}"

    if pinned_context:
        instruction += f"\n\nIMPORTANT persistent context from the user (always keep in mind): {pinned_context}"

    if rag_context:
        instruction += f"\n\nRelevant knowledge base context:\n{rag_context}"
        instruction += "\nUse this information when relevant to answer user queries."
    elif knowledge_base:
        instruction += "\n\nYou have access to the following documents from your knowledge base (context preview):"
        for doc in knowledge_base:
            instruction += f"\n- {doc['file_name']}: {doc['content_preview']}"
        instruction += "\nUse this information when relevant to answer user queries."

    return instruction
