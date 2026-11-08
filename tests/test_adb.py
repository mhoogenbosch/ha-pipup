"""Advanced install: method choice, the options section, and the ADB install itself."""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pipup.adb import (
    AdbError,
    PiPupAdb,
    ReleaseApk,
    async_download_apk,
    auto_update_installs,
    effective_method,
    release_apk,
)
from custom_components.pipup.api import PiPupError
from custom_components.pipup.const import DOMAIN

APK = b"PK\x03\x04 fake apk"
SHA = hashlib.sha256(APK).hexdigest()
APK_URL = "https://github.com/mhoogenbosch/PiPup/releases/download/v0.30.0/PiPup.apk"
RELEASE = {
    "tag_name": "v0.30.0",
    "assets": [
        {"browser_download_url": APK_URL.replace(".apk", ".apk.sha256")},
        {"browser_download_url": APK_URL, "digest": f"sha256:{SHA}"},
    ],
}
AUTO = {"install_method": "auto"}
STATE = {
    "app": "PiPup",
    "version": "0.29.2",
    "id": "dev1",
    "name": "Living Room",
    "visible": False,
    "popup": None,
    "device": {"android": "11"},
    "update": {"installing": False, "silent": False},
}


@pytest.mark.parametrize(
    ("options", "data", "expected"),
    [
        ({}, {"update": {"silent": False}}, "app"),  # default: as before 1.29
        (AUTO, {"update": {"silent": True}}, "app"),
        (AUTO, {"update": {"silent": False}}, "adb"),
        (AUTO, {"device": {"android": "11"}}, "adb"),
        (AUTO, {"device": {"android": "14"}}, "app"),
        (AUTO, {}, "app"),  # unknown: the app's own update
        ({"install_method": "adb"}, {"update": {"silent": True}}, "adb"),
        ({"install_method": "app"}, {"update": {"silent": False}}, "app"),
    ],
)
def test_effective_method(options, data, expected) -> None:
    """Default = app; Auto follows the app's `silent` flag, then the Android version."""
    assert effective_method(options, data) == expected


@pytest.mark.parametrize(
    ("options", "data", "expected"),
    [
        ({**AUTO, "auto_update": False}, {"update": {"silent": True}}, False),
        ({}, {"update": {"silent": True}}, False),  # off by default
        ({**AUTO, "auto_update": True}, {"update": {"silent": False}}, True),  # ADB
        ({**AUTO, "auto_update": True}, {"update": {"silent": True}}, True),  # app, silent
        ({"install_method": "adb", "auto_update": True}, {"update": {"silent": True}}, True),
        # the app's own update would wait for the remote: nothing installs at night
        ({"install_method": "app", "auto_update": True}, {"update": {"silent": False}}, False),
        ({"install_method": "app", "auto_update": True}, {"device": {"android": "11"}}, False),
    ],
)
def test_auto_update_installs(options, data, expected) -> None:
    """True only when the nightly update can install on this TV by itself."""
    assert auto_update_installs(options, data) is expected


async def test_auto_update_header(hass: HomeAssistant) -> None:
    """The header is left out until known, then follows the options on every poll."""
    from custom_components.pipup.api import PiPupClient

    client = PiPupClient(MagicMock(), "192.168.1.14", 7979)
    assert "X-HA-PiPup-Auto-Update" not in client._headers
    client.auto_update = True
    assert client._headers["X-HA-PiPup-Auto-Update"] == "on"
    assert client._headers["X-HA-PiPup-Version"]  # the version stays
    client.auto_update = False
    assert client._headers["X-HA-PiPup-Auto-Update"] == "off"

    entry, patchers = await _setup(hass, dict(STATE), {**AUTO, "auto_update": True})
    try:
        coordinator = entry.runtime_data
        assert coordinator.client.auto_update is True
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "auto_update": False}
        )
        await coordinator.async_refresh()
        assert coordinator.client.auto_update is False
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        for p in patchers:
            p.stop()


