"""Parent device: the PiPup device hangs under the TV's own device."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pipup.const import DOMAIN

HOST = "192.168.1.14"
STATE = {"app": "PiPup", "version": "0.25.0", "id": "dev1", "name": "TV", "visible": False, "popup": None}


def _tv(hass: HomeAssistant, domain: str, name: str, host: str = HOST) -> str:
    entry = MockConfigEntry(domain=domain, title=name, data={"host": host})
    entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(domain, name)}, name=name
    ).id


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="PiPup", unique_id="dev1",
        data={"host": HOST, "port": 7979}, options={"update_source": "off", **(options or {})},
    )
    entry.add_to_hass(hass)
    with patch.multiple(
        "custom_components.pipup.api.PiPupClient",
        state=AsyncMock(return_value=dict(STATE)),
        settings=AsyncMock(return_value={}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _via(hass: HomeAssistant, entry: MockConfigEntry) -> str | None:
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "dev1"), entry.entry_id)
    return device.via_device_id


async def test_prefers_remote_over_adb_and_ignores_others(hass: HomeAssistant) -> None:
    _tv(hass, "nfandroidtv", "notify")
    adb = _tv(hass, "androidtv", "adb")
    remote = _tv(hass, "androidtv_remote", "remote")
    _tv(hass, "androidtv_remote", "other tv", host="192.168.1.99")
    entry = await _setup(hass)
    assert _via(hass, entry) == remote
    assert adb != remote


async def test_no_tv_integration_no_parent(hass: HomeAssistant) -> None:
    _tv(hass, "nfandroidtv", "notify")
    entry = await _setup(hass)
    assert _via(hass, entry) is None


async def test_option_wins_and_late_tv_is_linked(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert _via(hass, entry) is None
    adb = _tv(hass, "androidtv", "adb")  # TV integration added after PiPup
    await hass.async_block_till_done()
    assert _via(hass, entry) == adb

    chosen = _tv(hass, "androidtv_remote", "chosen", host="192.168.1.50")
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(entry, options={**entry.options, "parent_device": chosen})
    await hass.async_block_till_done()
    assert _via(hass, entry) == chosen
