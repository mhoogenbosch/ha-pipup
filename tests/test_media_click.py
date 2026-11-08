"""PiPup #83 (app >= 0.32.0): media session, seek, click - client, entity, services, ADB, options."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.media_player import MediaPlayerEntityFeature as F, MediaPlayerState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pipup.adb import AdbError
from custom_components.pipup.api import PiPupClient, PiPupError, PiPupUnsupportedError
from custom_components.pipup.config_flow import parse_click_packages
from custom_components.pipup.const import DOMAIN
from custom_components.pipup.media_player import PiPupMediaPlayer
from custom_components.pipup.services import CLICK_SCHEMA, LAUNCH_APP_SCHEMA, SEEK_SCHEMA

from .test_adb import APK, STATE, _adb_session, _fake_device, _section, _setup

BASE_URL = "http://192.168.1.21:7979"
PLEX = "com.plexapp.android"
ACCESS = "nl.rogro82.pipup/nl.rogro82.pipup.PiPupAccessibilityService"
CONTROL = "nl.rogro82.pipup/nl.rogro82.pipup.PiPupControlService"
LISTENER = "nl.rogro82.pipup/nl.rogro82.pipup.MediaListenerService"
SESSION = {
    "access": True, "package": PLEX, "state": "playing", "positionMs": 2472400,
    "positionUpdatedAt": "2026-11-05T16:20:00.000Z", "durationMs": 5400000, "speed": 1.0,
    "title": "Bill & Ted's Excellent Adventure", "artist": None, "album": None, "canSeek": True,
}


# ------------------------------------------------------------------ client


@pytest.fixture
async def client(hass: HomeAssistant, aioclient_mock) -> PiPupClient:
    # after aioclient_mock: the session it hands out is the mocked one
    return PiPupClient(async_get_clientsession(hass), "192.168.1.21", 7979)


async def test_media_read(client, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE_URL}/media", json=SESSION)
    assert (await client.media())["title"] == SESSION["title"]


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [
        (501, {"ok": False, "message": "media session access not granted"}, "not granted"),
        (400, "invalid request: unknown uri: /media", "needs >= 0.32.0"),  # older app
    ],
)
async def test_media_unsupported(client, aioclient_mock, status, body, match) -> None:
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
    aioclient_mock.get(f"{BASE_URL}/media", status=status, **kwargs)
    with pytest.raises(PiPupUnsupportedError, match=match):
        await client.media()


async def test_seek_params_and_errors(client, aioclient_mock) -> None:
    aioclient_mock.post(f"{BASE_URL}/media/seek", json={"ok": True, "message": "seeked"})
    await client.seek(ms=12500)
    await client.seek(by=-10000, package=PLEX)
    queries = [dict(call[1].query) for call in aioclient_mock.mock_calls]
    assert queries == [{"ms": "12500"}, {"by": "-10000", "package": PLEX}]
    with pytest.raises(ValueError):
        await client.seek(ms=1, by=1)
    with pytest.raises(ValueError):
        await client.seek()

    aioclient_mock.clear_requests()
    aioclient_mock.post(
        f"{BASE_URL}/media/seek", status=409,
        json={"ok": False, "message": "this app does not allow seeking"},
    )
    with pytest.raises(PiPupError, match=r"\(409\): this app does not allow seeking"):
        await client.seek(ms=0)


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, PiPupError), (404, PiPupError), (501, PiPupUnsupportedError)],
)
async def test_click_errors(client, aioclient_mock, status, error) -> None:
    aioclient_mock.post(f"{BASE_URL}/click", status=status, json={"ok": False, "message": "nope"})
    with pytest.raises(error, match="nope"):
        await client.click(PLEX, "action-button-watch", 5)
    assert dict(aioclient_mock.mock_calls[0][1].query) == {
        "package": PLEX, "id": "action-button-watch", "wait": "5",
    }


async def test_launch_with_click(client, aioclient_mock) -> None:
    aioclient_mock.post(f"{BASE_URL}/launch", json={"ok": True, "clicked": True})
    await client.launch(uri="plex://x", click="action-button-watch")
    await client.launch(package=PLEX, click="action-button-watch", wait=3)
    await client.launch(package=PLEX)  # no click: the call as before
    queries = [dict(call[1].query) for call in aioclient_mock.mock_calls]
    assert queries == [
        {"uri": "plex://x", "click": "action-button-watch"},
        {"package": PLEX, "click": "action-button-watch", "wait": "3"},
        {"package": PLEX},
    ]


# ------------------------------------------------------------------ media player


def _player(data: dict, online: bool = True) -> PiPupMediaPlayer:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.online = online
    coordinator.client.seek = AsyncMock(return_value={"ok": True})
    coordinator.async_refresh_soon = AsyncMock()
    player = PiPupMediaPlayer.__new__(PiPupMediaPlayer)
    player.coordinator = coordinator
    player.entity_id = "media_player.tv"
    player._apps = [{"package": PLEX, "label": "Plex", "tv": True}]
    return player


TV = {
    "screenOn": True,
    "power": {"canSleep": False},
    "control": {"launch": True, "keys": ["play_pause"], "lastLaunched": None},
    "volume": {"level": 40, "muted": False, "fixed": False, "max": 15},
}


def test_player_shows_the_session() -> None:
    player = _player({**TV, "media": SESSION})
    assert player.state == MediaPlayerState.PLAYING
    assert player.media_title == SESSION["title"]
    assert player.media_duration == 5400
    assert player.media_position == 2472
    assert player.media_position_updated_at == datetime(2026, 11, 5, 16, 20, tzinfo=timezone.utc)
    assert player.app_id == PLEX and player.app_name == "Plex"
    assert player.supported_features & F.SEEK
    assert player.extra_state_attributes["media_access"] == "granted"

    for app_state, expected in (
        ("paused", MediaPlayerState.PAUSED),
        ("buffering", MediaPlayerState.BUFFERING),
        ("stopped", MediaPlayerState.IDLE),
    ):
        assert _player({**TV, "media": {**SESSION, "state": app_state}}).state == expected
    # the screen decides first
    assert _player({**TV, "screenOn": False, "media": SESSION}).state == MediaPlayerState.OFF
    assert _player({**TV, "media": SESSION}, online=False).state == MediaPlayerState.OFF
    # an app that refuses seeking: no seek bar
    assert not _player({**TV, "media": {**SESSION, "canSeek": False}}).supported_features & F.SEEK


def test_player_without_session_or_access() -> None:
    idle = _player({**TV, "media": {"access": True, "package": None}})
    assert idle.state == MediaPlayerState.ON
    assert idle.media_title is None and idle.media_position_updated_at is None
    assert idle.supported_features & F.SEEK  # access: seeking works once something plays

    for reason in ("not_granted", "low_ram"):
        player = _player({**TV, "media": {"access": False, "reason": reason}})
        assert player.state == MediaPlayerState.ON
        assert not player.supported_features & F.SEEK
        assert player.extra_state_attributes["media_access"] == reason

    # an app older than 0.32.0 reports no media block: exactly as before
    old = _player(dict(TV))
    assert old.state == MediaPlayerState.ON
    assert not old.supported_features & F.SEEK
    assert "media_access" not in old.extra_state_attributes
    assert "click_packages" not in old.extra_state_attributes
    assert old.app_id is None and old.media_duration is None


def test_position_kept_until_it_really_changes() -> None:
    """A re-read that only moved the clock keeps the stamp: no new state per poll."""
    player = _player({**TV, "media": SESSION})
    first = (player.media_position, player.media_position_updated_at)
    assert first == (2472, datetime(2026, 11, 5, 16, 20, tzinfo=timezone.utc))

    def reread(**changes):
        player.coordinator.data = {**TV, "media": {**SESSION, **changes}}
        return player.media_position, player.media_position_updated_at

    # 15 s later, 15 s further: the same position as far as the frontend is concerned
    assert reread(positionMs=2487400, positionUpdatedAt="2026-11-05T16:20:15.000Z") == first
    # a seek back 30 s: a new pair
    assert reread(positionMs=2457400, positionUpdatedAt="2026-11-05T16:20:15.000Z") == (
        2457, datetime(2026, 11, 5, 16, 20, 15, tzinfo=timezone.utc)
    )
    # paused: a new pair once, then re-reads at the same position keep it
    paused = reread(state="paused", positionMs=2460000, positionUpdatedAt="2026-11-05T16:20:18.000Z")
    assert paused[1] == datetime(2026, 11, 5, 16, 20, 18, tzinfo=timezone.utc)
    assert reread(state="paused", positionMs=2460000,
                  positionUpdatedAt="2026-11-05T16:21:00.000Z") == paused
    # nothing playing any more: no position
    assert reread(package=None) == (None, None)


def test_player_click_attributes() -> None:
    player = _player({**TV, "control": {**TV["control"], "click": True, "clickPackages": [PLEX]}})
    attrs = player.extra_state_attributes
    assert attrs["click_service"] is True and attrs["click_packages"] == [PLEX]


async def test_player_seek() -> None:
    player = _player({**TV, "media": SESSION})
    await player.async_media_seek(61.25)
    player.coordinator.client.seek.assert_awaited_once_with(ms=61250, package=PLEX)


# ------------------------------------------------------------------ services


def test_service_schemas() -> None:
    assert CLICK_SCHEMA({"package": PLEX, "id": "action-button-watch"})["wait"] == 0
    assert CLICK_SCHEMA({"package": PLEX, "id": f"{PLEX}:id/play_button", "wait": 15})
    for bad in ({"package": PLEX, "id": "a b"}, {"package": PLEX, "id": "x", "wait": 16},
                {"package": "rm -rf /", "id": "x"}, {"package": PLEX}):
        with pytest.raises(vol.Invalid):
            CLICK_SCHEMA(bad)
    assert LAUNCH_APP_SCHEMA({"package": PLEX, "click": "action-button-watch", "wait": 5})
    assert SEEK_SCHEMA({"offset": -10})["offset"] == -10
    with pytest.raises(vol.Invalid):
        SEEK_SCHEMA({"position": 10, "offset": 5})
    with pytest.raises(vol.Invalid):
        SEEK_SCHEMA({"position": -1})


NEW = {
    "app": "PiPup", "version": "0.32.0-beta.1", "id": "new", "name": "New TV", "visible": False,
    "screenOn": True, "power": {"canWake": True, "canSleep": True},
    "control": {"launch": True, "keys": ["play_pause"], "lastLaunched": None,
                "click": True, "clickPackages": [PLEX]},
    "volume": {"level": 40, "max": 15, "muted": False, "fixed": False},
    "media": SESSION,
}
# an app from before #83: no media block, no click fields
PREV = {
    **{k: v for k, v in NEW.items() if k != "media"},
    "version": "0.31.0", "id": "prev", "name": "Prev TV",
    "control": {k: v for k, v in NEW["control"].items() if k not in ("click", "clickPackages")},
}


@pytest.fixture
async def tvs(hass: HomeAssistant):
    states = {"192.168.1.21": NEW, "192.168.1.22": PREV}
    calls = {name: AsyncMock(return_value={"ok": True}) for name in ("launch", "click", "seek")}

    async def state(self):
        return dict(states[self.host])

    async def settings(self, **values):
        return {}

    async def apps(self):
        return [{"package": PLEX, "label": "Plex", "tv": True}]

    def bound(name):
        async def call(self, *args, **kwargs):
            return await calls[name](self.host, *args, **kwargs)
        return call

    entries = []
    for host, tv in states.items():
        entry = MockConfigEntry(domain=DOMAIN, title=tv["name"], unique_id=tv["id"],
                                data={"host": host, "port": 7979}, options={"update_source": "off"})
        entry.add_to_hass(hass)
        entries.append(entry)
    with patch.multiple(
        "custom_components.pipup.api.PiPupClient",
        state=state, settings=settings, apps=apps,
        launch=bound("launch"), click=bound("click"), seek=bound("seek"),
    ):
        # setting up the first sets up the domain, which loads the other entry too
        assert await hass.config_entries.async_setup(entries[0].entry_id)
        await hass.async_block_till_done()
        for entry in entries:
            entry.runtime_data.async_refresh_soon = AsyncMock()
        with patch(
            "custom_components.pipup.services.async_extract_config_entry_ids",
            AsyncMock(return_value=[e.entry_id for e in entries]),
        ):
            yield calls


async def test_click_service_gates_older_apps(hass: HomeAssistant, tvs) -> None:
    with pytest.raises(HomeAssistantError, match=r"Prev TV: click needs PiPup app 0\.32\.0 or newer \(this TV runs 0\.31\.0\)"):
        await hass.services.async_call(
            DOMAIN, "click", {"package": PLEX, "id": "action-button-watch", "wait": 4}, blocking=True
        )
    # the TV with 0.32.0-beta.1 counts as 0.32.0 and got the click
    tvs["click"].assert_awaited_once_with("192.168.1.21", PLEX, "action-button-watch", 4)


async def test_seek_service(hass: HomeAssistant, tvs) -> None:
    with pytest.raises(HomeAssistantError, match="Prev TV: seek needs"):
        await hass.services.async_call(DOMAIN, "seek", {"position": 12.5}, blocking=True)
    tvs["seek"].assert_awaited_with("192.168.1.21", ms=12500, package=None)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "seek", {"offset": -10, "package": PLEX}, blocking=True)
    tvs["seek"].assert_awaited_with("192.168.1.21", by=-10000, package=PLEX)
    with pytest.raises(HomeAssistantError, match="position or an offset"):
        await hass.services.async_call(DOMAIN, "seek", {}, blocking=True)


async def test_launch_app_click(hass: HomeAssistant, tvs) -> None:
    with pytest.raises(HomeAssistantError, match="Prev TV: launch_app with click needs"):
        await hass.services.async_call(
            DOMAIN, "launch_app", {"uri": "plex://x", "click": "action-button-watch"}, blocking=True
        )
    tvs["launch"].assert_awaited_once_with(
        "192.168.1.21", package=None, uri="plex://x", wake=True, click="action-button-watch", wait=None
    )
    # without click the older TV is fine, and nobody gets click/wait
    tvs["launch"].reset_mock()
    await hass.services.async_call(DOMAIN, "launch_app", {"package": PLEX}, blocking=True)
    assert tvs["launch"].await_count == 2
    for call in tvs["launch"].await_args_list:
        assert "click" not in call.kwargs


# ------------------------------------------------------------------ ADB


def _broadcast(packages: str) -> str:
    return (
        "am broadcast -f 0x20 -n nl.rogro82.pipup/.ControlConfigReceiver "
        f'-a nl.rogro82.pipup.SET_CLICK_PACKAGES --es packages "{packages}"'
    )


async def test_click_packages_switch_the_control_service(hass: HomeAssistant) -> None:
    other = "com.other/com.other.Service"
    device = _fake_device({
        "am broadcast": 'Broadcast completed: result=1, data="com.plexapp.android"',
        "settings get secure enabled_accessibility_services": f"{other}:{ACCESS}",
    })
    adb = await _adb_session(hass, device)
    await adb.set_click_packages([PLEX])
    assert device.calls[0] == _broadcast(PLEX)
    assert f"settings put secure enabled_accessibility_services {other}:{ACCESS}:{CONTROL}" in device.calls
    assert "settings put secure accessibility_enabled 1" in device.calls

    # emptied: only the control service goes (the short form counts as the same)
    device = _fake_device({
        "am broadcast": 'Broadcast completed: result=1, data=""',
        "settings get secure enabled_accessibility_services":
            f"{other}:nl.rogro82.pipup/.PiPupControlService:{ACCESS}",
    })
    adb = await _adb_session(hass, device)
    await adb.set_click_packages([])
    assert device.calls[0] == _broadcast("")
    assert f"settings put secure enabled_accessibility_services {other}:{ACCESS}" in device.calls

    # nothing else left: the setting is deleted rather than written empty
    device = _fake_device({
        "am broadcast": "Broadcast completed: result=1",
        "settings get secure enabled_accessibility_services": CONTROL,
    })
    adb = await _adb_session(hass, device)
    await adb.set_click_packages([])
    assert "settings delete secure enabled_accessibility_services" in device.calls


async def test_click_packages_need_the_new_app(hass: HomeAssistant) -> None:
    device = _fake_device({"am broadcast": "Broadcast completed: result=0"})
    adb = await _adb_session(hass, device)
    with pytest.raises(AdbError) as err:
        await adb.set_click_packages([PLEX])
    assert err.value.reason == "grant_failed"
    assert not any(c.startswith("settings put") for c in device.calls)


async def test_media_access(hass: HomeAssistant) -> None:
    device = _fake_device({})
    adb = await _adb_session(hass, device)
    await adb.set_media_access(True)
    await adb.set_media_access(False)
    assert device.calls == [
        f"cmd notification allow_listener {LISTENER}",
        f"cmd notification disallow_listener {LISTENER}",
    ]

    # no `cmd notification` on this Android: the setting is written directly
    device = _fake_device({
        "cmd notification": "Unknown command: notification",
        "settings get secure enabled_notification_listeners": "com.other/com.other.L",
    })
    adb = await _adb_session(hass, device)
    await adb.set_media_access(True)
    assert (
        f"settings put secure enabled_notification_listeners com.other/com.other.L:{LISTENER}"
        in device.calls
    )


async def test_install_restores_every_grant(hass: HomeAssistant) -> None:
    """A reinstall that switched both services and the listener off gets them back."""
    device = _fake_device({"pm install": "Success"})
    access_reads = iter([f"nl.rogro82.pipup/.PiPupAccessibilityService:{CONTROL}", "null"])
    listener_reads = iter([LISTENER, "null"])

    async def shell(command, **_):
        device.calls.append(command)
        if command == "settings get secure enabled_accessibility_services":
            return next(access_reads)
        if command == "settings get secure enabled_notification_listeners":
            return next(listener_reads)
        return "Success" if command.startswith("pm install") else ""

    device.shell.side_effect = shell
    adb = await _adb_session(hass, device)
    await adb.install(APK)
    assert f"settings put secure enabled_accessibility_services {ACCESS}:{CONTROL}" in device.calls
    assert f"cmd notification allow_listener {LISTENER}" in device.calls


async def test_install_leaves_what_survived(hass: HomeAssistant) -> None:
    device = _fake_device({
        "pm install": "Success",
        "settings get secure enabled_accessibility_services": CONTROL,
        "settings get secure enabled_notification_listeners": LISTENER,
    })
    adb = await _adb_session(hass, device)
    await adb.install(APK)
    assert not any(c.startswith("settings put") for c in device.calls)
    assert not any(c.startswith("cmd notification") for c in device.calls)


# ------------------------------------------------------------------ options


def test_parse_click_packages() -> None:
    assert parse_click_packages(" com.plexapp.android, com.a.b\ncom.plexapp.android ") == (
        [PLEX, "com.a.b"], []
    )
    assert parse_click_packages("") == ([], [])
    assert parse_click_packages("plex, com.x;rm") == ([], ["plex", "com.x;rm"])


TV32 = {**STATE, "version": "0.32.0"}


async def test_options_apply_grants(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, dict(TV32))
    apply = AsyncMock(return_value=None)
    test = AsyncMock(return_value=None)
    try:
        with (
            patch("custom_components.pipup.config_flow.PiPupOptionsFlow._apply_grants", apply),
            patch("custom_components.pipup.config_flow.PiPupOptionsFlow._test_adb", test),
        ):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                _section(media_access=True, click_packages=f"{PLEX}, com.a.b"),
            )
            assert result["type"] == "create_entry"
            apply.assert_awaited_once()
            assert entry.options["media_access"] is True
            assert entry.options["click_packages"] == f"{PLEX},com.a.b"

            # saved again unchanged: no ADB at all
            apply.reset_mock()
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"default_duration": 9,
                 **_section(media_access=True, click_packages=f"{PLEX},com.a.b")},
            )
            assert result["type"] == "create_entry"
            apply.assert_not_awaited()
            test.assert_not_awaited()
    finally:
        for p in patchers:
            p.stop()


async def test_options_grants_errors(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, dict(TV32))
    apply = AsyncMock(side_effect=["adb_not_authorized", None, "adb_grant_failed"])
    try:
        with patch("custom_components.pipup.config_flow.PiPupOptionsFlow._apply_grants", apply):
            # an invalid package: shown, nothing applied
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _section(click_packages="plex")
            )
            assert result["errors"] == {"base": "invalid_click_packages"}
            apply.assert_not_awaited()

            # the TV still has to allow debugging: the accept step applies them
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _section(media_access=True)
            )
            assert result["step_id"] == "adb_authorize"
            result = await hass.config_entries.options.async_configure(result["flow_id"], {})
            assert result["type"] == "create_entry"
            assert apply.await_count == 2
            assert apply.await_args_list[1].args[1] == 30.0  # the long accept timeout
            assert entry.options["media_access"] is True

            # the TV refuses: shown, not saved
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _section(media_access=False)
            )
            assert result["errors"] == {"base": "adb_grant_failed"}
            assert entry.options["media_access"] is True
    finally:
        for p in patchers:
            p.stop()


async def test_options_grants_need_new_app(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, dict(STATE))  # 0.29.2
    apply = AsyncMock(return_value=None)
    try:
        with patch("custom_components.pipup.config_flow.PiPupOptionsFlow._apply_grants", apply):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _section(media_access=True)
            )
            assert result["errors"] == {"base": "media_click_needs_app"}
            apply.assert_not_awaited()
    finally:
        for p in patchers:
            p.stop()


async def test_apply_grants_sends_only_changes(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(
        hass, dict(TV32), {"media_access": True, "click_packages": PLEX}
    )
    adb = MagicMock()
    adb.__aenter__ = AsyncMock(return_value=adb)
    adb.__aexit__ = AsyncMock(return_value=None)
    adb.connect = AsyncMock()
    adb.set_media_access = AsyncMock()
    adb.set_click_packages = AsyncMock(side_effect=AdbError("grant_failed", "result=0"))
    try:
        with patch("custom_components.pipup.config_flow.PiPupAdb", return_value=adb):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            flow = hass.config_entries.options._progress[result["flow_id"]]
            options = {**entry.options, "media_access": True, "click_packages": f"{PLEX},com.a.b"}
            assert await flow._apply_grants(options, 2.0) == "adb_grant_failed"
            adb.set_media_access.assert_not_awaited()  # unchanged
            adb.set_click_packages.assert_awaited_once_with([PLEX, "com.a.b"])

            adb.connect.side_effect = AdbError("cannot_connect")
            assert await flow._apply_grants(options, 2.0) == "adb_cannot_connect"
    finally:
        for p in patchers:
            p.stop()


def test_new_errors_are_translated() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "pipup"
    for path in [root / "strings.json", *sorted((root / "translations").glob("*.json"))]:
        data = json.loads(path.read_text(encoding="utf-8"))
        errors = data["options"]["error"]
        for key in ("invalid_click_packages", "media_click_needs_app", "adb_grant_failed"):
            assert key in errors, (path.name, key)
        section = data["options"]["step"]["init"]["sections"]["advanced_install"]["data"]
        assert {"media_access", "click_packages"} <= set(section), path.name
        assert {"click", "seek"} <= set(data["services"]), path.name
        assert {"click", "wait"} <= set(data["services"]["launch_app"]["fields"]), path.name