def test_release_apk_picks_apk_and_digest() -> None:
    """The .apk asset, not the .sha256 beside it; digest without its prefix."""
    apk = release_apk(RELEASE)
    assert apk == ReleaseApk(APK_URL, SHA)
    assert release_apk({"assets": []}) is None


async def test_download_verified_and_shared(hass: HomeAssistant, aioclient_mock) -> None:
    """One download for several TVs; a wrong checksum is refused."""
    aioclient_mock.get(APK_URL, content=APK)
    for _ in range(3):
        assert await async_download_apk(hass, ReleaseApk(APK_URL, SHA)) == APK
    assert aioclient_mock.call_count == 1

    bad = APK_URL.replace("v0.30.0", "v0.30.1")
    aioclient_mock.get(bad, content=b"tampered")
    with pytest.raises(AdbError) as err:
        await async_download_apk(hass, ReleaseApk(bad, SHA))
    assert err.value.reason == "checksum_mismatch"


def _fake_device(outputs: dict[str, str]) -> MagicMock:
    """An AdbDeviceAsync whose shell answers by command prefix."""
    device = MagicMock()
    device.connect = AsyncMock()
    device.close = AsyncMock()
    device.push = AsyncMock()
    calls: list[str] = []

    async def shell(command, **_):
        calls.append(command)
        for prefix, out in outputs.items():
            if command.startswith(prefix):
                return out
        return ""

    device.shell = AsyncMock(side_effect=shell)
    device.calls = calls
    return device


async def _adb_session(hass: HomeAssistant, device: MagicMock) -> PiPupAdb:
    with (
        patch("custom_components.pipup.adb.AdbDeviceAsync", return_value=device),
        patch(
            "custom_components.pipup.adb.async_get_signer",
            AsyncMock(return_value=object()),
        ),
    ):
        adb = PiPupAdb(hass, "192.168.1.14", {})
        await adb.connect(1)
    return adb


async def test_install_steps(hass: HomeAssistant) -> None:
    """install.sh's steps: push, pm install, remove, app-ops, accessibility back."""
    access = "nl.rogro82.pipup/nl.rogro82.pipup.PiPupAccessibilityService"
    device = _fake_device({"pm install": "Success"})
    reads = iter([access, "null"])  # on before the install, reset by it

    async def shell(command, **_):
        device.calls.append(command)
        if command == "settings get secure enabled_accessibility_services":
            return next(reads)
        if command.startswith("settings get"):
            return "null"  # no media listener granted
        return "Success" if command.startswith("pm install") else ""

    device.shell.side_effect = shell
    adb = await _adb_session(hass, device)
    await adb.install(APK)
    await adb.close()

    device.push.assert_awaited_once()
    assert device.push.await_args.args[1] == "/data/local/tmp/pipup.apk"
    calls = device.calls
    assert "pm install -r /data/local/tmp/pipup.apk" in calls
    assert "rm -f /data/local/tmp/pipup.apk" in calls
    assert "appops set nl.rogro82.pipup SYSTEM_ALERT_WINDOW allow" in calls
    assert "appops set nl.rogro82.pipup REQUEST_INSTALL_PACKAGES allow" in calls
    assert "cmd appops set nl.rogro82.pipup android:auto_start allow" in calls
    assert f"settings put secure enabled_accessibility_services {access}" in calls
    device.close.assert_awaited()


async def test_install_never_uninstalls_on_signature_clash(hass: HomeAssistant) -> None:
    """A different signature is reported, never 'fixed' by uninstalling."""
    device = _fake_device(
        {"pm install": "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]"}
    )
    adb = await _adb_session(hass, device)
    with pytest.raises(AdbError) as err:
        await adb.install(APK)
    assert err.value.reason == "signature_mismatch"
    assert not any("uninstall" in c for c in device.calls)
    assert "rm -f /data/local/tmp/pipup.apk" in device.calls


