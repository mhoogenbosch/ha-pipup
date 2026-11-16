"""1.32.0b3 (PiPup #83): method ok (OK over ADB below Android 13), waking over ADB, beta APKs."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.pipup.adb import (
    AdbError,
    KEYCODE_DPAD_CENTER,
    KEYCODE_WAKEUP,
    adb_ready,
    async_adb_key,
    component_list,
)
from custom_components.pipup.const import DOMAIN
from custom_components.pipup.coordinator import PiPupCoordinator
from custom_components.pipup.services import CLICK_SCHEMA, LAUNCH_APP_SCHEMA
from custom_components.pipup.update import (
    _latest_release,
    _latest_release_tag,
    _pick_release,
    _source_urls,
)

from .test_media_click import PLEX, tvs  # noqa: F401 - fixture

ACCESS = "nl.rogro82.pipup/nl.rogro82.pipup.PiPupAccessibilityService"
FOCUSED = {
    "ok": True, "message": "focused action-button-watch; press OK", "method": "ok",
    "focused": True, "pressed": False, "key": "KEYCODE_DPAD_CENTER",
}


# ------------------------------------------------------------------ null entries


def test_component_list_drops_null_entries() -> None:
    """The readme's old append kept `null`: null:pkg/... (PiPup #83)."""
    assert component_list("null") == []
    assert component_list("") == []
    assert component_list(f"null:{ACCESS}") == [ACCESS]
    assert component_list(f"{ACCESS}::null: ") == [ACCESS]


# ------------------------------------------------------------------ adb_ready


@pytest.mark.parametrize(
    ("options", "ready"),
    [
        ({}, False),
        ({"install_method": "app"}, False),
        ({"install_method": "adb"}, True),
        ({"click_packages": PLEX}, True),
        ({"media_access": True}, True),
        ({"click_packages": ""}, False),
    ],
)
def test_adb_ready(options, ready) -> None:
    assert adb_ready(options, {"update": {"silent": True}}) is ready


def test_adb_ready_auto_follows_the_tv() -> None:
    # Auto = ADB on a TV whose own update is not silent (Android < 12)
    assert adb_ready({"install_method": "auto"}, {"update": {"silent": False}}) is True
    assert adb_ready({"install_method": "auto"}, {"update": {"silent": True}}) is False


async def test_adb_key_only_sends_known_keys(hass: HomeAssistant) -> None:
    with pytest.raises(ValueError):
        await async_adb_key(hass, "192.168.1.21", {}, "KEYCODE_HOME; reboot")
    adb = MagicMock()
    adb.connect = AsyncMock()
    adb.shell = AsyncMock(return_value="")
    adb.__aenter__ = AsyncMock(return_value=adb)
    adb.__aexit__ = AsyncMock(return_value=None)
    with patch("custom_components.pipup.adb.PiPupAdb", return_value=adb):
        await async_adb_key(hass, "192.168.1.21", {}, KEYCODE_WAKEUP)
    adb.shell.assert_awaited_once_with("input keyevent KEYCODE_WAKEUP")


# ------------------------------------------------------------------ method ok


def test_schemas_take_ok() -> None:
    assert CLICK_SCHEMA({"package": PLEX, "id": "x", "method": "OK"})["method"] == "ok"
    assert LAUNCH_APP_SCHEMA({"uri": "plex://x", "click": "x", "method": "ok"})["method"] == "ok"


def _only_new(hass: HomeAssistant):
    new = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.title == "New TV")
    return patch(
        "custom_components.pipup.services.async_extract_config_entry_ids",
        AsyncMock(return_value=[new.entry_id]),
    )


async def test_ok_presses_over_adb_below_android_13(hass: HomeAssistant, tvs) -> None:  # noqa: F811
    tvs["click"].return_value = dict(FOCUSED)
    key = AsyncMock()
    with _only_new(hass), patch("custom_components.pipup.services.adb_ready", return_value=True), \
            patch("custom_components.pipup.services.async_adb_key", key):
        result = await hass.services.async_call(
            DOMAIN, "click", {"package": PLEX, "id": "action-button-watch", "method": "ok"},
            blocking=True, return_response=True,
        )
    key.assert_awaited_once()
    assert key.await_args.args[1:] == ("192.168.1.21", key.await_args.args[2], KEYCODE_DPAD_CENTER)
    answer = result["tvs"]["New TV"]
    assert answer["pressed"] is True and answer["pressed_over_adb"] is True
    assert answer["changed"] is None


