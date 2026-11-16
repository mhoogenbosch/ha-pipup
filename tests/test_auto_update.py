"""Automatic updates: the quiet window, one attempt a night, records and repairs."""
from __future__ import annotations

import asyncio
from datetime import datetime, time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.pipup.adb import REASONS, AdbError
from custom_components.pipup.auto_update import parse_time, window_start
from custom_components.pipup.const import DOMAIN

from .test_adb import APK, STATE, _fake_adb, _setup

AUTO = {"install_method": "auto", "auto_update": True}


@pytest.fixture(autouse=True)
def no_auto_timer():
    """The tests call the tick themselves; the real 10-minute timer would also fire
    when freezer moves the clock, and reach for a real TV."""
    with patch(
        "custom_components.pipup.update.async_track_time_interval",
        return_value=lambda: None,
    ):
        yield


def _local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=dt_util.get_default_time_zone())


@pytest.mark.parametrize(
    ("start", "end", "now", "expected"),
    [
        ("03:00:00", "05:00:00", (26, 3, 10), (26, 3)),
        ("03:00:00", "05:00:00", (26, 5, 0), None),  # end is exclusive
        ("03:00:00", "05:00:00", (26, 2, 59), None),
        ("23:00:00", "02:00:00", (26, 23, 30), (26, 23)),
        ("23:00:00", "02:00:00", (27, 1, 30), (26, 23)),  # same night after midnight
        ("23:00:00", "02:00:00", (27, 2, 30), None),
        ("04:00:00", "04:00:00", (26, 3, 0), (25, 4)),  # all day
    ],
)
def test_window_start(start, end, now, expected) -> None:
    """The window's start names the night; None outside it."""
    result = window_start(_local(*now), parse_time(start, ""), parse_time(end, ""))
    assert result == (_local(*expected) if expected else None)


def test_parse_time_falls_back() -> None:
    """A broken stored value uses the default instead of crashing the tick."""
    assert parse_time("nonsense", "03:00:00") == time(3, 0)
    assert parse_time(None, "05:00:00") == time(5, 0)


async def _entity(hass: HomeAssistant):
    entity_id = next(s.entity_id for s in hass.states.async_all("update"))
    return entity_id, hass.data["entity_components"]["update"].get_entity(entity_id)


async def _at(freezer, day: int, hour: int, minute: int = 0) -> None:
    freezer.move_to(dt_util.as_utc(_local(day, hour, minute)))


async def _teardown(hass: HomeAssistant, entry, patchers) -> None:
    """Unload before the patches go: freezer moved the clock hours ahead, so the
    coordinator's overdue refresh would otherwise reach for the real TV."""
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    for p in patchers:
        p.stop()


async def _tick(entity, adb):
    with patch("custom_components.pipup.update.PiPupAdb", return_value=adb):
        await entity._async_auto_tick()


async def test_installs_once_a_night(hass: HomeAssistant, freezer) -> None:
    """In the window: install, record, logbook; the same night never again."""
    tv = dict(STATE)
    entry, patchers = await _setup(hass, tv, AUTO)
    adb = _fake_adb()

    async def installed(_apk):
        tv["version"] = "0.30.0"

    adb.install.side_effect = installed
    try:
        entity_id, entity = await _entity(hass)
        await _at(freezer, 26, 2, 50)
        await _tick(entity, adb)
        adb.install.assert_not_awaited()  # outside the window

        await _at(freezer, 26, 3, 10)
        with patch(
            "homeassistant.components.logbook.async_log_entry"
        ) as log_entry:
            hass.config.components.add("logbook")
            await _tick(entity, adb)
        adb.install.assert_awaited_once_with(APK)
        assert "0.29.2 → 0.30.0 (ADB)" in log_entry.call_args.args[2]
        attrs = hass.states.get(entity_id).attributes
        assert attrs["last_auto_update"]["result"] == "success"
        assert attrs["last_auto_update"]["to"] == "0.30.0"
        assert attrs["auto_update_failures"] == 0

        # a newer release the same night: waits for tomorrow
        tv["version"] = "0.29.2"
        await _at(freezer, 26, 4, 0)
        await _tick(entity, adb)
        assert adb.install.await_count == 1
    finally:
        await _teardown(hass, entry, patchers)