async def test_connect_errors(hass: HomeAssistant) -> None:
    """Timeout after the prompt = not authorised; before it = cannot connect."""
    from adb_shell.exceptions import TcpTimeoutException

    async def refused(**_):
        raise TcpTimeoutException("no answer")

    async def unanswered(rsa_keys, auth_timeout_s, auth_callback):
        auth_callback(None)
        raise TcpTimeoutException("no answer")

    for side_effect, reason in ((refused, "cannot_connect"), (unanswered, "not_authorized")):
        device = _fake_device({})
        device.connect.side_effect = side_effect
        with pytest.raises(AdbError) as err:
            await _adb_session(hass, device)
        assert err.value.reason == reason
        device.close.assert_awaited()


async def _setup(hass: HomeAssistant, tv: dict, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN, title="TV", unique_id="dev1",
        data={"host": "192.168.1.14", "port": 7979}, options=options or {},
    )
    entry.add_to_hass(hass)
    patchers = [
        patch.multiple(
            "custom_components.pipup.api.PiPupClient",
            state=AsyncMock(side_effect=lambda *_: dict(tv)),
            settings=AsyncMock(return_value={}),
            update_app=AsyncMock(return_value=None),
        ),
        patch("custom_components.pipup.update._latest_release_tag",
              AsyncMock(return_value="0.30.0")),
        patch("custom_components.pipup.update._latest_release",
              AsyncMock(return_value=RELEASE)),
        patch("custom_components.pipup.update.async_download_apk",
              AsyncMock(return_value=APK)),
        patch("custom_components.pipup.update.ADB_APP_BACK_POLL", 0),
        patch("custom_components.pipup.coordinator.POST_CALL_SETTLE_SECONDS", 0),
    ]
    for p in patchers:
        p.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, patchers


def _fake_adb() -> MagicMock:
    adb = MagicMock()
    adb.__aenter__ = AsyncMock(return_value=adb)
    adb.__aexit__ = AsyncMock(return_value=None)
    adb.connect = AsyncMock()
    adb.install = AsyncMock()
    adb.start_service = AsyncMock()
    adb.start_activity = AsyncMock()
    return adb


async def test_install_button_uses_adb_below_android_12(hass: HomeAssistant) -> None:
    """Android 11 + Auto: Install goes over ADB, the app's self-update is not asked."""
    tv = dict(STATE)
    entry, patchers = await _setup(hass, tv, AUTO)
    adb = _fake_adb()
    # a cached APK from the download: gone after the install
    hass.data.setdefault(DOMAIN, {})["apk_cache"] = {APK_URL: APK}

    async def installed(_apk):
        tv["version"] = "0.30.0"

    adb.install.side_effect = installed
    try:
        from custom_components.pipup.api import PiPupClient

        entity_id = next(s.entity_id for s in hass.states.async_all("update"))
        attrs = hass.states.get(entity_id).attributes
        assert attrs["install_method"] == "adb"
        assert attrs["requires_remote"] is False
        with patch("custom_components.pipup.update.PiPupAdb", return_value=adb):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )
        adb.install.assert_awaited_once_with(APK)
        adb.start_service.assert_awaited_once()
        adb.start_activity.assert_not_awaited()
        PiPupClient.update_app.assert_not_awaited()
        state = hass.states.get(entity_id)
        assert state.attributes["installed_version"] == "0.30.0"
        assert state.attributes["in_progress"] is False
        assert hass.data[DOMAIN]["apk_cache"] == {}
    finally:
        for p in patchers:
            p.stop()


