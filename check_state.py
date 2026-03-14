import sys
import os

# Add project root to sys.path
sys.path.append(os.getcwd())

try:
    from core import GeminiChat
    print(f"GeminiChat methods: {[m for m in dir(GeminiChat) if not m.startswith('__')]}")
except Exception as e:
    print(f"Failed to import GeminiChat: {e}")

try:
    import bot.conversation_handlers as handlers
    print(f"get_user_knowledge in handlers: {'get_user_knowledge' in dir(handlers)}")
except Exception as e:
    print(f"Failed to import bot.conversation_handlers: {e}")

try:
    from database.database import get_user_knowledge
    print(f"get_user_knowledge in database: {get_user_knowledge is not None}")
except Exception as e:
    print(f"Failed to import get_user_knowledge from database: {e}")