async def test_failure_retries_next_night_and_raises_repair(hass: HomeAssistant, freezer) -> None:
    """Failures count per night; a repair after three; a success clears it."""
    tv = dict(STATE)
    entry, patchers = await _setup(hass, tv, AUTO)
    adb = _fake_adb()
    adb.install.side_effect = AdbError("install_failed", "Failure [X]")
    issue_id = f"auto_update_failed_{entry.entry_id}"
    try:
        entity_id, entity = await _entity(hass)
        for night in (26, 27, 28):
            await _at(freezer, night, 3, 10)
            await _tick(entity, adb)
            await _at(freezer, night, 3, 40)
            await _tick(entity, adb)  # same night: no second try
            if night < 28:
                assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
        assert adb.install.await_count == 3
        attrs = hass.states.get(entity_id).attributes
        assert attrs["auto_update_failures"] == 3
        assert attrs["last_auto_update"]["result"] == "install_failed"
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.translation_placeholders["failures"] == "3"

        async def installed(_apk):
            tv["version"] = "0.30.0"

        adb.install.side_effect = installed
        await _at(freezer, 29, 3, 10)
        await _tick(entity, adb)
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
        assert hass.states.get(entity_id).attributes["auto_update_failures"] == 0
    finally:
        await _teardown(hass, entry, patchers)


async def test_user_fixable_failure_repairs_at_once(hass: HomeAssistant, freezer) -> None:
    """Not authorised on the TV: no point in waiting three nights."""
    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    adb = _fake_adb()
    adb.connect.side_effect = AdbError("not_authorized")
    try:
        _, entity = await _entity(hass)
        await _at(freezer, 26, 3, 10)
        await _tick(entity, adb)
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"auto_update_failed_{entry.entry_id}"
        )
        assert issue is not None
        assert "did not allow debugging" in issue.translation_placeholders["reason"]
    finally:
        await _teardown(hass, entry, patchers)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"visible": True}, "a popup on screen"),
        ({"version": "0.30.0"}, "nothing to install"),
    ],
)
async def test_skips_without_using_the_night(
    hass: HomeAssistant, freezer, change, reason
) -> None:
    """A popup on screen or nothing new: skipped, and tried again at a later tick."""
    tv = {**STATE, **change}
    entry, patchers = await _setup(hass, tv, AUTO)
    adb = _fake_adb()
    try:
        entity_id, entity = await _entity(hass)
        await _at(freezer, 26, 3, 10)
        await _tick(entity, adb)
        adb.connect.assert_not_awaited()
        assert hass.states.get(entity_id).attributes["last_auto_update"] is None, reason
    finally:
        await _teardown(hass, entry, patchers)


async def test_disabled_and_offline(hass: HomeAssistant, freezer) -> None:
    """Off by default; an unreachable TV is not attempted."""
    entry, patchers = await _setup(hass, dict(STATE), {"install_method": "auto"})
    adb = _fake_adb()
    try:
        _, entity = await _entity(hass)
        await _at(freezer, 26, 3, 10)
        await _tick(entity, adb)
        adb.connect.assert_not_awaited()

        hass.config_entries.async_update_entry(entry, options=AUTO)
        await hass.async_block_till_done()
        entry.runtime_data.online = False
        await _tick(entity, adb)
        adb.connect.assert_not_awaited()
    finally:
        await _teardown(hass, entry, patchers)


async def test_tv_in_use_is_never_opened(hass: HomeAssistant, freezer) -> None:
    """The service stays silent on a TV that is on: no activity, recorded as installed."""
    from custom_components.pipup.api import PiPupError, PiPupClient

    tv = dict(STATE)
    entry, patchers = await _setup(hass, tv, AUTO)
    adb = _fake_adb()
    adb.asleep = AsyncMock(return_value=False)
    try:
        entity_id, entity = await _entity(hass)
        PiPupClient.state.side_effect = PiPupError("down")
        await _at(freezer, 26, 3, 10)
        with patch("custom_components.pipup.update.ADB_APP_BACK_TIMEOUT", 0):
            await _tick(entity, adb)
        adb.start_activity.assert_not_awaited()
        record = hass.states.get(entity_id).attributes["last_auto_update"]
        assert record["result"] == "success"
        assert record["to"] == "0.30.0"
    finally:
        await _teardown(hass, entry, patchers)