async def test_install_falls_back_to_activity(hass: HomeAssistant) -> None:
    """The service stays silent (TCL guard): open the app, then it answers."""
    tv = dict(STATE)
    entry, patchers = await _setup(hass, tv, AUTO)
    adb = _fake_adb()
    try:
        from custom_components.pipup.api import PiPupClient

        down = {"on": True}
        PiPupClient.state.side_effect = lambda *_: (
            (_ for _ in ()).throw(PiPupError("down")) if down["on"] else dict(tv)
        )

        async def opened():
            down["on"] = False

        adb.start_activity.side_effect = opened
        entity_id = next(s.entity_id for s in hass.states.async_all("update"))
        with (
            patch("custom_components.pipup.update.PiPupAdb", return_value=adb),
            patch("custom_components.pipup.update.ADB_APP_BACK_TIMEOUT", 0),
        ):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )
        adb.start_activity.assert_awaited_once()
    finally:
        for p in patchers:
            p.stop()


async def test_install_failure_clears_progress(hass: HomeAssistant) -> None:
    """A failed ADB install raises with the reason and leaves no stuck progress."""
    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    adb = _fake_adb()
    adb.connect.side_effect = AdbError("not_authorized")
    try:
        entity_id = next(s.entity_id for s in hass.states.async_all("update"))
        with (
            patch("custom_components.pipup.update.PiPupAdb", return_value=adb),
            pytest.raises(HomeAssistantError, match="did not allow debugging"),
        ):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )
        assert hass.states.get(entity_id).attributes["in_progress"] is False
    finally:
        for p in patchers:
            p.stop()


def _section(**values) -> dict:
    return {"advanced_install": {"install_method": "app", "adb_port": 5555,
                                 "adb_key": "own", **values}}


async def test_options_authorize_flow(hass: HomeAssistant) -> None:
    """Changing the section tests ADB; an unknown key leads to the accept step."""
    entry, patchers = await _setup(hass, dict(STATE))
    test = AsyncMock(side_effect=["adb_not_authorized", "adb_not_authorized", None])
    try:
        with patch(
            "custom_components.pipup.config_flow.PiPupOptionsFlow._test_adb", test
        ):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _section(install_method="auto")
            )
            assert result["type"] == "form"
            assert result["step_id"] == "adb_authorize"
            # nobody pressed OK yet
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {}
            )
            assert result["errors"] == {"base": "adb_not_authorized"}
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {}
            )
            assert result["type"] == "create_entry"
        assert entry.options["install_method"] == "auto"
        assert test.await_count == 3
    finally:
        for p in patchers:
            p.stop()


async def test_options_no_adb_test_when_unchanged(hass: HomeAssistant) -> None:
    """Saving other options never touches ADB (a sleeping Fire TV has no network)."""
    entry, patchers = await _setup(hass, dict(STATE))
    test = AsyncMock(return_value="adb_cannot_connect")
    try:
        with patch(
            "custom_components.pipup.config_flow.PiPupOptionsFlow._test_adb", test
        ):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"default_duration": 10, **_section()}
            )
            assert result["type"] == "create_entry"
            test.assert_not_awaited()

            # a change that does need ADB, and the TV is unreachable: shown, not saved
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], _section(install_method="adb")
            )
            assert result["type"] == "form"
            assert result["errors"] == {"base": "adb_cannot_connect"}
        assert entry.options["install_method"] == "app"
    finally:
        for p in patchers:
            p.stop()


async def test_default_keeps_app_self_update(hass: HomeAssistant) -> None:
    """Without a choice in Advanced install, Android 11 still uses the app's update."""
    entry, patchers = await _setup(hass, dict(STATE))
    adb = _fake_adb()
    try:
        from custom_components.pipup.api import PiPupClient

        entity_id = next(s.entity_id for s in hass.states.async_all("update"))
        assert hass.states.get(entity_id).attributes["install_method"] == "app"
        with patch("custom_components.pipup.update.PiPupAdb", return_value=adb):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )
        PiPupClient.update_app.assert_awaited_once()
        adb.connect.assert_not_awaited()
    finally:
        for p in patchers:
            p.stop()


# --- review fixes (PR #50)