async def test_ok_without_adb_says_so(hass: HomeAssistant, tvs) -> None:  # noqa: F811
    tvs["launch"].return_value = {**FOCUSED, "launched": True, "clicked": True}
    key = AsyncMock()
    with _only_new(hass), patch("custom_components.pipup.services.adb_ready", return_value=False), \
            patch("custom_components.pipup.services.async_adb_key", key):
        with pytest.raises(HomeAssistantError, match="only ADB can press OK"):
            await hass.services.async_call(
                DOMAIN, "launch_app", {"uri": "plex://x", "click": "action-button-watch", "method": "ok"},
                blocking=True,
            )
    key.assert_not_awaited()


async def test_ok_adb_failure_is_reported(hass: HomeAssistant, tvs) -> None:  # noqa: F811
    tvs["click"].return_value = dict(FOCUSED)
    with _only_new(hass), patch("custom_components.pipup.services.adb_ready", return_value=True), \
            patch("custom_components.pipup.services.async_adb_key",
                  AsyncMock(side_effect=AdbError("cannot_connect"))):
        with pytest.raises(HomeAssistantError, match="pressing OK over ADB failed"):
            await hass.services.async_call(
                DOMAIN, "click", {"package": PLEX, "id": "x", "method": "ok"}, blocking=True
            )


@pytest.mark.parametrize(
    "answer",
    [
        # Android 13+: the app pressed OK itself
        {"ok": True, "method": "ok", "focused": True, "pressed": True, "changed": True},
        # a tap or click answer is left alone
        {"ok": True, "method": "tap", "changed": False},
        # a key the integration does not send
        {**FOCUSED, "key": "KEYCODE_HOME"},
    ],
)
async def test_ok_leaves_other_answers_alone(hass: HomeAssistant, tvs, answer) -> None:  # noqa: F811
    tvs["click"].return_value = answer
    key = AsyncMock()
    with _only_new(hass), patch("custom_components.pipup.services.adb_ready", return_value=True), \
            patch("custom_components.pipup.services.async_adb_key", key):
        result = await hass.services.async_call(
            DOMAIN, "click", {"package": PLEX, "id": "x", "method": "ok"},
            blocking=True, return_response=True,
        )
    key.assert_not_awaited()
    assert result["tvs"]["New TV"] == answer


# ------------------------------------------------------------------ waking


async def test_launch_wakes_a_dark_screen_first(hass: HomeAssistant, tvs) -> None:  # noqa: F811
    new = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.title == "New TV")
    coordinator = new.runtime_data
    coordinator.data = {**coordinator.data, "screenOn": False}
    order = []
    coordinator.async_screen_on = AsyncMock(side_effect=lambda: order.append("wake") or {})
    tvs["launch"].side_effect = lambda *a, **k: order.append("launch") or {"ok": True}
    with _only_new(hass):
        await hass.services.async_call(DOMAIN, "launch_app", {"package": PLEX}, blocking=True)
        assert order == ["wake", "launch"]
        # wake: false leaves a dark TV dark
        order.clear()
        await hass.services.async_call(
            DOMAIN, "launch_app", {"package": PLEX, "wake": False}, blocking=True
        )
        assert order == ["launch"]


def _coordinator(power_answer: dict, options: dict) -> MagicMock:
    coordinator = MagicMock()
    coordinator.client.power = AsyncMock(return_value=power_answer)
    coordinator.client.host = "192.168.1.21"
    coordinator.config_entry.options = options
    coordinator.config_entry.title = "TCL"
    coordinator.data = {"update": {"silent": False}}
    return coordinator


@pytest.mark.parametrize(
    ("power", "options", "woken"),
    [
        # app >= 0.32.0-beta.3, screen stayed off, ADB set up: KEYCODE_WAKEUP
        ({"ok": True, "reached": False, "screenOn": False}, {"click_packages": PLEX}, True),
        # same, no ADB setup: only a warning, never an "Allow debugging?" prompt
        ({"ok": True, "reached": False, "screenOn": False}, {}, False),
        # the screen came on
        ({"ok": True, "reached": True, "screenOn": True}, {"click_packages": PLEX}, False),
        # an older app: no `reached`, screenOn may lag a poll behind
        ({"ok": True, "screenOn": False}, {"click_packages": PLEX}, False),
    ],
)
async def test_screen_on_falls_back_to_adb(hass: HomeAssistant, power, options, woken) -> None:
    coordinator = _coordinator(power, options)
    coordinator.hass = hass
    key = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", key):
        answer = await PiPupCoordinator.async_screen_on(coordinator)
    assert key.await_count == (1 if woken else 0)
    if woken:
        assert key.await_args.args[3] == KEYCODE_WAKEUP
        assert answer["woken_over_adb"] is True