async def test_app_method_on_silent_tv(hass: HomeAssistant, freezer) -> None:
    """Android 12+: the app's own update; success once the version changed."""
    from custom_components.pipup.api import PiPupClient

    tv = {**STATE, "device": {"android": "14"}, "update": {"installing": False, "silent": True}}
    entry, patchers = await _setup(hass, tv, AUTO)
    adb = _fake_adb()
    try:
        entity_id, entity = await _entity(hass)
        await _at(freezer, 26, 3, 10)
        await _tick(entity, adb)
        PiPupClient.update_app.assert_awaited_once()
        adb.connect.assert_not_awaited()
        assert hass.states.get(entity_id).attributes["last_auto_update"]["result"] == "requested"

        tv["version"] = "0.30.0"
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        record = hass.states.get(entity_id).attributes["last_auto_update"]
        assert record["result"] == "success"
        assert record["method"] == "app"
    finally:
        await _teardown(hass, entry, patchers)


async def test_app_method_needing_remote_is_skipped(hass: HomeAssistant, freezer) -> None:
    """Android < 12 on the app route would wait for the remote at night: skipped."""
    from custom_components.pipup.api import PiPupClient

    entry, patchers = await _setup(
        hass, dict(STATE), {"install_method": "app", "auto_update": True}
    )
    try:
        _, entity = await _entity(hass)
        await _at(freezer, 26, 3, 10)
        await _tick(entity, _fake_adb())
        PiPupClient.update_app.assert_not_awaited()
    finally:
        await _teardown(hass, entry, patchers)


async def test_one_tv_at_a_time(hass: HomeAssistant, freezer) -> None:
    """Another TV installing: this one waits for a later tick, the night not used."""
    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    adb = _fake_adb()
    try:
        _, entity = await _entity(hass)
        lock = hass.data.setdefault(DOMAIN, {}).setdefault(
            "auto_update_lock", asyncio.Lock()
        )
        await _at(freezer, 26, 3, 10)
        async with lock:
            await _tick(entity, adb)
        adb.connect.assert_not_awaited()
        await _tick(entity, adb)
        adb.connect.assert_awaited_once()
    finally:
        await _teardown(hass, entry, patchers)


async def test_restart_inside_the_window_does_not_retry(hass: HomeAssistant, freezer) -> None:
    """The night's attempt survives a restart (restored attributes)."""
    await _at(freezer, 26, 3, 30)
    mock_restore_cache(
        hass,
        [
            State(
                "update.tv_pipup_app",
                "on",
                {
                    "last_auto_update": {
                        "time": _local(26, 3, 10).isoformat(),
                        "result": "install_failed",
                        "from": "0.29.2",
                        "to": None,
                        "method": "adb",
                    },
                    "auto_update_failures": 2,
                },
            )
        ],
    )
    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    adb = _fake_adb()
    try:
        entity_id, entity = await _entity(hass)
        assert entity_id == "update.tv_pipup_app"
        assert hass.states.get(entity_id).attributes["auto_update_failures"] == 2
        await _tick(entity, adb)
        adb.connect.assert_not_awaited()
    finally:
        await _teardown(hass, entry, patchers)


async def test_options_refuse_auto_update_needing_remote(hass: HomeAssistant) -> None:
    """Automatic updates on the app route of an Android 11 TV: refused with a reason."""
    entry, patchers = await _setup(hass, dict(STATE))
    try:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"advanced_install": {"install_method": "app", "adb_port": 5555,
                                  "adb_key": "own", "auto_update": True,
                                  "quiet_start": "03:00:00", "quiet_end": "05:00:00"}},
        )
        assert result["errors"] == {"advanced_install": "auto_update_needs_adb"}
        assert "auto_update" not in entry.options
    finally:
        await _teardown(hass, entry, patchers)


# --- review fixes (PR #51)


async def _fail_three_nights(hass, freezer, entity, adb) -> None:
    adb.install.side_effect = AdbError("install_failed", "Failure [X]")
    for night in (26, 27, 28):
        await _at(freezer, night, 3, 10)
        await _tick(entity, adb)


async def test_manual_install_clears_failures_and_repair(hass: HomeAssistant, freezer) -> None:
    """Updated by hand after failed nights: the repair goes, the counter restarts."""
    tv = dict(STATE)
    entry, patchers = await _setup(hass, tv, AUTO)
    adb = _fake_adb()
    issue_id = f"auto_update_failed_{entry.entry_id}"
    try:
        entity_id, entity = await _entity(hass)
        await _fail_three_nights(hass, freezer, entity, adb)
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

        tv["version"] = "0.30.0"  # Install pressed, or the app's own check
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
        assert hass.states.get(entity_id).attributes["auto_update_failures"] == 0
    finally:
        await _teardown(hass, entry, patchers)