async def test_shell_waits_for_silent_command(hass: HomeAssistant) -> None:
    """`pm install` prints nothing for a while: the *effective* wait covers that.

    Checked through adb-shell's own transaction info, because the library caps the
    transport timeout at the read timeout. 1.30.0 passed only a long transport
    timeout, every wait became 10 s, and a TCL's install broke off half-way.
    """
    from adb_shell import constants
    from adb_shell.hidden_helpers import _AdbTransactionInfo

    device = _fake_device({"pm install": "Success"})
    adb = await _adb_session(hass, device)
    await adb.install(APK)
    calls = [c.kwargs for c in device.shell.await_args_list]
    calls.append(device.push.await_args.kwargs)
    for kwargs in calls:
        info = _AdbTransactionInfo(
            None,
            None,
            kwargs.get("transport_timeout_s"),
            kwargs.get("read_timeout_s", constants.DEFAULT_READ_TIMEOUT_S),
            kwargs.get("timeout_s"),
        )
        assert info.transport_timeout_s >= 60, kwargs


async def test_closed_socket_fails_fast() -> None:
    """A TV that closes the socket: an error at once, not a spin until the timeout."""
    import asyncio

    from adb_shell.exceptions import AdbConnectionError

    from custom_components.pipup.adb import _EofSafeTransport

    transport = _EofSafeTransport("192.0.2.1", 5555)
    reader = asyncio.StreamReader()
    reader.feed_eof()
    transport._reader = reader
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(AdbConnectionError):
        await transport.bulk_read(24, 120)
    assert loop.time() - start < 1


async def test_install_order(hass: HomeAssistant) -> None:
    """Push, install, remove, then the app-ops - never app-ops before the install."""
    device = _fake_device({"pm install": "Success"})
    adb = await _adb_session(hass, device)
    await adb.install(APK)
    calls = device.calls
    pm = calls.index("pm install -r /data/local/tmp/pipup.apk")
    rm = calls.index("rm -f /data/local/tmp/pipup.apk")
    overlay = calls.index("appops set nl.rogro82.pipup SYSTEM_ALERT_WINDOW allow")
    assert pm < rm < overlay


async def test_push_failure_is_mapped_and_cleaned_up(hass: HomeAssistant) -> None:
    """A full /data/local/tmp (FAIL from sync) is push_failed; the partial APK goes."""
    from adb_shell.exceptions import PushFailedError

    device = _fake_device({})
    device.push.side_effect = PushFailedError("No space left on device")
    adb = await _adb_session(hass, device)
    with pytest.raises(AdbError) as err:
        await adb.install(APK)
    assert err.value.reason == "push_failed"
    assert "rm -f /data/local/tmp/pipup.apk" in device.calls
    assert not any(c.startswith("pm install") for c in device.calls)


async def test_cleanup_does_not_hide_the_real_error(hass: HomeAssistant) -> None:
    """The connection dies during pm install: that error is reported, not the rm's."""
    from adb_shell.exceptions import TcpTimeoutException

    device = _fake_device({})
    seen: list[str] = []

    async def shell(command, **_):
        seen.append(command)
        if command.startswith("pm install"):
            return "Failure [INSTALL_FAILED_OLDER_SDK]"
        if command.startswith("rm "):
            raise TcpTimeoutException("gone")
        return ""

    device.shell.side_effect = shell
    adb = await _adb_session(hass, device)
    with pytest.raises(AdbError) as err:
        await adb.install(APK)
    assert err.value.reason == "install_failed"
    assert "rm -f /data/local/tmp/pipup.apk" in seen
    assert not any(c.startswith("appops") for c in seen)


