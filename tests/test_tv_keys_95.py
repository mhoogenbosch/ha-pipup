"""2.1.0b1 (PiPup #95): the remote's other buttons, waking, closing system dialogs, turning
off with the sleep key over ADB, and the extra media session fields."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.media_player import MediaPlayerEntityFeature as F
from homeassistant.core import HomeAssistant

from custom_components.pipup.adb import (
    ADB_CLOSE_DIALOGS,
    AdbError,
    async_adb_close_dialogs,
    async_adb_key,
    valid_adb_keycode,
)
from custom_components.pipup.api import PiPupError
from custom_components.pipup.coordinator import PiPupCoordinator
from custom_components.pipup.services import SEND_KEY_SCHEMA

from .test_adb import STATE, _section, _setup
from .test_media_click import SESSION, TV, _player

ADB = {"install_method": "adb"}


def _coordinator(data: dict, options: dict, keys: list[str] | None = None) -> PiPupCoordinator:
    coordinator = PiPupCoordinator.__new__(PiPupCoordinator)
    coordinator.hass = MagicMock()
    control = {**TV["control"], "keys": keys if keys is not None else ["play_pause"]}
    coordinator.data = {**TV, **data, "control": control}
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = options
    coordinator.config_entry.title = "TV"
    coordinator.client = MagicMock()
    coordinator.client.host = "192.168.1.21"
    coordinator.client.key = AsyncMock(return_value={"ok": True})
    coordinator.client.volume = AsyncMock(return_value={"ok": True})
    coordinator.client.power = AsyncMock(return_value={"state": "off", "ok": True})
    return coordinator


# ------------------------------------------------------------------ the action's key field


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("up", "up"),
        ("OK", "ok"),
        ("center", "center"),  # an alias; the coordinator maps it to ok
        ("close_dialogs", "close_dialogs"),
        ("KEYCODE_TV_INPUT", "KEYCODE_TV_INPUT"),
        ("keycode_dpad_up", "KEYCODE_DPAD_UP"),
        (" sleep ", "sleep"),
    ],
)
def test_key_names_accepted(given, expected) -> None:
    assert SEND_KEY_SCHEMA({"key": given})["key"] == expected


@pytest.mark.parametrize(
    "given",
    [
        "KEYCODE_HOME; reboot",
        "KEYCODE_HOME && reboot",
        "KEYCODE_",
        "KEYCODE_" + "A" * 41,
        "KEYCODE_HOME\nreboot",
        "keyevent 3",
        "rm -rf /",
        "",
    ],
)
def test_key_names_refused(given) -> None:
    """Nothing but a listed name or a strict KEYCODE_ name gets near a shell command."""
    with pytest.raises(vol.Invalid):
        SEND_KEY_SCHEMA({"key": given})


def test_adb_keycode_check() -> None:
    assert valid_adb_keycode("KEYCODE_DPAD_UP")
    assert valid_adb_keycode("KEYCODE_TV_INPUT_HDMI_1")
    assert not valid_adb_keycode("KEYCODE_HOME;reboot")
    assert not valid_adb_keycode("KEYCODE_HOME\n")
    assert not valid_adb_keycode("home")


async def test_adb_key_refuses_a_bad_name_before_connecting() -> None:
    with patch("custom_components.pipup.adb.PiPupAdb") as adb:
        with pytest.raises(ValueError):
            await async_adb_key(MagicMock(), "192.168.1.21", ADB, "KEYCODE_HOME; reboot")
    adb.assert_not_called()


async def test_close_dialogs_over_adb_is_one_fixed_command() -> None:
    device = MagicMock()
    device.connect = AsyncMock()
    device.shell = AsyncMock(return_value="Broadcast completed: result=0")
    device.__aenter__ = AsyncMock(return_value=device)
    device.__aexit__ = AsyncMock(return_value=False)
    with patch("custom_components.pipup.adb.PiPupAdb", return_value=device):
        out = await async_adb_close_dialogs(MagicMock(), "192.168.1.21", ADB)
    device.shell.assert_awaited_once_with(ADB_CLOSE_DIALOGS)
    assert ADB_CLOSE_DIALOGS == "am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS"
    assert out.startswith("Broadcast completed")


# ------------------------------------------------------------------ routing


async def test_a_key_the_app_can_send_goes_to_the_app() -> None:
    """D-pad on Android 13+: the app lists it, so no ADB even where ADB is set up."""
    coordinator = _coordinator({}, ADB, keys=["play_pause", "up", "ok"])
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        await coordinator.async_send_key("up")
        await coordinator.async_send_key("center")  # alias of ok
    assert [c.args for c in coordinator.client.key.await_args_list] == [("up",), ("ok",)]
    adb.assert_not_awaited()


@pytest.mark.parametrize(
    ("key", "keycode"),
    [
        ("up", "KEYCODE_DPAD_UP"),
        ("ok", "KEYCODE_DPAD_CENTER"),
        ("select", "KEYCODE_DPAD_CENTER"),
        ("menu", "KEYCODE_MENU"),
        ("power", "KEYCODE_POWER"),
        ("sleep", "KEYCODE_SLEEP"),
        ("home", "KEYCODE_HOME"),
        ("KEYCODE_TV_INPUT", "KEYCODE_TV_INPUT"),
    ],
)
async def test_other_keys_go_over_adb(key, keycode) -> None:
    coordinator = _coordinator({}, ADB)
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        answer = await coordinator.async_send_key(key)
    coordinator.client.key.assert_not_awaited()
    assert adb.await_args.args[1:] == ("192.168.1.21", ADB, keycode)
    assert answer["via"] == "adb"


async def test_without_adb_the_key_is_refused_with_a_hint() -> None:
    """No tested ADB setup: never open ADB (the TV could ask "Allow debugging?")."""
    coordinator = _coordinator({}, {})
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        with pytest.raises(PiPupError, match="Advanced install"):
            await coordinator.async_send_key("menu")
    adb.assert_not_awaited()


async def test_an_adb_failure_is_reported() -> None:
    coordinator = _coordinator({}, ADB)
    adb = AsyncMock(side_effect=AdbError("cannot_connect"))
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        with pytest.raises(PiPupError, match="menu over ADB failed"):
            await coordinator.async_send_key("menu")


async def test_volume_goes_through_the_app() -> None:
    coordinator = _coordinator({}, {})
    await coordinator.async_send_key("volume_up")
    await coordinator.async_send_key("volume_down")
    await coordinator.async_send_key("mute")
    assert [c.kwargs for c in coordinator.client.volume.await_args_list] == [
        {"step": "up"}, {"step": "down"}, {"mute": "toggle"},
    ]


async def test_close_dialogs_through_the_app_up_to_android_11() -> None:
    coordinator = _coordinator({}, ADB, keys=["play_pause", "close_dialogs"])
    close = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_close_dialogs", close):
        await coordinator.async_send_key("close_dialogs")
    coordinator.client.key.assert_awaited_once_with("close_dialogs")
    close.assert_not_awaited()


async def test_close_dialogs_over_adb_from_android_12() -> None:
    coordinator = _coordinator({}, ADB)
    close = AsyncMock(return_value="Broadcast completed: result=0")
    with patch("custom_components.pipup.coordinator.async_adb_close_dialogs", close):
        answer = await coordinator.async_send_key("close_dialogs")
    close.assert_awaited_once()
    assert answer["via"] == "adb"


async def test_close_dialogs_without_a_route_says_so() -> None:
    coordinator = _coordinator({}, {})
    with pytest.raises(PiPupError, match="close_dialogs needs ADB"):
        await coordinator.async_send_key("close_dialogs")


# ------------------------------------------------------------------ wakeup


async def test_wakeup_through_the_app() -> None:
    coordinator = _coordinator({}, ADB, keys=["play_pause", "wakeup"])
    coordinator.client.key = AsyncMock(
        return_value={"ok": True, "woke": True, "screenOn": True, "dreaming": False}
    )
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        answer = await coordinator.async_send_key("wakeup")
    coordinator.client.key.assert_awaited_once_with("wakeup")
    adb.assert_not_awaited()
    assert answer["woke"] is True


async def test_wakeup_falls_back_to_adb_when_the_screen_stays_off() -> None:
    coordinator = _coordinator({"screenOn": False}, ADB, keys=["play_pause", "wakeup"])
    coordinator.client.key = AsyncMock(return_value={"ok": True, "woke": False, "screenOn": False})
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        answer = await coordinator.async_send_key("wakeup")
    assert adb.await_args.args[3] == "KEYCODE_WAKEUP"
    assert answer["woken_over_adb"] is True


async def test_wakeup_without_adb_returns_what_the_app_said() -> None:
    coordinator = _coordinator({"screenOn": False}, {}, keys=["play_pause", "wakeup"])
    coordinator.client.key = AsyncMock(return_value={"ok": True, "woke": False, "screenOn": False})
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        answer = await coordinator.async_send_key("wakeup")
    adb.assert_not_awaited()
    assert answer["screenOn"] is False


async def test_wakeup_on_an_older_app_leaves_a_lit_screen_alone() -> None:
    """No /power on while the screen is on: its wake activity could pause the app on screen."""
    coordinator = _coordinator({"screenOn": True, "dreaming": False}, ADB)
    answer = await coordinator.async_send_key("wakeup")
    coordinator.client.power.assert_not_awaited()
    coordinator.client.key.assert_not_awaited()
    assert answer["woke"] is False


@pytest.mark.parametrize("data", [{"screenOn": False}, {"screenOn": True, "dreaming": True}])
async def test_wakeup_on_an_older_app_uses_power_on(data) -> None:
    coordinator = _coordinator(data, {})
    coordinator.client.power = AsyncMock(return_value={"state": "on", "ok": True, "screenOn": True})
    answer = await coordinator.async_send_key("wakeup")
    coordinator.client.power.assert_awaited_once_with("on")
    assert answer["screenOn"] is True


# ------------------------------------------------------------------ turning off


async def test_turn_off_with_the_sleep_key_over_adb() -> None:
    coordinator = _coordinator({}, {**ADB, "sleep_over_adb": True})
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        answer = await coordinator.async_screen_off()
    assert adb.await_args.args[3] == "KEYCODE_SLEEP"
    coordinator.client.power.assert_not_awaited()
    assert answer["state"] == "off" and answer["via"] == "adb"


@pytest.mark.parametrize(
    "options",
    [ADB, {"sleep_over_adb": False}, {"sleep_over_adb": True}],  # off, off, on but ADB gone
)
async def test_turn_off_through_the_app_otherwise(options) -> None:
    coordinator = _coordinator({}, options)
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        await coordinator.async_screen_off()
    coordinator.client.power.assert_awaited_once_with("off")
    adb.assert_not_awaited()


def test_turn_off_is_offered_when_the_sleep_key_goes_over_adb() -> None:
    """The app cannot turn this TV off (no device admin), the sleep key over ADB can."""
    player = _player(dict(TV))
    player.coordinator.config_entry.options = {}
    assert not player.supported_features & F.TURN_OFF
    player.coordinator.config_entry.options = {**ADB, "sleep_over_adb": True}
    assert player.supported_features & F.TURN_OFF


async def test_options_refuse_the_sleep_key_without_adb(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, dict(STATE))
    try:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _section(sleep_over_adb=True)
        )
        assert result["type"] == "form"
        assert result["errors"] == {"advanced_install": "sleep_needs_adb"}
        assert "sleep_over_adb" not in entry.options
    finally:
        for p in patchers:
            p.stop()


async def test_options_save_the_sleep_key_with_adb(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, dict(STATE))
    try:
        with patch(
            "custom_components.pipup.config_flow.PiPupOptionsFlow._test_adb",
            AsyncMock(return_value=None),
        ):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _section(install_method="adb", sleep_over_adb=True)
            )
        assert result["type"] == "create_entry"
        assert entry.options["sleep_over_adb"] is True
    finally:
        for p in patchers:
            p.stop()


# ------------------------------------------------------------------ media session fields


def test_extra_session_fields_only_when_present() -> None:
    session = {**SESSION, "subtitle": "S2 · E5", "description": None, "albumArtist": "",
               "discNumber": 2, "trackNumber": 5}
    attrs = _player({**TV, "media": session}).extra_state_attributes
    assert attrs["media_subtitle"] == "S2 · E5"
    assert attrs["media_disc_number"] == 2
    assert attrs["media_track_number"] == 5
    assert "media_description" not in attrs
    assert "media_album_artist" not in attrs
    # an older app reports none of them
    attrs = _player({**TV, "media": SESSION}).extra_state_attributes
    assert not any(key.startswith(("media_subtitle", "media_disc")) for key in attrs)


def test_new_strings_are_translated() -> None:
    import json
    from pathlib import Path

    base = Path(__file__).parent.parent / "custom_components" / "pipup"
    for name in ("strings.json", "translations/en.json", "translations/nl.json"):
        data = json.loads((base / name).read_text())
        advanced = data["options"]["step"]["init"]["sections"]["advanced_install"]
        assert "sleep_over_adb" in advanced["data"]
        assert "sleep_over_adb" in advanced["data_description"]
        assert "sleep_needs_adb" in data["options"]["error"]
        for key in ("up", "ok", "menu", "wakeup", "close_dialogs", "sleep"):
            assert key in data["selector"]["key"]["options"]
