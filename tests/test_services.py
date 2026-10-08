"""pipup.show / pipup.dismiss over several TVs: concurrent, errors collected, no refresh on push."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pipup.api import PiPupError
from custom_components.pipup.const import DOMAIN

BASE = {"app": "PiPup", "visible": False, "popup": None}
# host -> /state; .11 pushes (app 0.24.0), .12 and .13 are polled (app 0.23.0)
TVS = {
    "192.168.1.11": {**BASE, "version": "0.24.0", "id": "push", "name": "Push TV",
                     "popups": [], "push": {"supported": True, "webhook": True}},
    "192.168.1.12": {**BASE, "version": "0.23.0", "id": "poll", "name": "Poll TV"},
    "192.168.1.13": {**BASE, "version": "0.23.0", "id": "dead", "name": "Dead TV"},
}


async def _setup(hass: HomeAssistant):
    await hass.config.async_update(internal_url="http://192.168.1.2:8123")
    arrived: list[str] = []
    all_in = asyncio.Event()

    async def state(self):
        return dict(TVS[self.host])

    async def settings(self, **values):
        return {}

    async def call(self, *args, **kwargs):
        # every TV must be in flight at once: a sequential loop never gets past this
        arrived.append(self.host)
        if len(arrived) == len(TVS):
            all_in.set()
        await asyncio.wait_for(all_in.wait(), 2)
        if self.host == "192.168.1.13":
            raise PiPupError("Cannot reach PiPup at http://192.168.1.13:7979: off")

    entries = {}
    for host, tv in TVS.items():
        entry = MockConfigEntry(
            domain=DOMAIN, title=tv["name"], unique_id=tv["id"],
            data={"host": host, "port": 7979}, options={"update_source": "off"},
        )
        entry.add_to_hass(hass)
        entries[host] = entry
    patcher = patch.multiple(
        "custom_components.pipup.api.PiPupClient",
        state=state, settings=settings, notify=call, cancel=call,
    )
    patcher.start()
    # setting up the domain sets up every entry
    assert await hass.config_entries.async_setup(entries["192.168.1.11"].entry_id)
    await hass.async_block_till_done()
    for entry in entries.values():
        entry.runtime_data.async_refresh_soon = AsyncMock()
    target = patch(
        "custom_components.pipup.services.async_extract_config_entry_ids",
        AsyncMock(return_value=[e.entry_id for e in entries.values()]),
    )
    target.start()
    return entries, arrived, all_in, [patcher, target]


@pytest.mark.parametrize(
    ("service", "data"), [("show", {"title": "Hi"}), ("dismiss", {"popup_id": "x"})]
)
async def test_all_tvs_called_at_once(hass: HomeAssistant, service: str, data: dict) -> None:
    entries, arrived, _, patchers = await _setup(hass)
    try:
        assert entries["192.168.1.11"].runtime_data.push_active
        assert not entries["192.168.1.12"].runtime_data.push_active
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(DOMAIN, service, data, blocking=True)
        # one TV failed: the others still got the call, the error names only that one
        assert sorted(arrived) == sorted(TVS)
        assert str(err.value) == "Cannot reach PiPup at http://192.168.1.13:7979: off"
        # the pushing TV reports the change itself; the polled one is read again
        entries["192.168.1.11"].runtime_data.async_refresh_soon.assert_not_awaited()
        entries["192.168.1.12"].runtime_data.async_refresh_soon.assert_awaited_once()
        entries["192.168.1.13"].runtime_data.async_refresh_soon.assert_not_awaited()
    finally:
        for p in patchers:
            p.stop()
