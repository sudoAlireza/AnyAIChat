"""Tests for providers.base types, flags, and error hierarchy."""
import pytest

from providers.base import (
    Capability,
    ModelInfo,
    ChatMessage,
    ChatResponse,
    ProviderError,
    RateLimitError,
    AuthenticationError,
    ModelNotFoundError,
    ContentFilterError,
    ServiceUnavailableError,
    InsufficientQuotaError,
    AIProvider,
    StructuredOutputProvider,
)


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------

class TestCapability:
    def test_individual_flags(self):
        assert Capability.TEXT_CHAT is not None
        assert Capability.STREAMING is not None
        assert Capability.VISION is not None

    def test_flag_combination_with_or(self):
        combo = Capability.TEXT_CHAT | Capability.STREAMING
        assert Capability.TEXT_CHAT in combo
        assert Capability.STREAMING in combo
        assert Capability.VISION not in combo

    def test_flag_combination_multiple(self):
        combo = (
            Capability.TEXT_CHAT | Capability.STREAMING | Capability.VISION
            | Capability.WEB_SEARCH
        )
        assert Capability.TEXT_CHAT in combo
        assert Capability.STREAMING in combo
        assert Capability.VISION in combo
        assert Capability.WEB_SEARCH in combo
        assert Capability.IMAGE_GENERATION not in combo

    def test_empty_capability(self):
        empty = Capability(0)
        assert Capability.TEXT_CHAT not in empty
        assert Capability.STREAMING not in empty

    def test_flag_and_operation(self):
        combo = Capability.TEXT_CHAT | Capability.STREAMING | Capability.VISION
        result = combo & Capability.TEXT_CHAT
        assert result == Capability.TEXT_CHAT

    def test_all_flags_unique(self):
        all_flags = [
            Capability.TEXT_CHAT, Capability.STREAMING, Capability.VISION,
            Capability.IMAGE_GENERATION, Capability.AUDIO_INPUT,
            Capability.AUDIO_OUTPUT, Capability.THINKING_MODE,
            Capability.CODE_EXECUTION, Capability.WEB_SEARCH,
            Capability.STRUCTURED_OUTPUT, Capability.TOOL_USE,
            Capability.CONTEXT_CACHING, Capability.FILE_UPLOAD,
            Capability.EMBEDDINGS,
        ]
        values = [f.value for f in all_flags]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# ModelInfo
# ---------------------------------------------------------------------------

class TestModelInfo:
    def test_creation_with_required_fields(self):
        model = ModelInfo(id="test-model", display_name="Test Model", provider="test")
        assert model.id == "test-model"
        assert model.display_name == "Test Model"
        assert model.provider == "test"

    def test_defaults(self):
        model = ModelInfo(id="m", display_name="M", provider="p")
        assert model.capabilities == Capability(0)
        assert model.context_window == 0
        assert model.max_output == 0
        assert model.input_price_per_mtok == 0.0
        assert model.output_price_per_mtok == 0.0

    def test_creation_with_all_fields(self):
        caps = Capability.TEXT_CHAT | Capability.STREAMING
        model = ModelInfo(
            id="gpt-4",
            display_name="GPT-4",
            provider="openai",
            capabilities=caps,
            context_window=128000,
            max_output=8192,
            input_price_per_mtok=30.0,
            output_price_per_mtok=60.0,
        )
        assert model.context_window == 128000
        assert model.max_output == 8192
        assert model.input_price_per_mtok == 30.0
        assert Capability.TEXT_CHAT in model.capabilities


# ---------------------------------------------------------------------------
# ChatMessage
# ---------------------------------------------------------------------------

class TestChatMessage:
    def test_creation_with_role_only(self):
        msg = ChatMessage(role="user")
        assert msg.role == "user"
        assert msg.content == ""
        assert msg.images == []
        assert msg.files == []
        assert msg.metadata == {}

    def test_creation_with_content(self):
        msg = ChatMessage(role="assistant", content="Hello!")
        assert msg.role == "assistant"
        assert msg.content == "Hello!"

    def test_creation_with_metadata(self):
        msg = ChatMessage(role="user", content="hi", metadata={"provider": "gemini"})
        assert msg.metadata == {"provider": "gemini"}

    def test_images_default_is_independent(self):
        msg1 = ChatMessage(role="user")
        msg2 = ChatMessage(role="user")
        msg1.images.append(b"image_data")
        assert msg2.images == []

    def test_metadata_default_is_independent(self):
        msg1 = ChatMessage(role="user")
        msg2 = ChatMessage(role="user")
        msg1.metadata["key"] = "value"
        assert "key" not in msg2.metadata


# ---------------------------------------------------------------------------
# ChatResponse
# ---------------------------------------------------------------------------

class TestChatResponse:
    def test_creation_empty(self):
        resp = ChatResponse()
        assert resp.text == ""
        assert resp.usage == {}
        assert resp.sources == []
        assert resp.metadata == {}

    def test_creation_with_fields(self):
        resp = ChatResponse(
            text="Hello",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            sources=[{"url": "https://example.com"}],
            metadata={"model": "test"},
        )
        assert resp.text == "Hello"
        assert resp.usage["total_tokens"] == 30
        assert len(resp.sources) == 1
        assert resp.metadata["model"] == "test"

    def test_defaults_are_independent(self):
        r1 = ChatResponse()
        r2 = ChatResponse()
        r1.usage["tokens"] = 100
        assert "tokens" not in r2.usage


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class TestErrorHierarchy:
    def test_provider_error_is_exception(self):
        assert issubclass(ProviderError, Exception)

    def test_rate_limit_is_provider_error(self):
        assert issubclass(RateLimitError, ProviderError)

    def test_authentication_is_provider_error(self):
        assert issubclass(AuthenticationError, ProviderError)

    def test_model_not_found_is_provider_error(self):
        assert issubclass(ModelNotFoundError, ProviderError)

    def test_content_filter_is_provider_error(self):
        assert issubclass(ContentFilterError, ProviderError)

    def test_service_unavailable_is_provider_error(self):
        assert issubclass(ServiceUnavailableError, ProviderError)

    def test_insufficient_quota_is_provider_error(self):
        assert issubclass(InsufficientQuotaError, ProviderError)

    def test_provider_error_attributes(self):
        err = ProviderError("something failed", provider="openai", original=ValueError("orig"))
        assert str(err) == "something failed"
        assert err.provider == "openai"
        assert isinstance(err.original, ValueError)

    def test_subclass_inherits_attributes(self):
        err = RateLimitError("too fast", provider="gemini")
        assert err.provider == "gemini"
        assert err.original is None

    def test_catch_by_base_class(self):
        with pytest.raises(ProviderError):
            raise AuthenticationError("bad key", provider="openai")


# ---------------------------------------------------------------------------
# Protocol checks (runtime_checkable)
# ---------------------------------------------------------------------------

class TestProtocolChecks:
    def test_mock_provider_is_ai_provider(self):
        from tests.mocks.mock_provider import MockProvider
        provider = MockProvider()
        assert isinstance(provider, AIProvider)

    def test_mock_provider_is_structured_output_provider(self):
        from tests.mocks.mock_provider import MockProvider
        provider = MockProvider()
        assert isinstance(provider, StructuredOutputProvider)

    def test_plain_object_is_not_ai_provider(self):
        class NotAProvider:
            pass
        assert not isinstance(NotAProvider(), AIProvider)
