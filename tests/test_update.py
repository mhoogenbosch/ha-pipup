"""Update entity: odd installed versions."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pipup.const import DOMAIN

STATE = {
    "app": "PiPup",
    "version": "0.23.0",
    "id": "dev1",
    "name": "Living Room",
    "visible": False,
    "popup": None,
    "update": {"installing": False},
}


async def _setup(hass: HomeAssistant, tv: dict, latest: str | None):
    entry = MockConfigEntry(
        domain=DOMAIN, title="TV", unique_id="dev1",
        data={"host": "192.168.1.14", "port": 7979}, options={},
    )
    entry.add_to_hass(hass)
    patchers = [
        patch.multiple(
            "custom_components.pipup.api.PiPupClient",
            state=AsyncMock(side_effect=lambda *_: dict(tv)),
            settings=AsyncMock(return_value={}),
            update_app=AsyncMock(return_value=None),
        ),
        patch("custom_components.pipup.update._latest_release_tag",
              AsyncMock(return_value=latest)),
        patch("custom_components.pipup.coordinator.POST_CALL_SETTLE_SECONDS", 0),
    ]
    for p in patchers:
        p.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, patchers


def _entity_id(hass: HomeAssistant) -> str:
    return next(s.entity_id for s in hass.states.async_all("update"))


async def test_unparseable_installed_version(hass: HomeAssistant) -> None:
    """An installed version AwesomeVersion cannot compare does not break the update."""
    entry, patchers = await _setup(hass, {**STATE, "version": "unknown"}, "0.25.0")
    try:
        state = hass.states.get(_entity_id(hass))
        assert state.attributes["latest_version"] == "0.25.0"
        assert state.attributes["installed_version"] == "unknown"
    finally:
        for p in patchers:
            p.stop()
