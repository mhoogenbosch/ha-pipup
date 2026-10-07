"""Push: the webhook is set, the poll drops to a heartbeat, a push fires pipup_event."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.pipup.const import DOMAIN, PUSH_HEARTBEAT_INTERVAL

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


async def test_push_heartbeat_and_event(hass: HomeAssistant, hass_client_no_auth) -> None:
    await hass.config.async_update(internal_url="http://192.168.1.2:8123")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="TV",
        unique_id="dev1",
        data={"host": "192.168.1.14", "port": 7979},
        options={"update_source": "off"},
    )
    entry.add_to_hass(hass)
    settings = AsyncMock(return_value={})
    with patch.multiple(
        "custom_components.pipup.api.PiPupClient",
        state=AsyncMock(return_value=dict(STATE)),
        settings=settings,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        assert coordinator.push_active
        assert coordinator.update_interval == PUSH_HEARTBEAT_INTERVAL
        webhook_url = next(
            c.kwargs["webhook"] for c in settings.call_args_list if "webhook" in c.kwargs
        )
        assert webhook_url.startswith("http://192.168.1.2:8123/api/webhook/")

        events = async_capture_events(hass, "pipup_event")
        client = await hass_client_no_auth()
        path = webhook_url.removeprefix("http://192.168.1.2:8123")
        resp = await client.post(
            path,
            json={**STATE, "visible": True, "popup": {"id": "bell"},
                  "popups": [{"id": "bell"}], "event": "popup_shown", "shownId": "bell"},
        )
        assert resp.status == 200
        await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["event"] == "popup_shown"
    assert data["shown_id"] == "bell"
    assert data["pipup_id"] == "dev1"
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "dev1"), entry.entry_id)
    assert device is not None and data["device_id"] == device.id
    assert coordinator.data["visible"] is True


async def test_push_set_up_after_app_update(hass: HomeAssistant) -> None:
    """An app that gains push after setup (self-update) gets the webhook at the next poll."""
    await hass.config.async_update(internal_url="http://192.168.1.2:8123")
    entry = MockConfigEntry(
        domain=DOMAIN, title="TV", unique_id="dev1",
        data={"host": "192.168.1.14", "port": 7979}, options={"update_source": "off"},
    )
    entry.add_to_hass(hass)
    old = {**STATE, "version": "0.23.0"}
    old.pop("push")
    tv = {"now": old}
    settings = AsyncMock(return_value={})
    with patch.multiple(
        "custom_components.pipup.api.PiPupClient",
        state=AsyncMock(side_effect=lambda *_: dict(tv["now"])),
        settings=settings,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        assert not coordinator.push_active

        tv["now"] = dict(STATE)  # the app updated itself to 0.24.0+
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.push_active
        assert coordinator.update_interval == PUSH_HEARTBEAT_INTERVAL
        assert any("webhook" in c.kwargs for c in settings.call_args_list)
