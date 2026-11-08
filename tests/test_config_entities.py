"""Settings on the device page (#53): automatic updates switch, reload button; buffer_ms."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pipup.const import DOMAIN
from custom_components.pipup.services import SHOW_SCHEMA, build_device_payload

SWITCH = "switch.tv_automatic_updates"
BUTTON = "button.tv_reload"


def _state(android: str = "14", silent: bool | None = True) -> dict:
    update = {} if silent is None else {"silent": silent}
    return {
        "app": "PiPup", "version": "0.31.0", "id": "dev1", "name": "TV", "visible": False,
        "popup": None, "device": {"android": android}, "update": update,
    }


@pytest.fixture
def tv():
    """The TV's /state; a test may change it before setup."""
    return _state()


@pytest.fixture
async def entry(hass: HomeAssistant, tv: dict, request) -> MockConfigEntry:
    options = getattr(request, "param", {})
    entry = MockConfigEntry(
        domain=DOMAIN, title="TV", unique_id="dev1",
        data={"host": "192.168.1.14", "port": 7979}, options=options,
    )
    entry.add_to_hass(hass)
    patchers = [
        patch.multiple(
            "custom_components.pipup.api.PiPupClient",
            state=AsyncMock(side_effect=lambda *_: dict(tv)),
            settings=AsyncMock(return_value={}),
        ),
        patch("custom_components.pipup.update._latest_release_tag", AsyncMock(return_value=None)),
    ]
    for p in patchers:
        p.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    for p in patchers:
        p.stop()


async def test_switch_turns_automatic_updates_on_and_off(hass: HomeAssistant, entry) -> None:
    assert hass.states.get(SWITCH).state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    assert entry.options["auto_update"] is True
    assert hass.states.get(SWITCH).state == "on"
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    assert entry.options["auto_update"] is False
    assert hass.states.get(SWITCH).state == "off"


async def test_switch_does_not_reload_the_entry(hass: HomeAssistant, entry) -> None:
    coordinator = entry.runtime_data
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator


@pytest.mark.parametrize("tv", [_state(android="11", silent=False)])
async def test_switch_refuses_when_the_tv_needs_the_remote(hass: HomeAssistant, entry) -> None:
    # Android 11, app self-update (the default): every install needs a press on the remote
    with pytest.raises(HomeAssistantError, match="Advanced install"):
        await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    assert not entry.options.get("auto_update")


@pytest.mark.parametrize("tv", [_state(android="11", silent=False)])
@pytest.mark.parametrize("entry", [{"install_method": "adb"}], indirect=True)
async def test_switch_allows_adb_on_an_older_tv(hass: HomeAssistant, entry) -> None:
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    assert entry.options["auto_update"] is True


async def test_switch_follows_the_options_flow(hass: HomeAssistant, entry) -> None:
    hass.config_entries.async_update_entry(entry, options={**entry.options, "auto_update": True})
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "on"


@pytest.mark.parametrize("entry", [{"update_source": "off"}], indirect=True)
async def test_no_switch_without_updates(hass: HomeAssistant, entry) -> None:
    assert hass.states.get(SWITCH) is None
    assert er.async_get(hass).async_get_entity_id("switch", DOMAIN, "dev1_auto_update") is None


async def test_reload_button_reloads_the_entry(hass: HomeAssistant, entry) -> None:
    coordinator = entry.runtime_data
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not coordinator


def test_buffer_ms_goes_to_a_video_popup() -> None:
    data = SHOW_SCHEMA({"title": "Doorbell", "video_url": "rtsp://cam/main", "buffer_ms": 150})
    payload = build_device_payload(data, {}, None)
    assert payload["media"]["video"]["bufferMs"] == 150


def test_buffer_ms_goes_to_a_camera_stream() -> None:
    payload = build_device_payload({"title": "Doorbell", "buffer_ms": 200}, {}, "rtsp://go2rtc/doorbell")
    assert payload["media"]["video"]["bufferMs"] == 200


def test_buffer_ms_is_left_out_elsewhere() -> None:
    assert "media" not in build_device_payload({"title": "t", "buffer_ms": 200}, {}, None)
    whep = build_device_payload({"title": "t", "whep_url": "http://g/api/webrtc?src=x", "buffer_ms": 200}, {}, None)
    assert "bufferMs" not in whep["media"]["whep"]
    plain = build_device_payload({"title": "t", "video_url": "rtsp://cam"}, {}, None)
    assert "bufferMs" not in plain["media"]["video"]


@pytest.mark.parametrize("value", [50, 20000])
def test_buffer_ms_range(value: int) -> None:
    with pytest.raises(Exception):
        SHOW_SCHEMA({"title": "t", "video_url": "rtsp://cam", "buffer_ms": value})
