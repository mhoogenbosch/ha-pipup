"""Button webhook: the event comes from the token's popup, not from the request body."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.pipup.const import DOMAIN

STATE = {
    "app": "PiPup",
    "version": "0.23.0",
    "id": "dev1",
    "name": "Living Room",
    "visible": False,
    "popup": None,
}
BUTTONS = [{"id": "unlock", "label": "Open the door"}, {"id": "ignore", "label": "Ignore"}]


async def _show_with_buttons(hass: HomeAssistant, hass_client_no_auth):
    """Show a button popup and return (http client, callback path)."""
    await hass.config.async_update(internal_url="http://192.168.1.2:8123")
    entry = MockConfigEntry(
        domain=DOMAIN, title="TV", unique_id="dev1",
        data={"host": "192.168.1.14", "port": 7979}, options={"update_source": "off"},
    )
    entry.add_to_hass(hass)
    notify = AsyncMock(return_value=None)
    with patch.multiple(
        "custom_components.pipup.api.PiPupClient",
        state=AsyncMock(return_value=dict(STATE)),
        settings=AsyncMock(return_value={}),
        notify=notify,
    ), patch("custom_components.pipup.services.async_extract_config_entry_ids",
             AsyncMock(return_value={entry.entry_id})):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(
            DOMAIN, "show", {"popup_id": "doorbell", "title": "Door", "buttons": BUTTONS},
            blocking=True,
        )
    callback = notify.await_args.args[0]["callback"]
    return await hass_client_no_auth(), callback.removeprefix("http://192.168.1.2:8123")


async def test_issued_button_fires(hass: HomeAssistant, hass_client_no_auth) -> None:
    client, path = await _show_with_buttons(hass, hass_client_no_auth)
    events = async_capture_events(hass, "pipup_button")
    resp = await client.post(path, json={
        "popup": "doorbell", "button": "unlock", "label": "Open the door",
        "device": "dev1", "name": "Living Room",
    })
    assert resp.status == 200
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data == {
        "popup_id": "doorbell", "button": "unlock", "label": "Open the door",
        "device_id": "dev1", "device_name": "Living Room",
    }

    # single use: the same token a second time fires nothing
    await client.post(path, json={"popup": "doorbell", "button": "unlock"})
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_foreign_button_does_not_fire(hass: HomeAssistant, hass_client_no_auth) -> None:
    client, path = await _show_with_buttons(hass, hass_client_no_auth)
    events = async_capture_events(hass, "pipup_button")
    await client.post(path, json={"popup": "doorbell", "button": "unlock_back_door",
                                  "device": "dev1"})
    await hass.async_block_till_done()
    assert events == []


async def test_body_popup_and_device_are_ignored(hass: HomeAssistant, hass_client_no_auth) -> None:
    client, path = await _show_with_buttons(hass, hass_client_no_auth)
    events = async_capture_events(hass, "pipup_button")
    await client.post(path, json={
        "popup": "garage", "button": "unlock", "label": "Forged label",
        "device": "other-tv", "name": "Other TV",
    })
    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert data["popup_id"] == "doorbell"
    assert data["label"] == "Open the door"
    assert data["device_id"] == "dev1" and data["device_name"] == "Living Room"
