"""2.1.0b2 (PiPup #95): the key path keeps its ADB connection open, and a wake that
leaves the screen dark gets KEYCODE_WAKEUP once more."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.pipup.adb import (
    _KEY_CONNECTIONS_KEY,
    KEY_IDLE_CLOSE,
    KEY_SHELL_TIMEOUT,
    AdbError,
    async_adb_close_dialogs,
    async_adb_key,
    async_close_key_connection,
)
from custom_components.pipup.api import PiPupError
from custom_components.pipup.const import DOMAIN

from .test_adb import STATE, _setup
from .test_tv_keys_95 import _coordinator

HOST = "192.168.1.21"
ADB = {"install_method": "adb"}


def _device(shell=None) -> MagicMock:
    device = MagicMock()
    device.connect = AsyncMock()
    device.shell = shell or AsyncMock(return_value="")
    device.close = AsyncMock()
    return device


def _pool(hass: HomeAssistant) -> dict:
    return hass.data.get(DOMAIN, {}).get(_KEY_CONNECTIONS_KEY, {})


# ------------------------------------------------------------------ reuse


async def test_two_keys_share_one_connection(hass: HomeAssistant) -> None:
    device = _device()
    with patch("custom_components.pipup.adb.PiPupAdb", return_value=device) as cls:
        await async_adb_key(hass, HOST, ADB, "KEYCODE_DPAD_UP")
        await async_adb_key(hass, HOST, ADB, "KEYCODE_DPAD_RIGHT")
        await async_adb_close_dialogs(hass, HOST, ADB)
    assert cls.call_count == 1
    device.connect.assert_awaited_once()
    assert [c.args[0] for c in device.shell.await_args_list] == [
        "input keyevent KEYCODE_DPAD_UP",
        "input keyevent KEYCODE_DPAD_RIGHT",
        "am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS",
    ]
    assert all(c.kwargs == {"timeout": KEY_SHELL_TIMEOUT} for c in device.shell.await_args_list)
    device.close.assert_not_awaited()
    await async_close_key_connection(hass, HOST)
    device.close.assert_awaited_once()


async def test_each_tv_has_its_own_connection(hass: HomeAssistant) -> None:
    first, second = _device(), _device()
    with patch("custom_components.pipup.adb.PiPupAdb", side_effect=[first, second]):
        await async_adb_key(hass, HOST, ADB, "KEYCODE_HOME")
        await async_adb_key(hass, "192.168.1.22", ADB, "KEYCODE_HOME")
    first.connect.assert_awaited_once()
    second.connect.assert_awaited_once()
    await async_close_key_connection(hass, HOST)
    await async_close_key_connection(hass, "192.168.1.22")


async def test_changed_adb_options_reconnect(hass: HomeAssistant) -> None:
    first, second = _device(), _device()
    with patch("custom_components.pipup.adb.PiPupAdb", side_effect=[first, second]) as cls:
        await async_adb_key(hass, HOST, ADB, "KEYCODE_HOME")
        await async_adb_key(hass, HOST, {**ADB, "adb_port": 5556}, "KEYCODE_HOME")
    assert cls.call_count == 2
    first.close.assert_awaited_once()
    await async_close_key_connection(hass, HOST)
    second.close.assert_awaited_once()


# ------------------------------------------------------------------ idle close


async def test_connection_closes_after_idle(hass: HomeAssistant) -> None:
    device = _device()
    with patch("custom_components.pipup.adb.PiPupAdb", return_value=device):
        await async_adb_key(hass, HOST, ADB, "KEYCODE_HOME")
    device.close.assert_not_awaited()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=KEY_IDLE_CLOSE + 1))
    await hass.async_block_till_done()
    device.close.assert_awaited_once()
    assert _pool(hass)[HOST].adb is None


async def test_a_key_before_the_idle_timer_keeps_it_open(hass: HomeAssistant) -> None:
    device = _device()
    with patch("custom_components.pipup.adb.PiPupAdb", return_value=device) as cls:
        await async_adb_key(hass, HOST, ADB, "KEYCODE_HOME")
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=KEY_IDLE_CLOSE / 2))
        await hass.async_block_till_done()
        await async_adb_key(hass, HOST, ADB, "KEYCODE_BACK")
    assert cls.call_count == 1
    device.close.assert_not_awaited()
    await async_close_key_connection(hass, HOST)


# ------------------------------------------------------------------ dead connection


async def test_a_dead_kept_connection_reconnects_once(hass: HomeAssistant) -> None:
    gone = _device()
    gone.shell = AsyncMock(side_effect=["", AdbError("connection_lost", "EOF")])
    fresh = _device()
    with patch("custom_components.pipup.adb.PiPupAdb", side_effect=[gone, fresh]) as cls:
        await async_adb_key(hass, HOST, ADB, "KEYCODE_HOME")
        await async_adb_key(hass, HOST, ADB, "KEYCODE_BACK")
    assert cls.call_count == 2
    gone.close.assert_awaited_once()
    fresh.shell.assert_awaited_once_with("input keyevent KEYCODE_BACK", timeout=KEY_SHELL_TIMEOUT)
    await async_close_key_connection(hass, HOST)


async def test_a_fresh_connection_that_fails_is_not_retried(hass: HomeAssistant) -> None:
    device = _device(shell=AsyncMock(side_effect=AdbError("connection_lost", "EOF")))
    with patch("custom_components.pipup.adb.PiPupAdb", return_value=device) as cls:
        with pytest.raises(AdbError):
            await async_adb_key(hass, HOST, ADB, "KEYCODE_HOME")
    assert cls.call_count == 1
    device.close.assert_awaited_once()
    assert _pool(hass)[HOST].adb is None
    assert _pool(hass)[HOST].idle is None


async def test_a_failed_connect_keeps_nothing(hass: HomeAssistant) -> None:
    device = _device()
    device.connect = AsyncMock(side_effect=AdbError("cannot_connect", "timeout"))
    with patch("custom_components.pipup.adb.PiPupAdb", return_value=device):
        with pytest.raises(AdbError):
            await async_adb_key(hass, HOST, ADB, "KEYCODE_HOME")
    device.shell.assert_not_awaited()
    assert _pool(hass)[HOST].adb is None
    assert _pool(hass)[HOST].idle is None


async def test_dead_twice_raises(hass: HomeAssistant) -> None:
    gone = _device(shell=AsyncMock(side_effect=["", AdbError("connection_lost", "EOF")]))
    also_gone = _device(shell=AsyncMock(side_effect=AdbError("connection_lost", "EOF")))
    with patch("custom_components.pipup.adb.PiPupAdb", side_effect=[gone, also_gone]) as cls:
        await async_adb_key(hass, HOST, ADB, "KEYCODE_HOME")
        with pytest.raises(AdbError):
            await async_adb_key(hass, HOST, ADB, "KEYCODE_BACK")
    assert cls.call_count == 2
    assert _pool(hass)[HOST].adb is None


# ------------------------------------------------------------------ one at a time


async def test_concurrent_keys_take_turns(hass: HomeAssistant) -> None:
    running = 0
    most = 0

    async def shell(command: str, timeout: float) -> str:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return ""

    device = _device(shell=AsyncMock(side_effect=shell))
    with patch("custom_components.pipup.adb.PiPupAdb", return_value=device) as cls:
        await asyncio.gather(
            *(async_adb_key(hass, HOST, ADB, k) for k in
              ("KEYCODE_DPAD_UP", "KEYCODE_DPAD_DOWN", "KEYCODE_DPAD_LEFT", "KEYCODE_DPAD_RIGHT"))
        )
    assert most == 1
    assert cls.call_count == 1
    assert device.shell.await_count == 4
    await async_close_key_connection(hass, HOST)


# ------------------------------------------------------------------ unload


async def test_unload_closes_the_connection(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, dict(STATE), {"install_method": "adb"})
    try:
        device = _device()
        with patch("custom_components.pipup.adb.PiPupAdb", return_value=device):
            await async_adb_key(hass, "192.168.1.14", ADB, "KEYCODE_HOME")
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        device.close.assert_awaited_once()
        assert "192.168.1.14" not in _pool(hass)
    finally:
        for p in patchers:
            p.stop()


async def test_closing_without_a_connection_is_fine(hass: HomeAssistant) -> None:
    await async_close_key_connection(hass, "192.168.1.99")


# ------------------------------------------------------------------ wakeup retry


async def test_wakeup_sends_the_key_once_more_when_the_screen_stays_dark() -> None:
    coordinator = _coordinator({"screenOn": False}, ADB, keys=["play_pause", "wakeup"])
    coordinator.client.key = AsyncMock(return_value={"ok": True, "woke": False, "screenOn": False})
    coordinator.client.state = AsyncMock(return_value={"screenOn": False})
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        answer = await coordinator.async_send_key("wakeup")
    assert adb.await_count == 2
    assert all(c.args[3] == "KEYCODE_WAKEUP" for c in adb.await_args_list)
    assert answer["adb_wake_attempts"] == 2


async def test_wakeup_stops_after_one_key_when_the_screen_came_on() -> None:
    coordinator = _coordinator({"screenOn": False}, ADB, keys=["play_pause", "wakeup"])
    coordinator.client.key = AsyncMock(return_value={"ok": True, "woke": False, "screenOn": False})
    coordinator.client.state = AsyncMock(return_value={"screenOn": True})
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        answer = await coordinator.async_send_key("wakeup")
    assert adb.await_count == 1
    assert answer["adb_wake_attempts"] == 1


async def test_wakeup_retries_when_the_app_cannot_tell() -> None:
    """An app not answering yet (just woken) gets the second key as well."""
    coordinator = _coordinator({"screenOn": False}, ADB, keys=["play_pause", "wakeup"])
    coordinator.client.key = AsyncMock(return_value={"ok": True, "woke": False, "screenOn": False})
    coordinator.client.state = AsyncMock(side_effect=PiPupError("not reachable"))
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        answer = await coordinator.async_send_key("wakeup")
    assert adb.await_count == 2
    assert answer["woken_over_adb"] is True


async def test_turn_on_also_retries_the_adb_wake() -> None:
    coordinator = _coordinator({}, ADB)
    coordinator.client.power = AsyncMock(
        return_value={"ok": True, "reached": False, "screenOn": False}
    )
    coordinator.client.state = AsyncMock(return_value={"screenOn": False})
    adb = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        answer = await coordinator.async_screen_on()
    assert adb.await_count == 2
    assert answer["adb_wake_attempts"] == 2


async def test_a_failing_second_wake_key_is_reported() -> None:
    coordinator = _coordinator({"screenOn": False}, ADB, keys=["play_pause", "wakeup"])
    coordinator.client.key = AsyncMock(return_value={"ok": True, "woke": False, "screenOn": False})
    coordinator.client.state = AsyncMock(return_value={"screenOn": False})
    adb = AsyncMock(side_effect=[None, AdbError("connection_lost", "EOF")])
    with patch("custom_components.pipup.coordinator.async_adb_key", adb):
        with pytest.raises(PiPupError, match="wakeup over ADB failed"):
            await coordinator.async_send_key("wakeup")