async def test_connect_closes_socket_on_unexpected_error(hass: HomeAssistant) -> None:
    """Library errors outside the known set still close the transport."""
    from adb_shell.exceptions import InvalidCommandError

    for exc, expected in ((InvalidCommandError("bad"), AdbError), (RuntimeError("x"), RuntimeError)):
        device = _fake_device({})
        device.connect.side_effect = exc
        with pytest.raises(expected):
            await _adb_session(hass, device)
        device.close.assert_awaited()


@pytest.mark.parametrize(
    ("power", "sleeps"),
    [
        ("  mWakefulness=Asleep", True),
        ("  mWakefulness=Dozing", True),
        ("  mWakefulness=Awake", False),
        ("  mWakefulness=Dreaming", False),  # screensaver: someone may be watching
        ("", False),  # no answer: leave the TV on
        ("/system/bin/sh: dumpsys: inaccessible or not found", False),
    ],
)
async def test_activity_fallback_only_resleeps_a_sleeping_tv(
    hass: HomeAssistant, power: str, sleeps: bool
) -> None:
    """The fallback switches a TV off again only when it positively was asleep."""
    device = _fake_device({"dumpsys power": power})
    adb = await _adb_session(hass, device)
    with patch("custom_components.pipup.adb.asyncio.sleep", AsyncMock()):
        await adb.start_activity()
    assert "input keyevent KEYCODE_WAKEUP" in device.calls
    assert ("input keyevent KEYCODE_SLEEP" in device.calls) is sleeps


async def test_own_key_private_and_created_once(hass: HomeAssistant, tmp_path) -> None:
    """The own key is 0600, and two first uses at once generate it only once."""
    import asyncio
    import os
    import stat

    from custom_components.pipup import adb as adb_mod

    with (
        patch.object(hass.config, "config_dir", str(tmp_path)),
        patch.object(adb_mod, "keygen", wraps=adb_mod.keygen) as keygen,
    ):
        os.makedirs(tmp_path / ".storage")
        await asyncio.gather(
            adb_mod.async_get_signer(hass, "own"), adb_mod.async_get_signer(hass, "own")
        )
    path = tmp_path / ".storage" / "pipup_adbkey"
    assert keygen.call_count == 1
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    with pytest.raises(AdbError) as err:
        with patch.object(hass.config, "config_dir", str(tmp_path)):
            await adb_mod.async_get_signer(hass, "androidtv")
    assert err.value.reason == "no_androidtv_key"

    # a key without its .pub (found on a real host), or PEM-wrapped garbage
    # (pyasn1 error): a clear error, not a traceback
    key = tmp_path / ".storage" / "androidtv_adbkey"
    key.write_bytes(path.read_bytes())
    for content in (None, b"-----BEGIN PRIVATE KEY-----\nQUJD\n-----END PRIVATE KEY-----\n"):
        if content is not None:
            key.write_bytes(content)
            (tmp_path / ".storage" / "androidtv_adbkey.pub").write_text("x")
        with pytest.raises(AdbError) as err:
            with patch.object(hass.config, "config_dir", str(tmp_path)):
                await adb_mod.async_get_signer(hass, "androidtv")
        assert err.value.reason == "key_unreadable"


async def test_release_unavailable_is_not_no_apk(hass: HomeAssistant) -> None:
    """GitHub unreachable: say so, instead of 'the release has no APK'."""
    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    adb = _fake_adb()
    try:
        from custom_components.pipup import update

        update._latest_release.return_value = None
        entity_id = next(s.entity_id for s in hass.states.async_all("update"))
        with (
            patch("custom_components.pipup.update.PiPupAdb", return_value=adb),
            pytest.raises(HomeAssistantError, match="could not fetch the latest release"),
        ):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )
        adb.connect.assert_not_awaited()
    finally:
        for p in patchers:
            p.stop()


async def test_latest_release_none_when_source_down(hass: HomeAssistant, aioclient_mock) -> None:
    """The real _latest_release returns None on a failed fetch (not a stale dict)."""
    from custom_components.pipup.update import _latest_release

    aioclient_mock.get(
        "https://api.github.com/repos/mhoogenbosch/PiPup/releases/latest", status=403
    )
    assert await _latest_release(hass, "github:mhoogenbosch/PiPup") is None


