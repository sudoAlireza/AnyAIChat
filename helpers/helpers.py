import markdown
import re
from bs4 import BeautifulSoup


def conversations_page_content(convs: dict) -> str:
    page_content = ""
    for index, item in enumerate(convs):
        # Escape potential markdown in title
        title = item.get('title', 'Untitled').replace('*', '\\*').replace('_', '\\_')
        page_content += f"{index+1}.\n*Title*: {title}\n*ConversationID*: /{item.get('conversation_id')}\n\n"

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
