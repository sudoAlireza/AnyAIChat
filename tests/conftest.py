"""Shared test fixtures."""
import pytest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

@pytest.fixture
def mock_provider():
    from tests.mocks.mock_provider import MockProvider
    return MockProvider()

@pytest.fixture
def registry(mock_provider):
    from providers.registry import ProviderRegistry
    ProviderRegistry.reset()
    reg = ProviderRegistry()
    reg.register(mock_provider)
    return reg
