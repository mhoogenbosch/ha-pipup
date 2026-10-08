"""Update entity: progress ends when the version changes; odd versions and a dead source."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pipup.const import DOMAIN
from custom_components.pipup.update import _latest_release_tag

RELEASES = "https://api.github.com/repos/mhoogenbosch/PiPup/releases/latest"
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


async def test_install_lands_without_latest_tag(hass: HomeAssistant) -> None:
    """GitHub unreachable (latest None): a changed version still ends the progress."""
    tv = dict(STATE)
    entry, patchers = await _setup(hass, tv, "0.25.0")
    try:
        from custom_components.pipup import update

        entity_id = _entity_id(hass)
        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True
        )
        assert hass.states.get(entity_id).attributes["in_progress"] is True

        # the release source stops answering (rate limit) while the TV installs
        assert await async_setup_component(hass, "homeassistant", {})
        update._latest_release_tag.return_value = None
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
        )
        assert hass.states.get(entity_id).attributes["latest_version"] is None

        tv["version"] = "0.24.0"  # the app updated itself
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).attributes["in_progress"] is False
    finally:
        for p in patchers:
            p.stop()


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


async def test_release_check_shared_and_failure_cached(hass: HomeAssistant, aioclient_mock) -> None:
    """Parallel checks make one request; a failed one is remembered (no retry storm)."""
    source = "github:mhoogenbosch/PiPup"
    aioclient_mock.get(RELEASES, status=403)
    tags = await asyncio.gather(*(_latest_release_tag(hass, source) for _ in range(5)))
    assert tags == [None] * 5
    assert aioclient_mock.call_count == 1
    assert await _latest_release_tag(hass, source) is None
    assert aioclient_mock.call_count == 1

    # force still asks again
    aioclient_mock.clear_requests()
    aioclient_mock.get(RELEASES, json={"tag_name": "v0.25.0"})
    assert await _latest_release_tag(hass, source, force=True) == "0.25.0"
    assert await _latest_release_tag(hass, source) == "0.25.0"
    assert aioclient_mock.call_count == 1
