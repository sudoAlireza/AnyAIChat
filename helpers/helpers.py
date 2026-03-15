import markdown
import re
from bs4 import BeautifulSoup


def conversations_page_content(convs: dict) -> str:
    page_content = "💬 *Your Conversations*\n"
    page_content += "━" * 24 + "\n\n"

    for index, item in enumerate(convs):
        title = item.get('title', 'Untitled').replace('*', '\\*').replace('_', '\\_')
        msg_count = item.get('message_count', 0)
        created = item.get('created_at', '')

        # Format date nicely
        date_str = ""
        if created:
            try:
                from datetime import datetime
                dt = datetime.strptime(created[:16], "%Y-%m-%d %H:%M")
                date_str = dt.strftime("%b %d, %H:%M")
            except (ValueError, TypeError):
                date_str = str(created)[:10]

        # Preview of last message
        preview = item.get('last_message', '')
        if preview:
            preview = preview.replace('*', '').replace('_', '').replace('`', '')
            if len(preview) > 60:
                preview = preview[:57] + "..."
            preview = f"\n     _{preview}_"
        else:
            preview = ""

        page_content += f"{index + 1}. *{title}*\n"
        page_content += f"     📊 {msg_count} msgs  •  📅 {date_str}{preview}\n\n"

    return page_content


def strip_markdown(md: str) -> str:
    html = markdown.markdown(md)
    soup = BeautifulSoup(html, features="html.parser")
    return soup.get_text()


def split_message(text: str, limit: int = 4000) -> list:
    """Splits a long message into multiple messages, respecting word boundaries."""
    if len(text) <= limit:
        return [text]
    
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        
        # Find the last newline within the limit
        split_at = text.rfind('\n', 0, limit)
        
        # If no newline, find the last space
        if split_at == -1:
            split_at = text.rfind(' ', 0, limit)
        
        # If still no good split point, just cut at the limit
        if split_at == -1:
            split_at = limit
            
        parts.append(text[:split_at].strip())
        text = text[split_at:].strip()
        
    return parts

def escape_markdown_v2(text: str) -> str:
    """Escapes special characters for Telegram's MarkdownV2."""
    # List of characters that need to be escaped in MarkdownV2 outside of code blocks/links
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
