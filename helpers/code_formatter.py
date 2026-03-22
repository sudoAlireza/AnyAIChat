"""Format Gemini code execution responses for Telegram."""


def format_code_execution_response(response) -> tuple:
    """Extract executable_code and code_execution_result parts from a Gemini response.

    Returns (text_parts, code_blocks) where:
    - text_parts: the normal text content
    - code_blocks: list of dicts with 'code', 'output', 'outcome' keys
    """
    text_parts = []
    code_blocks = []

    candidates = getattr(response, 'candidates', None)
    if not candidates:
        return "", []

    for candidate in candidates:
        parts = getattr(candidate, 'content', None)
        if not parts:
            continue
        parts = getattr(parts, 'parts', [])

        pending_code = None
        for part in parts:
            if hasattr(part, 'executable_code') and part.executable_code:
                pending_code = getattr(part.executable_code, 'code', '')
            elif hasattr(part, 'code_execution_result') and part.code_execution_result:
                result = part.code_execution_result
                output = getattr(result, 'output', '')
                outcome = getattr(result, 'outcome', 'OUTCOME_OK')
                code_blocks.append({
                    'code': pending_code or '',
                    'output': output,
                    'outcome': str(outcome),
                })
                pending_code = None
            elif hasattr(part, 'text') and part.text:
                text_parts.append(part.text)

        # If there was code without a result block
        if pending_code:
            code_blocks.append({
                'code': pending_code,
                'output': '',
                'outcome': 'PENDING',
            })

    return "\n".join(text_parts), code_blocks


def format_code_blocks_for_telegram(code_blocks: list) -> str:
    """Format code execution blocks for Telegram display."""
    if not code_blocks:
        return ""

    parts = []
    for block in code_blocks:
        if block.get('code'):
            parts.append(f"```python\n{block['code']}\n```")
        if block.get('output'):
            outcome = block.get('outcome', '')
            if 'ERROR' in outcome.upper():
                parts.append(f"*Error:*\n```\n{block['output']}\n```")
            else:
                parts.append(f"*Output:*\n```\n{block['output']}\n```")

    return "\n".join(parts)
