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

LESSON_SCHEMA = {
    "type": "object",
    "properties": {
        "lesson": {
            "type": "string",
            "description": "The full lesson content in markdown format",
        },
        "quiz": {
            "type": "array",
            "description": "2 multiple-choice questions about today's lesson",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question (max 300 chars)"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string", "description": "Answer option (max 100 chars)"},
                    },
                    "correct": {"type": "integer", "description": "0-based index of the correct option"},
                    "explanation": {"type": "string", "description": "Brief explanation (max 200 chars)"},
                },
                "required": ["question", "options", "correct", "explanation"],
            },
        },
    },
    "required": ["lesson", "quiz"],
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