async def test_switching_off_clears_repair(hass: HomeAssistant, freezer) -> None:
    """Automatic updates switched off: no repair about them stays behind."""
    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    adb = _fake_adb()
    issue_id = f"auto_update_failed_{entry.entry_id}"
    try:
        _, entity = await _entity(hass)
        await _fail_three_nights(hass, freezer, entity, adb)
        hass.config_entries.async_update_entry(entry, options={"install_method": "auto"})
        await hass.async_block_till_done()
        await _tick(entity, adb)
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    finally:
        await _teardown(hass, entry, patchers)


async def test_unexpected_error_uses_the_night(hass: HomeAssistant, freezer) -> None:
    """A surprise exception is recorded once, not retried every ten minutes."""
    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    adb = _fake_adb()
    adb.install.side_effect = ValueError("broken JSON from a mirror")
    try:
        entity_id, entity = await _entity(hass)
        await _at(freezer, 26, 3, 10)
        await _tick(entity, adb)
        await _at(freezer, 26, 3, 20)
        await _tick(entity, adb)
        assert adb.install.await_count == 1
        attrs = hass.states.get(entity_id).attributes
        assert attrs["last_auto_update"]["result"] == "unexpected"
        assert attrs["auto_update_failures"] == 1
        assert attrs["in_progress"] is False
    finally:
        await _teardown(hass, entry, patchers)


def _requested_record(day: int, hour: int, minute: int) -> State:
    return State(
        "update.tv_pipup_app",
        "on",
        {
            "last_auto_update": {
                "time": _local(day, hour, minute).isoformat(),
                "result": "requested",
                "from": "0.29.2",
                "to": None,
                "method": "app",
            },
            "auto_update_failures": 0,
        },
    )


SILENT_TV = {**STATE, "device": {"android": "14"}, "update": {"installing": False, "silent": True}}


async def test_restart_during_app_run_still_logs_success(hass: HomeAssistant, freezer) -> None:
    """HA restarted while the app updated itself: the run is picked up and closed."""
    await _at(freezer, 26, 3, 15)
    mock_restore_cache(hass, [_requested_record(26, 3, 10)])
    tv = {**SILENT_TV, "version": "0.30.0"}  # landed during the restart
    entry, patchers = await _setup(hass, tv, AUTO)
    try:
        entity_id, _ = await _entity(hass)
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        record = hass.states.get(entity_id).attributes["last_auto_update"]
        assert record["result"] == "success"
        assert record["to"] == "0.30.0"
    finally:
        await _teardown(hass, entry, patchers)


async def test_restart_during_app_run_counts_failure(hass: HomeAssistant, freezer) -> None:
    """Restarted, and the update never landed: a counted failure after the deadline."""
    await _at(freezer, 26, 3, 40)  # requested 30 minutes ago
    mock_restore_cache(hass, [_requested_record(26, 3, 10)])
    entry, patchers = await _setup(hass, dict(SILENT_TV), AUTO)
    try:
        entity_id, _ = await _entity(hass)
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        attrs = hass.states.get(entity_id).attributes
        assert attrs["last_auto_update"]["result"] == "app_update_not_landed"
        assert attrs["auto_update_failures"] == 1
    finally:
        await _teardown(hass, entry, patchers)


async def test_stuck_installing_flag_ends_the_run(hass: HomeAssistant, freezer) -> None:
    """The app keeps saying `installing`: the run still ends after the deadline."""
    tv = dict(SILENT_TV)
    entry, patchers = await _setup(hass, tv, AUTO)
    try:
        entity_id, entity = await _entity(hass)
        await _at(freezer, 26, 3, 10)
        await _tick(entity, _fake_adb())
        tv["update"] = {"installing": True, "silent": True}
        await _at(freezer, 26, 3, 30)
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        record = hass.states.get(entity_id).attributes["last_auto_update"]
        assert record["result"] == "app_update_not_landed"
        # readable, not a raw key
        assert "did not land within 15 minutes" in REASONS["app_update_not_landed"]
    finally:
        await _teardown(hass, entry, patchers)


async def test_dst_repeated_hour_is_one_night(hass: HomeAssistant, freezer) -> None:
    """Clocks go back (25-10-2026, 03:00 → 02:00): 02:30-03:30 is still one night."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    try:
        _, entity = await _entity(hass)
        tz = dt_util.get_default_time_zone()
        first = datetime(2026, 10, 25, 2, 40, tzinfo=tz)  # summer time, +02:00
        entity._last_auto = {"time": first.isoformat(), "result": "install_failed"}
        second = datetime(2026, 10, 25, 2, 35, fold=1, tzinfo=tz)  # winter time, +01:00
        night = window_start(second, time(2, 30), time(3, 30))
        assert night is not None and night.utcoffset().total_seconds() == 3600
        assert entity._attempted_since(night)
    finally:
        await _teardown(hass, entry, patchers)


async def test_old_app_without_silent_flag(hass: HomeAssistant, freezer) -> None:
    """An app that does not report `silent`: the Android version decides, in the
    options and in the tick alike."""
    from custom_components.pipup.api import PiPupClient

    old = {**STATE, "device": {"android": "14"}, "update": {"installing": False}}
    entry, patchers = await _setup(hass, old, {"install_method": "app", "auto_update": True})
    try:
        _, entity = await _entity(hass)
        await _at(freezer, 26, 3, 10)
        await _tick(entity, _fake_adb())
        PiPupClient.update_app.assert_awaited_once()
    finally:
        await _teardown(hass, entry, patchers)

    old11 = {**STATE, "device": {"android": "11"}, "update": {"installing": False}}
    entry, patchers = await _setup(hass, old11)
    try:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"advanced_install": {"install_method": "app", "adb_port": 5555,
                                  "adb_key": "own", "auto_update": True,
                                  "quiet_start": "03:00:00", "quiet_end": "05:00:00"}},
        )
        assert result["errors"] == {"advanced_install": "auto_update_needs_adb"}
    finally:
        await _teardown(hass, entry, patchers)


async def test_app_route_run_blocks_other_tvs(hass: HomeAssistant, freezer) -> None:
    """While one TV's app updates itself, another TV waits for a later tick."""
    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    adb = _fake_adb()
    try:
        _, entity = await _entity(hass)
        running = hass.data.setdefault(DOMAIN, {}).setdefault("auto_update_running", set())
        running.add("update.other_tv_app")
        await _at(freezer, 26, 3, 10)
        await _tick(entity, adb)
        adb.connect.assert_not_awaited()
        running.clear()
        await _tick(entity, adb)
        adb.connect.assert_awaited_once()
    finally:
        await _teardown(hass, entry, patchers)


async def test_tv_in_use_not_reinstalled_next_night(hass: HomeAssistant, freezer) -> None:
    """Installed while the TV was on: the next nights wait for the app, no reinstall."""
    from custom_components.pipup.api import PiPupError, PiPupClient

    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    adb = _fake_adb()
    adb.asleep = AsyncMock(return_value=False)
    try:
        _, entity = await _entity(hass)
        PiPupClient.state.side_effect = PiPupError("down")
        with patch("custom_components.pipup.update.ADB_APP_BACK_TIMEOUT", 0):
            await _at(freezer, 26, 3, 10)
            await _tick(entity, adb)
            await _at(freezer, 27, 3, 10)
            await _tick(entity, adb)
        assert adb.install.await_count == 1
    finally:
        await _teardown(hass, entry, patchers)


async def test_running_set_released(hass: HomeAssistant, freezer) -> None:
    """A finished app-route run and an unloaded entry leave no TV blocking the rest."""
    tv = dict(SILENT_TV)
    entry, patchers = await _setup(hass, tv, AUTO)
    running = lambda: hass.data[DOMAIN].get("auto_update_running")  # noqa: E731
    try:
        _, entity = await _entity(hass)
        await _at(freezer, 26, 3, 10)
        await _tick(entity, _fake_adb())
        assert running() == {entity.entity_id}
        tv["version"] = "0.30.0"
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert not running()

        # pending again, then the entry unloads mid-run
        tv["version"] = "0.29.2"  # e.g. an older build reinstalled by hand
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        await _at(freezer, 27, 3, 10)
        await _tick(entity, _fake_adb())
        assert running() == {entity.entity_id}
    finally:
        await _teardown(hass, entry, patchers)
    assert not running()


async def test_restart_with_fresh_requested_record_stays_pending(
    hass: HomeAssistant, freezer
) -> None:
    """Restarted 5 minutes after the request, not landed yet: still running."""
    await _at(freezer, 26, 3, 15)
    mock_restore_cache(hass, [_requested_record(26, 3, 10)])
    entry, patchers = await _setup(hass, dict(SILENT_TV), AUTO)
    try:
        entity_id, entity = await _entity(hass)
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        attrs = hass.states.get(entity_id).attributes
        assert attrs["in_progress"] is True
        assert attrs["last_auto_update"]["result"] == "requested"
        assert entity._auto_pending
        assert hass.data[DOMAIN]["auto_update_running"] == {entity_id}
    finally:
        await _teardown(hass, entry, patchers)