async def test_screen_on_adb_failure(hass: HomeAssistant) -> None:
    from custom_components.pipup.api import PiPupError

    coordinator = _coordinator({"ok": True, "reached": False, "screenOn": False}, {"install_method": "adb"})
    coordinator.hass = hass
    with patch("custom_components.pipup.coordinator.async_adb_key",
               AsyncMock(side_effect=AdbError("not_authorized"))):
        with pytest.raises(PiPupError, match="waking it over ADB failed"):
            await PiPupCoordinator.async_screen_on(coordinator)


# ------------------------------------------------------------------ beta APKs


def _rel(tag: str, prerelease: bool = False, draft: bool = False) -> dict:
    return {"tag_name": f"v{tag}", "prerelease": prerelease, "draft": draft, "assets": []}


def test_pick_release() -> None:
    releases = [
        _rel("0.33.0", draft=True),
        _rel("0.31.1"),  # a hotfix tagged after the beta
        _rel("0.32.0-beta.3", prerelease=True),
        _rel("0.32.0-beta.2", prerelease=True),
        _rel("0.31.0"),
    ]
    assert _pick_release(releases, beta=False)["tag_name"] == "v0.31.1"
    assert _pick_release(releases, beta=True)["tag_name"] == "v0.32.0-beta.3"
    # the release wins over its own betas
    assert _pick_release([_rel("0.32.0-beta.3", True), _rel("0.32.0")], beta=True)["tag_name"] == "v0.32.0"
    assert _pick_release([_rel("0.32.0-beta.1", True)], beta=False) == {}
    assert _pick_release([], beta=True) == {}


def test_beta_source_url() -> None:
    assert _source_urls("github:mhoogenbosch/PiPup")[0].endswith("/releases/latest")
    assert _source_urls("github:mhoogenbosch/PiPup", beta=True)[0].endswith("/releases?per_page=20")
    # a folder's releases.json already is the list
    assert _source_urls("http://mirror/pipup", beta=True)[0] == "http://mirror/pipup/releases.json"


async def test_beta_and_release_cached_apart(hass: HomeAssistant, aioclient_mock) -> None:
    source = "github:mhoogenbosch/PiPup"
    aioclient_mock.get(
        "https://api.github.com/repos/mhoogenbosch/PiPup/releases/latest", json=_rel("0.31.0")
    )
    aioclient_mock.get(
        "https://api.github.com/repos/mhoogenbosch/PiPup/releases?per_page=20",
        json=[_rel("0.32.0-beta.3", True), _rel("0.31.0")],
    )
    assert await _latest_release_tag(hass, source) == "0.31.0"
    assert await _latest_release_tag(hass, source, beta=True) == "0.32.0-beta.3"
    # both cached: no new requests
    calls = aioclient_mock.call_count
    assert await _latest_release_tag(hass, source) == "0.31.0"
    assert (await _latest_release(hass, source, beta=True))["tag_name"] == "v0.32.0-beta.3"
    assert aioclient_mock.call_count == calls


@pytest.mark.parametrize(
    ("options", "beta"),
    [
        ({"beta_updates": True, "install_method": "adb"}, True),
        ({"beta_updates": True, "install_method": "app"}, False),
        ({"beta_updates": False, "install_method": "adb"}, False),
        ({"install_method": "adb"}, False),
    ],
)
def test_update_entity_beta_only_with_adb(options, beta) -> None:
    from custom_components.pipup.update import PiPupUpdateEntity

    entity = MagicMock()
    entity.coordinator.config_entry.options = options
    entity.coordinator.data = {"update": {"silent": True}}
    assert PiPupUpdateEntity._beta.fget(entity) is beta


def test_new_strings_are_translated() -> None:
    import json
    from pathlib import Path

    base = Path(__file__).parent.parent / "custom_components" / "pipup"
    for name in ("strings.json", "translations/en.json", "translations/nl.json"):
        data = json.loads((base / name).read_text())
        advanced = data["options"]["step"]["init"]["sections"]["advanced_install"]
        assert "beta_updates" in advanced["data"]
        assert "beta_updates" in advanced["data_description"]
        assert "beta_needs_adb" in data["options"]["error"]
        assert "ok" in data["selector"]["method"]["options"]
