"""A TV at a new address: the entry follows it without core's deprecated reload (#55).

Until HA 2026.12 the config flow reloaded an updated entry itself and warned when the
entry had an update listener ("has an update listener and should use it for scheduling
a reload"). From 2026.12 that reload is the listener's job, so the listener has to
notice a changed host:port.
"""
from __future__ import annotations

from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pipup.const import DOMAIN

OLD = "192.168.1.14"
NEW = "192.168.1.77"
STATE = {"app": "PiPup", "version": "0.30.1", "id": "dev1", "name": "TV", "visible": False, "popup": None}
DEPRECATION = "has an update listener"


@pytest.fixture
def client():
    """Every PiPupClient answers as device dev1, wherever it is."""
    with patch.multiple(
        "custom_components.pipup.api.PiPupClient",
        state=AsyncMock(return_value=dict(STATE)),
        settings=AsyncMock(return_value={}),
    ):
        yield


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="PiPup", unique_id="dev1",
        data={"host": OLD, "port": 7979}, options={"update_source": "off"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.client.host == OLD
    return entry


def _zeroconf(host: str) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        port=7979,
        hostname="Android.local.",
        type="_pipup._tcp.local.",
        name="TV._pipup._tcp.local.",
        properties={"id": "dev1", "name": "TV"},
    )


async def test_new_address_reloads_onto_it(hass: HomeAssistant, client) -> None:
    entry = await _setup(hass)
    hass.config_entries.async_update_entry(entry, data={"host": NEW, "port": 7979})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.client.host == NEW


async def test_zeroconf_moves_entry_without_core_reload(
    hass: HomeAssistant, client, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await _setup(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(NEW)
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == NEW
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.client.host == NEW
    assert DEPRECATION not in caplog.text


async def test_user_step_moves_entry_without_core_reload(
    hass: HomeAssistant, client, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await _setup(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": NEW, "port": 7979}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.runtime_data.client.host == NEW
    assert DEPRECATION not in caplog.text


async def test_same_address_does_not_reload(hass: HomeAssistant, client) -> None:
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(OLD)
    )
    await hass.async_block_till_done()
    assert result["reason"] == "already_configured"
    assert entry.runtime_data is coordinator  # not rebuilt


async def test_option_change_keeps_the_coordinator(hass: HomeAssistant, client) -> None:
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "default_position": "top_left"}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator
