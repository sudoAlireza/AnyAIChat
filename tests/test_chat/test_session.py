"""Tests for chat.session.ChatSession."""
import pytest

from providers.base import ChatResponse
from providers.registry import ProviderRegistry
from chat.session import ChatSession
from tests.mocks.mock_provider import MockProvider


@pytest.fixture(autouse=True)
def setup_registry():
    """Register MockProvider before each test, reset after."""
    ProviderRegistry.reset()
    reg = ProviderRegistry()
    reg.register(MockProvider())
    yield
    ProviderRegistry.reset()


# ---------------------------------------------------------------------------
# _convert_history
# ---------------------------------------------------------------------------

class TestConvertHistory:
    def test_old_gemini_format(self):
        """Old Gemini format {role: "model", parts: [{text: "..."}]} is converted."""
        old_history = [
            {"role": "user", "parts": [{"text": "hello"}]},
            {"role": "model", "parts": [{"text": "hi there"}]},
        ]
        result = ChatSession._convert_history(old_history)
        assert len(result) == 2
        assert result[0].role == "user"
        assert result[0].content == "hello"
        assert result[1].role == "assistant"  # "model" -> "assistant"
        assert result[1].content == "hi there"

    def test_old_gemini_format_metadata(self):
        """Migrated entries carry provider=gemini, migrated=True metadata."""
        old_history = [{"role": "model", "parts": [{"text": "test"}]}]
        result = ChatSession._convert_history(old_history)
        assert result[0].metadata.get("provider") == "gemini"
        assert result[0].metadata.get("migrated") is True

    def test_old_gemini_format_multiple_parts(self):
        """Multiple text parts in old format are joined with newlines."""
        old_history = [
            {"role": "user", "parts": [{"text": "part1"}, {"text": "part2"}]},
        ]
        result = ChatSession._convert_history(old_history)
        assert result[0].content == "part1\npart2"

    def test_old_gemini_format_string_parts(self):
        """Parts can also be plain strings."""
        old_history = [
            {"role": "user", "parts": ["hello world"]},
        ]
        result = ChatSession._convert_history(old_history)
        assert result[0].content == "hello world"

    def test_new_universal_format(self):
        """New universal format {role, content} passes through correctly."""
        new_history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = ChatSession._convert_history(new_history)
        assert len(result) == 2
        assert result[0].role == "user"
        assert result[0].content == "hello"
        assert result[1].role == "assistant"
        assert result[1].content == "hi there"

    def test_new_format_model_role_normalized(self):
        """Even in new format, 'model' role is mapped to 'assistant'."""
        new_history = [{"role": "model", "content": "response"}]
        result = ChatSession._convert_history(new_history)
        assert result[0].role == "assistant"

    def test_new_format_with_metadata(self):
        """Metadata from new format is preserved."""
        new_history = [
            {"role": "user", "content": "test", "metadata": {"source": "voice"}},
        ]
        result = ChatSession._convert_history(new_history)
        assert result[0].metadata == {"source": "voice"}

    def test_empty_history(self):
        result = ChatSession._convert_history([])
        assert result == []

    def test_mixed_formats(self):
        """Old and new format entries can coexist."""
        mixed = [
            {"role": "user", "parts": [{"text": "old msg"}]},
            {"role": "assistant", "content": "new msg"},
        ]
        result = ChatSession._convert_history(mixed)
        assert len(result) == 2
        assert result[0].content == "old msg"
        assert result[1].content == "new msg"


# ---------------------------------------------------------------------------
# get_history_as_dicts
# ---------------------------------------------------------------------------

class TestGetHistoryAsDicts:
    def test_returns_universal_format(self):
        session = ChatSession(
            provider_name="mock",
            api_key="valid-key",
            model_name="mock-model-1",
            history=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )
        dicts = session.get_history_as_dicts()
        assert len(dicts) == 2
        assert dicts[0] == {"role": "user", "content": "hello", "metadata": {}}
        assert dicts[1] == {"role": "assistant", "content": "hi", "metadata": {}}

    def test_old_format_exported_as_universal(self):
        session = ChatSession(
            provider_name="mock",
            api_key="valid-key",
            model_name="mock-model-1",
            history=[{"role": "model", "parts": [{"text": "old"}]}],
        )
        dicts = session.get_history_as_dicts()
        assert dicts[0]["role"] == "assistant"
        assert dicts[0]["content"] == "old"
        assert "parts" not in dicts[0]

    def test_empty_history(self):
        session = ChatSession(
            provider_name="mock",
            api_key="valid-key",
            model_name="mock-model-1",
        )
        assert session.get_history_as_dicts() == []


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_appends_to_history(self):
        session = ChatSession(
            provider_name="mock",
            api_key="valid-key",
            model_name="mock-model-1",
        )
        assert session.get_history_length() == 0
        response = await session.send_message("Hello")
        # History should have user message + assistant response
        assert session.get_history_length() == 2
        dicts = session.get_history_as_dicts()
        assert dicts[0]["role"] == "user"
        assert dicts[0]["content"] == "Hello"
        assert dicts[1]["role"] == "assistant"
        assert "Mock response to: Hello" in dicts[1]["content"]

    @pytest.mark.asyncio
    async def test_send_message_returns_chat_response(self):
        session = ChatSession(
            provider_name="mock",
            api_key="valid-key",
            model_name="mock-model-1",
        )
        response = await session.send_message("Test prompt")
        assert isinstance(response, ChatResponse)
        assert "Mock response to: Test prompt" in response.text
        assert response.usage["total_tokens"] == 30

    @pytest.mark.asyncio
    async def test_send_multiple_messages(self):
        session = ChatSession(
            provider_name="mock",
            api_key="valid-key",
            model_name="mock-model-1",
        )
        await session.send_message("First")
        await session.send_message("Second")
        assert session.get_history_length() == 4  # 2 user + 2 assistant


# ---------------------------------------------------------------------------
# one_shot
# ---------------------------------------------------------------------------

class TestOneShot:
    @pytest.mark.asyncio
    async def test_one_shot_delegates_to_provider(self):
        session = ChatSession(
            provider_name="mock",
            api_key="valid-key",
            model_name="mock-model-1",
        )
        response = await session.one_shot("Quick question")
        assert isinstance(response, ChatResponse)
        assert "Mock one-shot response to: Quick question" in response.text
        assert response.usage["total_tokens"] == 30

    @pytest.mark.asyncio
    async def test_one_shot_does_not_affect_history(self):
        session = ChatSession(
            provider_name="mock",
            api_key="valid-key",
            model_name="mock-model-1",
        )
        await session.one_shot("Quick question")
        assert session.get_history_length() == 0


# ---------------------------------------------------------------------------
# get_chat_title
# ---------------------------------------------------------------------------

class TestGetChatTitle:
    @pytest.mark.asyncio
    async def test_get_chat_title_returns_string(self):
        session = ChatSession(
            provider_name="mock",
            api_key="valid-key",
            model_name="mock-model-1",
            history=[
                {"role": "user", "content": "Tell me about Python"},
                {"role": "assistant", "content": "Python is a programming language."},
            ],
        )
        title = await session.get_chat_title()
        assert isinstance(title, str)
        assert len(title) > 0
        # MockProvider's chat_structured returns {"title": "Mock Title"}
        assert title == "Mock Title"

    @pytest.mark.asyncio
    async def test_get_chat_title_empty_history(self):
        session = ChatSession(
            provider_name="mock",
            api_key="valid-key",
            model_name="mock-model-1",
        )
        title = await session.get_chat_title()
        assert isinstance(title, str)
        assert len(title) > 0
