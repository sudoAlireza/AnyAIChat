"""Mock AI provider for testing."""
from providers.base import Capability, ModelInfo, ChatResponse

class MockProvider:
    provider_name = "mock"
    capabilities = (
        Capability.TEXT_CHAT | Capability.STREAMING | Capability.VISION
        | Capability.STRUCTURED_OUTPUT | Capability.WEB_SEARCH
    )

    async def validate_key(self, api_key: str) -> bool:
        return api_key == "valid-key"

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        return [
            ModelInfo(id="mock-model-1", display_name="Mock Model 1", provider="mock",
                      capabilities=self.capabilities, context_window=128000, max_output=8192),
            ModelInfo(id="mock-model-2", display_name="Mock Model 2", provider="mock",
                      capabilities=Capability.TEXT_CHAT, context_window=32000, max_output=4096),
        ]

    async def chat(self, api_key, model, messages, system_instruction=None, **kwargs):
        last_msg = messages[-1].content if messages else ""
        return ChatResponse(
            text=f"Mock response to: {last_msg}",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    async def chat_stream(self, api_key, model, messages, system_instruction=None, on_update=None, **kwargs):
        last_msg = messages[-1].content if messages else ""
        full_text = f"Mock streaming response to: {last_msg}"
        if on_update:
            await on_update(full_text)
        return ChatResponse(
            text=full_text,
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    async def chat_structured(self, api_key, model, messages, schema, system_instruction=None, **kwargs):
        import json
        return ChatResponse(
            text=json.dumps({"title": "Mock Title"}),
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            metadata={"parsed": {"title": "Mock Title"}},
        )

    async def one_shot(self, api_key, model, prompt, system_instruction=None, **kwargs):
        return ChatResponse(
            text=f"Mock one-shot response to: {prompt}",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )
