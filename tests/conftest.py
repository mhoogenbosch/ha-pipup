"""Test fixtures: load the custom integration from this repo."""
import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow custom_components/pipup to load in every test."""
    yield
