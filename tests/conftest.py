"""Test fixtures: load the custom integration from this repo."""
import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow custom_components/pipup to load in every test."""
    yield


@pytest.fixture(autouse=True)
def no_adb_wake_retry_delay(monkeypatch):
    """2.1.0b2: the wait before the second KEYCODE_WAKEUP is real time; not in tests."""
    monkeypatch.setattr("custom_components.pipup.coordinator.ADB_WAKE_RETRY_DELAY", 0)
