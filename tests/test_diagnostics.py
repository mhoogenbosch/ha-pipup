"""Diagnostics: the push webhook id never leaves HA in a download."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pipup.const import CONF_PUSH_WEBHOOK_ID, DOMAIN
from custom_components.pipup.diagnostics import async_get_config_entry_diagnostics

STATE = {
    "app": "PiPup",
    "version": "0.24.0",
    "id": "dev1",
    "name": "Living Room",
    "visible": False,
    "popup": None,
    "popups": [],
    "push": {"supported": True, "webhook": True},
}


async def test_diagnostics_redact_push_webhook_id(hass: HomeAssistant) -> None:
    await hass.config.async_update(internal_url="http://192.168.1.2:8123")
    entry = MockConfigEntry(
        domain=DOMAIN, title="TV", unique_id="dev1",
        data={"host": "192.168.1.14", "port": 7979}, options={"update_source": "off"},
    )
    entry.add_to_hass(hass)
    with patch.multiple(
        "custom_components.pipup.api.PiPupClient",
        state=AsyncMock(return_value=dict(STATE)),
        settings=AsyncMock(return_value={}),
        diagnose=AsyncMock(return_value=None),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.data[CONF_PUSH_WEBHOOK_ID]  # push set it up

        diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["data"][CONF_PUSH_WEBHOOK_ID] == "**REDACTED**"
    assert diag["entry"]["data"]["host"] == "192.168.1.14"
    assert entry.data[CONF_PUSH_WEBHOOK_ID] != "**REDACTED**"
