"""Tests for providers.registry.ProviderRegistry."""
import pytest

from providers.registry import ProviderRegistry
from tests.mocks.mock_provider import MockProvider


@pytest.fixture(autouse=True)
def reset_registry():
    """Ensure a clean registry for every test."""
    ProviderRegistry.reset()
    yield
    ProviderRegistry.reset()


class TestSingleton:
    def test_singleton_returns_same_instance(self):
        r1 = ProviderRegistry()
        r2 = ProviderRegistry()
        assert r1 is r2

    def test_reset_clears_singleton(self):
        r1 = ProviderRegistry()
        ProviderRegistry.reset()
        r2 = ProviderRegistry()
        assert r1 is not r2

    def test_reset_clears_providers(self):
        reg = ProviderRegistry()
        reg.register(MockProvider())
        assert len(reg.list_providers()) == 1
        ProviderRegistry.reset()
        reg2 = ProviderRegistry()
        assert len(reg2.list_providers()) == 0


class TestRegisterAndGet:
    def test_register_provider(self):
        reg = ProviderRegistry()
        provider = MockProvider()
        reg.register(provider)
        assert "mock" in reg.list_providers()

    def test_get_registered_provider(self):
        reg = ProviderRegistry()
        provider = MockProvider()
        reg.register(provider)
        result = reg.get("mock")
        assert result is provider

    def test_get_unknown_provider_returns_none(self):
        reg = ProviderRegistry()
        assert reg.get("nonexistent") is None

    def test_list_providers_empty(self):
        reg = ProviderRegistry()
        assert reg.list_providers() == []

    def test_list_providers_multiple(self):
        reg = ProviderRegistry()
        mock1 = MockProvider()
        mock1.provider_name = "provider_a"
        mock2 = MockProvider()
        mock2.provider_name = "provider_b"
        reg.register(mock1)
        reg.register(mock2)
        names = reg.list_providers()
        assert "provider_a" in names
        assert "provider_b" in names
        assert len(names) == 2

    def test_get_all_returns_dict(self):
        reg = ProviderRegistry()
        provider = MockProvider()
        reg.register(provider)
        all_providers = reg.get_all()
        assert isinstance(all_providers, dict)
        assert "mock" in all_providers
        assert all_providers["mock"] is provider

    def test_register_overwrites_existing(self):
        reg = ProviderRegistry()
        provider1 = MockProvider()
        provider2 = MockProvider()
        reg.register(provider1)
        reg.register(provider2)
        assert reg.get("mock") is provider2
        assert len(reg.list_providers()) == 1
