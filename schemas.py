"""JSON schemas for Gemini structured output responses."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Short CamelCase name for the plan (2-4 words, no spaces), used as a hashtag identifier",
        },
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "integer"},
                    "title": {"type": "string", "description": "Short title (3-6 words)"},
                    "subject": {"type": "string", "description": "One actionable sentence"},
                    "phase": {"type": "string", "description": "Week/phase label"},
                },
                "required": ["day", "title", "subject", "phase"],
            },
        },
    },
    "required": ["title", "plan"],
}

CHAT_TITLE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "A one-line short title up to 10 words for the conversation",
        },
    },
    "required": ["title"],
}

VOICE_COMMAND_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["start_task", "set_reminder", "generate_image", "none"],
        },
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "text": {"type": "string"},
                "time": {"type": "string"},
                "prompt": {"type": "string"},
            },
        },
    },
    "required": ["action", "parameters"],
}

REMINDER_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "The reminder text"},
        "datetime": {"type": "string", "description": "YYYY-MM-DD HH:MM format"},
        "recurring": {
            "type": ["string", "null"],
            "enum": [None, "daily", "weekly"],
            "description": "Recurring interval or null for one-time",
        },
    },
    "required": ["text", "datetime", "recurring"],
}
