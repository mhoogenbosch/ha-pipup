"""PiPup #88 and #89 (app >= 0.33.0): the app in front and a picture of the screen."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.pipup.adb import AdbError, adb_ready
from custom_components.pipup.api import PiPupClient, PiPupError, PiPupUnsupportedError
from custom_components.pipup.diagnostics import async_get_config_entry_diagnostics

from .test_adb import STATE, _adb_session, _fake_device, _section, _setup

BASE_URL = "http://192.168.1.21:7979"
ACCESS = "nl.rogro82.pipup/nl.rogro82.pipup.PiPupAccessibilityService"
CONTROL = "nl.rogro82.pipup/nl.rogro82.pipup.PiPupControlService"
PLEX = "com.plexapp.android"
NETFLIX = "com.netflix.ninja"
TOKEN = "abcdefghijklmnopqrstuvwxyz012345"
APPS = [{"package": NETFLIX, "label": "Netflix", "tv": True}]
TV33 = {
    **STATE, "version": "0.33.0-beta.1", "screenOn": True,
    "power": {"canWake": True, "canSleep": True, "sleepMethod": "device_admin"},
    "control": {"launch": True, "keys": ["play", "pause"], "lastLaunched": PLEX, "screen": True},
    "volume": {"level": 40, "step": 6, "max": 15, "muted": False, "fixed": False},
    "media": {"access": True},
    "foreground": {"package": NETFLIX, "label": "Netflix", "since": "2026-11-14T13:20:00Z"},
}
SCREEN_ON = {"screenshot": True, "screen_token": TOKEN}


@pytest.fixture(autouse=True)
def _no_restart_pause_and_an_app_list():
    """No 2 s restart pause; and the media player reads the app list at every (re)load."""
    with (
        patch("custom_components.pipup.adb.CONTROL_RESTART_PAUSE", 0),
        patch(
            "custom_components.pipup.api.PiPupClient.apps",
            AsyncMock(side_effect=lambda *_: list(APPS)),
        ),
    ):
        yield


_tv = _setup


def _stop(patchers) -> None:
    for p in patchers:
        p.stop()


def _entity_id(hass: HomeAssistant, domain: str) -> str | None:
    return next(
        (e.entity_id for e in er.async_get(hass).entities.values() if e.domain == domain), None
    )


# ------------------------------------------------------------------ client


@pytest.fixture
async def client(hass: HomeAssistant, aioclient_mock) -> PiPupClient:
    return PiPupClient(async_get_clientsession(hass), "192.168.1.21", 7979)


async def test_screen_sends_the_token_in_a_header(client, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/screen.jpg", content=b"\xff\xd8jpeg")
    assert await client.screen(TOKEN) == b"\xff\xd8jpeg"
    assert await client.screen(TOKEN, 320) == b"\xff\xd8jpeg"
    first, second = aioclient_mock.mock_calls
    assert first[3]["X-PiPup-Token"] == TOKEN
    # never in the URL, which ends up in logs
    assert dict(first[1].query) == {}
    assert dict(second[1].query) == {"width": "320"}


@pytest.mark.parametrize("status", [409, 429])
async def test_screen_off_or_too_fast_is_no_picture(client, aioclient_mock, status) -> None:
    aioclient_mock.get(
        f"{BASE_URL}/screen.jpg", status=status, json={"ok": False, "message": "the screen is off"}
    )
    assert await client.screen(TOKEN) is None


@pytest.mark.parametrize(
    ("status", "body", "error", "match"),
    [
        (501, {"ok": False, "message": "the screenshot is switched off"}, PiPupUnsupportedError,
         "switched off"),
        (400, "invalid request: unknown uri: /screen.jpg", PiPupUnsupportedError, "needs >= 0.33.0"),
        (400, {"ok": False, "message": "width must be 160-1920"}, PiPupError, "width must be"),
        (401, {"ok": False, "message": "give the screenshot token"}, PiPupError, r"\(401\)"),
    ],
)
async def test_screen_errors(client, aioclient_mock, status, body, error, match) -> None:
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
    aioclient_mock.get(f"{BASE_URL}/screen.jpg", status=status, **kwargs)
    with pytest.raises(error, match=match):
        await client.screen(TOKEN)


# ------------------------------------------------------------------ media player


async def test_source_and_app_follow_the_app_in_front(hass: HomeAssistant) -> None:
    entry, patchers = await _tv(hass, dict(TV33))
    try:
        state = hass.states.get(_entity_id(hass, "media_player"))
        # in front wins over what PiPup launched last (Plex)
        assert state.attributes["source"] == "Netflix"
        assert state.attributes["app_id"] == NETFLIX
        assert state.attributes["app_name"] == "Netflix"
        # the screenshot is off: no camera, no picture
        assert _entity_id(hass, "camera") is None
        assert "entity_picture" not in state.attributes
    finally:
        _stop(patchers)


async def test_a_media_session_keeps_the_app_and_front_the_source(hass: HomeAssistant) -> None:
    tv = {**TV33, "media": {"access": True, "package": "com.spotify.tv.android", "state": "playing"}}
    entry, patchers = await _tv(hass, tv)
    try:
        state = hass.states.get(_entity_id(hass, "media_player"))
        assert state.attributes["app_id"] == "com.spotify.tv.android"
        assert state.attributes["source"] == "Netflix"
    finally:
        _stop(patchers)


async def test_an_unknown_app_in_front_uses_its_own_label(hass: HomeAssistant) -> None:
    front = {"package": "com.google.android.tvlauncher", "label": "Android TV Home", "since": "x"}
    entry, patchers = await _tv(hass, {**TV33, "foreground": front})
    try:
        state = hass.states.get(_entity_id(hass, "media_player"))
        assert state.attributes["source"] == "Android TV Home"
        assert state.attributes["app_name"] == "Android TV Home"
    finally:
        _stop(patchers)


async def test_a_hidden_app_in_front_is_named_by_its_package(hass: HomeAssistant) -> None:
    front = {"package": "com.hidden.app", "label": None, "since": "x"}
    entry, patchers = await _tv(hass, {**TV33, "foreground": front})
    try:
        state = hass.states.get(_entity_id(hass, "media_player"))
        assert state.attributes["source"] == "com.hidden.app"
        assert state.attributes["app_id"] == "com.hidden.app"
        assert "app_name" not in state.attributes
    finally:
        _stop(patchers)


async def test_without_usage_access_the_last_launch_is_the_source(hass: HomeAssistant) -> None:
    tv = {**TV33, "foreground": None,
          "control": {**TV33["control"], "lastLaunched": NETFLIX}}
    entry, patchers = await _tv(hass, tv)
    try:
        state = hass.states.get(_entity_id(hass, "media_player"))
        assert state.attributes["source"] == "Netflix"
        assert "app_id" not in state.attributes
    finally:
        _stop(patchers)


async def test_the_screen_stands_in_for_artwork(hass: HomeAssistant) -> None:
    screen = AsyncMock(return_value=b"jpeg")
    art = AsyncMock(return_value=(b"art", "image/png"))
    with patch.multiple("custom_components.pipup.api.PiPupClient", screen=screen, media_art=art):
        entry, patchers = await _tv(hass, dict(TV33), dict(SCREEN_ON))
        try:
            entity_id = _entity_id(hass, "media_player")
            assert "entity_picture" in hass.states.get(entity_id).attributes
            player = hass.data["entity_components"]["media_player"].get_entity(entity_id)
            assert player.media_image_hash.startswith("screen-")
            assert await player.async_get_media_image() == (b"jpeg", "image/jpeg")
            screen.assert_awaited_once_with(TOKEN)
            art.assert_not_awaited()

            # artwork wins
            coordinator = entry.runtime_data
            coordinator.data["media"] = {
                "access": True, "package": PLEX, "state": "playing", "artKey": "k1",
            }
            assert player.media_image_hash == "k1"
            assert await player.async_get_media_image() == (b"art", "image/png")

            # the screen is off: nothing to show
            coordinator.data["media"] = {"access": True}
            coordinator.data["screenOn"] = False
            assert player.media_image_hash is None
            # the app says it cannot (Android < 11, the service lost its capability)
            coordinator.data["screenOn"] = True
            coordinator.data["control"] = {**TV33["control"], "screen": False}
            assert player.media_image_hash is None
        finally:
            _stop(patchers)


# ------------------------------------------------------------------ camera


async def test_camera_only_with_the_screenshot_on(hass: HomeAssistant) -> None:
    screen = AsyncMock(return_value=b"jpeg")
    with patch("custom_components.pipup.api.PiPupClient.screen", screen):
        entry, patchers = await _tv(hass, dict(TV33), dict(SCREEN_ON))
        try:
            entity_id = _entity_id(hass, "camera")
            assert entity_id is not None
            camera = hass.data["entity_components"]["camera"].get_entity(entity_id)
            assert await camera.async_camera_image() == b"jpeg"
            screen.assert_awaited_with(TOKEN, None)
            # a requested width stays inside what the app takes
            await camera.async_camera_image(width=4000)
            screen.assert_awaited_with(TOKEN, 1920)
            await camera.async_camera_image(width=10)
            screen.assert_awaited_with(TOKEN, 160)

            # the screen is off: not asked, no picture
            screen.reset_mock()
            entry.runtime_data.data["screenOn"] = False
            assert await camera.async_camera_image() is None
            screen.assert_not_awaited()

            # the TV refuses (another token): no picture, no exception
            entry.runtime_data.data["screenOn"] = True
            screen.side_effect = PiPupError("screen.jpg failed (401)")
            assert await camera.async_camera_image() is None
        finally:
            _stop(patchers)


async def test_the_token_stays_out_of_diagnostics(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.pipup.api.PiPupClient.diagnose", AsyncMock(return_value=None)
    ):
        entry, patchers = await _tv(hass, dict(TV33), dict(SCREEN_ON))
        try:
            dump = await async_get_config_entry_diagnostics(hass, entry)
            assert TOKEN not in str(dump)
            assert dump["entry"]["options"]["screenshot"] is True
        finally:
            _stop(patchers)


# ------------------------------------------------------------------ ADB


def _screen_broadcast(token: str) -> str:
    return (
        "am broadcast -f 0x20 -n nl.rogro82.pipup/.ControlConfigReceiver "
        f'-a nl.rogro82.pipup.SET_SCREEN_TOKEN --es token "{token}"'
    )


async def test_usage_access_is_an_appop(hass: HomeAssistant) -> None:
    device = _fake_device({})
    adb = await _adb_session(hass, device)
    await adb.set_usage_access(True)
    await adb.set_usage_access(False)
    assert device.calls == [
        "appops set nl.rogro82.pipup GET_USAGE_STATS allow",
        "appops set nl.rogro82.pipup GET_USAGE_STATS default",
    ]


async def test_a_token_switches_the_control_service_on(hass: HomeAssistant) -> None:
    device = _fake_device({
        "am broadcast": 'Broadcast completed: result=1, data="on"',
        "settings get secure enabled_accessibility_services": ACCESS,
    })
    adb = await _adb_session(hass, device)
    await adb.set_screen_token(TOKEN)
    assert device.calls[0] == _screen_broadcast(TOKEN)
    assert f"settings put secure enabled_accessibility_services {ACCESS}:{CONTROL}" in device.calls

    # already on (for clicks): off and on, for the capability an older app did not have
    device = _fake_device({
        "am broadcast": 'Broadcast completed: result=1, data="on"',
        "settings get secure enabled_accessibility_services": f"{CONTROL}:{ACCESS}",
    })
    adb = await _adb_session(hass, device)
    await adb.set_screen_token(TOKEN, keep_service=True)
    puts = [c for c in device.calls if c.startswith("settings put secure enabled_accessibility")]
    assert puts == [
        f"settings put secure enabled_accessibility_services {ACCESS}",
        f"settings put secure enabled_accessibility_services {ACCESS}:{CONTROL}",
    ]


async def test_no_token_switches_it_off_unless_clicks_need_it(hass: HomeAssistant) -> None:
    outputs = {
        "am broadcast": 'Broadcast completed: result=1, data="off"',
        "settings get secure enabled_accessibility_services": f"{CONTROL}:{ACCESS}",
    }
    device = _fake_device(outputs)
    adb = await _adb_session(hass, device)
    await adb.set_screen_token("")
    assert device.calls[0] == _screen_broadcast("")
    assert f"settings put secure enabled_accessibility_services {ACCESS}" in device.calls

    device = _fake_device(outputs)
    adb = await _adb_session(hass, device)
    await adb.set_screen_token("", keep_service=True)
    assert not any(c.startswith("settings put") for c in device.calls)


@pytest.mark.parametrize(
    "answer",
    [
        "Broadcast completed: result=0",                    # an app older than 0.33.0
        'Broadcast completed: result=1, data="invalid"',    # the app refused the token
        'Broadcast completed: result=1, data="off"',        # asked on, still off
    ],
)
async def test_a_token_the_app_did_not_take_is_an_error(hass: HomeAssistant, answer) -> None:
    device = _fake_device({"am broadcast": answer})
    adb = await _adb_session(hass, device)
    with pytest.raises(AdbError) as err:
        await adb.set_screen_token(TOKEN)
    assert err.value.reason == "grant_failed"
    assert not any(c.startswith("settings put") for c in device.calls)


async def test_emptying_the_click_list_keeps_the_service_for_the_screenshot(
    hass: HomeAssistant,
) -> None:
    device = _fake_device({
        "am broadcast": 'Broadcast completed: result=1, data=""',
        "settings get secure enabled_accessibility_services": f"{CONTROL}:{ACCESS}",
    })
    adb = await _adb_session(hass, device)
    await adb.set_click_packages([], keep_service=True)
    assert not any(c.startswith("settings put") for c in device.calls)


def test_the_new_grants_count_as_a_tested_adb_setup() -> None:
    assert adb_ready({"foreground_app": True}, {})
    assert adb_ready({"screenshot": True}, {})
    assert not adb_ready({}, {})


# ------------------------------------------------------------------ options


async def test_options_make_and_clear_the_token(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, dict(TV33))
    apply = AsyncMock(return_value=None)
    try:
        with patch("custom_components.pipup.config_flow.PiPupOptionsFlow._apply_grants", apply):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _section(screenshot=True, foreground_app=True)
            )
            assert result["type"] == "create_entry"
            token = entry.options["screen_token"]
            assert len(token) >= 16 and all(c.isalnum() or c in "_-" for c in token)
            assert apply.await_args.args[0]["screen_token"] == token
            assert entry.options["foreground_app"] is True
            await hass.async_block_till_done()

            # saving again without a change keeps the token and sends nothing
            apply.reset_mock()
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _section(screenshot=True, foreground_app=True)
            )
            assert entry.options["screen_token"] == token
            apply.assert_not_awaited()
            await hass.async_block_till_done()

            # off: the token is gone from the entry
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _section(screenshot=False, foreground_app=True)
            )
            assert "screen_token" not in entry.options
            apply.assert_awaited_once()
    finally:
        _stop(patchers)


async def test_options_need_the_new_app_to_switch_on(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, {**TV33, "version": "0.32.0"})
    apply = AsyncMock(return_value=None)
    try:
        with patch("custom_components.pipup.config_flow.PiPupOptionsFlow._apply_grants", apply):
            for values in ({"screenshot": True}, {"foreground_app": True}):
                result = await hass.config_entries.options.async_init(entry.entry_id)
                result = await hass.config_entries.options.async_configure(
                    result["flow_id"], _section(**values)
                )
                assert result["errors"] == {"base": "foreground_screen_needs_app"}
            apply.assert_not_awaited()
            assert "screen_token" not in entry.options
    finally:
        _stop(patchers)


async def test_apply_grants_sends_the_token_and_keeps_the_service(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, dict(TV33), {"click_packages": PLEX})
    adb = MagicMock()
    adb.__aenter__ = AsyncMock(return_value=adb)
    adb.__aexit__ = AsyncMock(return_value=None)
    adb.connect = AsyncMock()
    for name in ("set_media_access", "set_click_packages", "set_usage_access", "set_screen_token"):
        setattr(adb, name, AsyncMock())
    try:
        with patch("custom_components.pipup.config_flow.PiPupAdb", return_value=adb):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            flow = hass.config_entries.options._progress[result["flow_id"]]

            # on, next to a click list
            options = {**entry.options, "foreground_app": True, **SCREEN_ON}
            assert await flow._apply_grants(options, 2.0) is None
            adb.set_usage_access.assert_awaited_once_with(True)
            adb.set_screen_token.assert_awaited_once_with(TOKEN, keep_service=True)
            adb.set_click_packages.assert_not_awaited()  # unchanged

            # the click list emptied while the screenshot goes on: the service stays
            adb.set_screen_token.reset_mock()
            options = {**entry.options, "click_packages": "", **SCREEN_ON}
            assert await flow._apply_grants(options, 2.0) is None
            adb.set_click_packages.assert_awaited_once_with([], keep_service=True)
            adb.set_screen_token.assert_awaited_once_with(TOKEN, keep_service=False)
    finally:
        _stop(patchers)


async def test_apply_grants_switches_the_screenshot_off(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, dict(TV33), {"foreground_app": True, **SCREEN_ON})
    adb = MagicMock()
    adb.__aenter__ = AsyncMock(return_value=adb)
    adb.__aexit__ = AsyncMock(return_value=None)
    adb.connect = AsyncMock()
    for name in ("set_media_access", "set_click_packages", "set_usage_access", "set_screen_token"):
        setattr(adb, name, AsyncMock())
    try:
        with patch("custom_components.pipup.config_flow.PiPupAdb", return_value=adb):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            flow = hass.config_entries.options._progress[result["flow_id"]]
            options = {"foreground_app": False, "screenshot": False}
            assert await flow._apply_grants(options, 2.0) is None
            adb.set_usage_access.assert_awaited_once_with(False)
            adb.set_screen_token.assert_awaited_once_with("", keep_service=False)
    finally:
        _stop(patchers)


def test_new_texts_are_translated() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "pipup"
    for path in [root / "strings.json", *sorted((root / "translations").glob("*.json"))]:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "foreground_screen_needs_app" in data["options"]["error"], path.name
        section = data["options"]["step"]["init"]["sections"]["advanced_install"]
        for part in ("data", "data_description"):
            assert {"foreground_app", "screenshot"} <= set(section[part]), (path.name, part)
        assert "screen" in data["entity"]["camera"], path.name