async def test_unexpected_error_clears_progress(hass: HomeAssistant) -> None:
    """Not an AdbError: still no Install button stuck in progress."""
    entry, patchers = await _setup(hass, dict(STATE), AUTO)
    adb = _fake_adb()
    adb.install.side_effect = RuntimeError("library surprise")
    try:
        entity_id = next(s.entity_id for s in hass.states.async_all("update"))
        with (
            patch("custom_components.pipup.update.PiPupAdb", return_value=adb),
            pytest.raises(RuntimeError),
        ):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )
        assert hass.states.get(entity_id).attributes["in_progress"] is False
    finally:
        for p in patchers:
            p.stop()


async def test_apk_forgotten_after_install(hass: HomeAssistant, aioclient_mock) -> None:
    """After a successful install the ~8 MB APK does not stay in memory."""
    from custom_components.pipup import adb as adb_mod

    aioclient_mock.get(APK_URL, content=APK)
    await async_download_apk(hass, ReleaseApk(APK_URL, SHA))
    assert hass.data[DOMAIN]["apk_cache"]
    adb_mod.forget_apk(hass)
    assert not hass.data[DOMAIN]["apk_cache"]


async def test_options_error_keeps_entered_values(hass: HomeAssistant) -> None:
    """ADB unreachable: the form shows what was entered, not the stored options."""
    entry, patchers = await _setup(hass, dict(STATE))
    test = AsyncMock(return_value="adb_cannot_connect")
    try:
        with patch(
            "custom_components.pipup.config_flow.PiPupOptionsFlow._test_adb", test
        ):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"default_duration": 42, **_section(install_method="adb", adb_port=5556)},
            )
        assert result["errors"] == {"base": "adb_cannot_connect"}
        schema = {str(k): k for k in result["data_schema"].schema}
        assert schema["default_duration"].default() == 42
        section_schema = result["data_schema"].schema[schema["advanced_install"]].schema.schema
        # fields without a default (the click list: suggested value, so it can be cleared)
        defaults = {str(k): k.default() for k in section_schema if callable(k.default)}
        assert defaults["install_method"] == "adb"
        assert defaults["adb_port"] == 5556
    finally:
        for p in patchers:
            p.stop()


async def test_section_round_trip_keeps_adb(hass: HomeAssistant) -> None:
    """Saving an unrelated option with the section as stored keeps ADB, untested."""
    options = {"install_method": "adb", "adb_port": 5555, "adb_key": "androidtv"}
    entry, patchers = await _setup(hass, dict(STATE), options)
    test = AsyncMock(return_value="adb_cannot_connect")
    try:
        with patch(
            "custom_components.pipup.config_flow.PiPupOptionsFlow._test_adb", test
        ):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"default_duration": 7, **_section(install_method="adb", adb_key="androidtv")},
            )
        assert result["type"] == "create_entry"
        test.assert_not_awaited()
        assert entry.options["install_method"] == "adb"
        assert entry.options["adb_key"] == "androidtv"
    finally:
        for p in patchers:
            p.stop()


def test_flow_errors_are_translated() -> None:
    """Every error the options flow can show has a text in every language."""
    import json
    from pathlib import Path

    from custom_components.pipup.adb import REASONS

    connect_reasons = {"cannot_connect", "not_authorized", "no_androidtv_key", "key_unreadable"}
    assert connect_reasons <= set(REASONS)
    root = Path(__file__).parent.parent / "custom_components" / "pipup"
    for path in [root / "strings.json", *sorted((root / "translations").glob("*.json"))]:
        errors = json.loads(path.read_text(encoding="utf-8"))["options"]["error"]
        for reason in connect_reasons:
            assert f"adb_{reason}" in errors, (path.name, reason)
