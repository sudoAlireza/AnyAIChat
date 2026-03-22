"""Response formatting utilities for Telegram output."""

from __future__ import annotations



def format_sources(sources: list[dict]) -> str:
    """Format grounding/web search sources for Telegram display."""
    if not sources:
        return ""
    lines = ["\n\n📎 *Sources:*"]
    seen = set()
    for src in sources:
        uri = src.get("uri", "")
        title = src.get("title", uri)
        if uri and uri not in seen:
            seen.add(uri)
            lines.append(f"• [{title}]({uri})")
    return "\n".join(lines) if len(lines) > 1 else ""


def format_usage_summary(usage: dict, provider: str | None = None) -> str:
    """Format token usage for display, optionally including the provider name."""
    if not usage:
        return ""
    parts = []
    if provider:
        parts.append(f"Provider: {provider}")
    if usage.get("total_tokens"):
        parts.append(f"Tokens: {usage['total_tokens']:,}")
    if usage.get("cached_tokens"):
        parts.append(f"Cached: {usage['cached_tokens']:,}")
    if usage.get("thinking_tokens"):
        parts.append(f"Thinking: {usage['thinking_tokens']:,}")
    return " | ".join(parts)
