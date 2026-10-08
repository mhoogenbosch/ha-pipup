"""Overlays: entities, show/hide, redraw, following the pushed state, subentry flow."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.pipup.const import DOMAIN

STATE = {
    "app": "PiPup",
    "version": "0.23.0",
    "id": "dev1",
    "name": "Living Room",
    "visible": False,
    "popup": None,
}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="TV",
        unique_id="dev1",
        data={"host": "192.168.1.14", "port": 7979},
        options={"update_source": "off", "overlay_pages": "Fantasy | http://x/tv\nScores | http://x/scores"},
        subentries_data=[
            ConfigSubentryData(
                subentry_type="overlay",
                title="Fantasy",
                data={"popup_id": "fantasy"},
                subentry_id="sub1",
                unique_id=None,
            )
        ],
    )


# what the fake TV shows now: /state reads return it, push() changes it
TV: dict = {}


def push(coordinator, **changes) -> None:
    """The TV changed and pushed its new state."""
    TV.clear()
    TV.update({**STATE, **changes})
    coordinator.async_set_updated_data(dict(TV))


async def _setup(hass: HomeAssistant):
    TV.clear()
    TV.update(STATE)
    entry = _entry()
    entry.add_to_hass(hass)

    async def _state(*_args):
        return dict(TV)

    client = patch.multiple(
        "custom_components.pipup.api.PiPupClient",
        state=AsyncMock(side_effect=_state),
        settings=AsyncMock(return_value={}),
        notify=AsyncMock(return_value=None),
        cancel=AsyncMock(return_value=None),
    )
    client.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, client


async def test_overlay_flow(hass: HomeAssistant) -> None:
    entry, client = await _setup(hass)
    try:
        from custom_components.pipup.api import PiPupClient

        coordinator = entry.runtime_data
        sw = "switch.fantasy_overlay"
        assert hass.states.get(sw).state == "off"
        page = hass.states.get("select.fantasy_overlay_page")
        assert page.state == "Fantasy"
        assert page.attributes["options"] == ["Fantasy", "Scores", "Custom URL"]
        assert hass.states.get("sensor.fantasy_overlay_status").state == "Off"
        assert hass.states.get("text.fantasy_overlay_border_color").state == ""
        assert hass.states.get("number.fantasy_overlay_width").state == "420"

        # show: the payload the app gets
        await hass.services.async_call("switch", "turn_on", {"entity_id": sw}, blocking=True)
        payload = PiPupClient.notify.await_args.args[0]
        assert payload["id"] == "fantasy"
        assert payload["media"]["web"] == {"uri": "http://x/tv", "width": 420, "height": 900, "muted": True}
        assert payload["backgroundColor"] == "#CC000000"
        assert payload["position"] == 0 and payload["duration"] == 0
        assert "title" not in payload and "borderColor" not in payload
        # optimistic until the TV confirms
        assert hass.states.get(sw).state == "on"

        # the TV pushes: popup up
        push(coordinator, visible=True, popup={"id": "fantasy"})
        await hass.async_block_till_done()
        assert hass.states.get(sw).state == "on"
        assert hass.states.get("sensor.fantasy_overlay_status").state.startswith("Showing since")

        # a setting change while up redraws once, after the delay
        calls = PiPupClient.notify.await_count
        await hass.services.async_call("number", "set_value", {"entity_id": "number.fantasy_overlay_width", "value": 600}, blocking=True)
        await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.fantasy_overlay_transparent"}, blocking=True)
        assert hass.states.get("select.fantasy_overlay_layout").state == "Custom"
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
        await hass.async_block_till_done()
        assert PiPupClient.notify.await_count == calls + 1
        web = PiPupClient.notify.await_args.args[0]["media"]["web"]
        assert web["width"] == 600 and web["transparent"] is True

        # layout preset sets size and position
        await hass.services.async_call("select", "select_option", {"entity_id": "select.fantasy_overlay_layout", "option": "Strip bottom"}, blocking=True)
        assert hass.states.get("number.fantasy_overlay_width").state == "1880"
        assert hass.states.get("select.fantasy_overlay_position").state == "bottom_left"

        # replaced by another popup: switch off, status says by what
        coordinator.last_event = {"event": "popup_replaced", "replacedId": "fantasy"}
        push(coordinator, visible=True, popup={"id": "doorbell"})
        await hass.async_block_till_done()
        assert hass.states.get(sw).state == "off"
        assert hass.states.get("sensor.fantasy_overlay_status").state.startswith("Replaced by popup doorbell")

        # up again, then expired on the TV
        push(coordinator, visible=True, popup={"id": "fantasy"})
        await hass.async_block_till_done()
        coordinator.last_event = {"event": "popup_removed", "reason": "expired", "removedId": "fantasy"}
        push(coordinator)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.fantasy_overlay_status").state.startswith("Off: time up")

        # hide while up: cancel with our id, status "Hidden"
        push(coordinator, visible=True, popup={"id": "fantasy"})
        await hass.async_block_till_done()
        await hass.services.async_call("switch", "turn_off", {"entity_id": sw}, blocking=True)
        PiPupClient.cancel.assert_awaited_with("fantasy")
        push(coordinator)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.fantasy_overlay_status").state.startswith("Hidden at")

        # pattern-checked text
        await hass.services.async_call("text", "set_value", {"entity_id": "text.fantasy_overlay_border_color", "value": "#FF0000"}, blocking=True)
        assert hass.states.get("text.fantasy_overlay_border_color").state == "#FF0000"
    finally:
        client.stop()


async def test_add_overlay_and_pages(hass: HomeAssistant) -> None:
    entry, client = await _setup(hass)
    try:
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "overlay"), context={"source": "user"}
        )
        assert result["type"] == "form"
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"name": "Fantasy"}
        )
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
        new = [s for s in entry.subentries.values() if s.subentry_id != "sub1"][0]
        assert new.data["popup_id"] == "fantasy_2"
        assert hass.states.get("switch.fantasy_overlay_2") is not None

        # page list edited in the options: selects follow without a reload
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"overlay_pages": "Only | http://y/"}
        )
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
        page = hass.states.get("select.fantasy_overlay_page")
        assert page.attributes["options"] == ["Only", "Custom URL"]
        assert page.state == "Only"

        # removing an overlay removes its entities
        assert hass.config_entries.async_remove_subentry(entry, new.subentry_id)
        await hass.async_block_till_done()
        assert hass.states.get("switch.fantasy_overlay_2") is None
    finally:
        client.stop()


async def test_several_popups(hass: HomeAssistant) -> None:
    """App 0.24.0: other popups beside the overlay leave it on; sensors list them."""
    entry, client = await _setup(hass)
    try:
        from custom_components.pipup.api import PiPupClient

        coordinator = entry.runtime_data
        sw = "switch.fantasy_overlay"
        on_top = {"id": "fantasy"}
        push(coordinator, version="0.24.0", visible=True, popup=on_top, popups=[on_top])
        await hass.async_block_till_done()
        assert hass.states.get(sw).state == "on"

        # a second popup opens on top: the overlay stays on and keeps its status
        status = hass.states.get("sensor.fantasy_overlay_status").state
        coordinator.last_event = {"event": "popup_shown", "shownId": "doorbell"}
        push(coordinator, version="0.24.0", visible=True, popup={"id": "doorbell"},
             popups=[on_top, {"id": "doorbell"}])
        await hass.async_block_till_done()
        assert hass.states.get(sw).state == "on"
        assert hass.states.get("sensor.fantasy_overlay_status").state == status

        # the sensor entities appear on the next setup (they depend on the app's fields)
        TV.update(version="0.24.0", visible=True, popup={"id": "doorbell"},
                  popups=[on_top, {"id": "doorbell"}, {"id": None}])
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        count = [s for s in hass.states.async_all("sensor") if s.entity_id.endswith("popups_on_screen")]
        assert count and count[0].state == "3"
        current = [s for s in hass.states.async_all("sensor") if s.entity_id.endswith("current_popup")][0]
        assert current.state == "doorbell"
        assert current.attributes["popups"] == ["fantasy", "doorbell", None]

        # Redraw on top: no redraw of its own, then sent with the next redraw
        calls = PiPupClient.notify.await_count
        await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.fantasy_overlay_redraw_on_top"}, blocking=True)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
        await hass.async_block_till_done()
        assert PiPupClient.notify.await_count == calls
        await hass.services.async_call("number", "set_value", {"entity_id": "number.fantasy_overlay_width", "value": 500}, blocking=True)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
        await hass.async_block_till_done()
        assert PiPupClient.notify.await_args.args[0]["bringToFront"] is True

        # dismiss: the id-less popup by default, everything with all
        target = {"entity_id": [s.entity_id for s in hass.states.async_all("binary_sensor") if s.entity_id.endswith("_popup")][0]}
        await hass.services.async_call(DOMAIN, "dismiss", target, blocking=True)
        PiPupClient.cancel.assert_awaited_with(None, all_popups=False)
        await hass.services.async_call(DOMAIN, "dismiss", {**target, "all": True}, blocking=True)
        PiPupClient.cancel.assert_awaited_with(None, all_popups=True)
    finally:
        client.stop()


async def test_push_after_overlay_removed(hass: HomeAssistant, caplog) -> None:
    """A push between removing an overlay and the reload does not raise."""
    entry, client = await _setup(hass)
    try:
        coordinator = entry.runtime_data
        overlays = coordinator.overlays
        # the subentry is gone, the reload has not run yet
        with patch.object(hass.config_entries, "async_schedule_reload"):
            assert hass.config_entries.async_remove_subentry(entry, "sub1")
            await hass.async_block_till_done()
        assert "sub1" in overlays.subentry_ids
        push(coordinator, visible=True, popup={"id": "fantasy"})
        await hass.async_block_till_done()
        assert "KeyError" not in caplog.text
    finally:
        client.stop()
