"""Built-in ADB install for TVs where the app cannot update itself silently.

Android < 12 forces an on-screen confirmation for any app-initiated install (PiPup
#76). A shell-initiated `pm install -r` never asks, which is why `install.sh` works -
but Home Assistant OS has no `adb` binary. This module does the same steps in pure
Python with adb-shell, the library core's Android TV integration uses.

One connection per install, opened and closed here: nothing stays connected, so it
never competes with the Android TV integration's own connection for longer than the
install takes.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import aiohttp
from adb_shell.adb_device_async import AdbDeviceAsync
from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.exceptions import (
    AdbCommandFailureException,
    AdbConnectionError,
    AdbTimeoutError,
    DeviceAuthError,
    InvalidChecksumError,
    InvalidCommandError,
    InvalidResponseError,
    PushFailedError,
    TcpTimeoutException,
)
from adb_shell.transport.tcp_transport_async import TcpTransportAsync

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import STORAGE_DIR

from .const import (
    ADB_KEY_ANDROIDTV,
    DOMAIN,
    CONF_ADB_KEY,
    CONF_ADB_PORT,
    CONF_AUTO_UPDATE,
    CONF_CLICK_PACKAGES,
    CONF_INSTALL_METHOD,
    CONF_MEDIA_ACCESS,
    DEFAULT_ADB_PORT,
    DEFAULT_INSTALL_METHOD,
    INSTALL_METHOD_ADB,
    INSTALL_METHOD_APP,
    INSTALL_METHOD_AUTO,
)

_LOGGER = logging.getLogger(__name__)

PACKAGE = "nl.rogro82.pipup"
SERVICE_COMPONENT = f"{PACKAGE}/.PiPupService"
ACTIVITY_COMPONENT = f"{PACKAGE}/.MainActivity"
ACCESSIBILITY_COMPONENT = f"{PACKAGE}/{PACKAGE}.PiPupAccessibilityService"
# app >= 0.32.0 (PiPup #83): the separate service that presses views (off by default),
# the listener that only exists for the media session grant, and the receiver that
# takes the list of apps the control service may press in (shell-only: it requires
# WRITE_SECURE_SETTINGS from the sender, so no app on the TV can widen the list)
CONTROL_COMPONENT = f"{PACKAGE}/{PACKAGE}.PiPupControlService"
MEDIA_LISTENER_COMPONENT = f"{PACKAGE}/{PACKAGE}.MediaListenerService"
CLICK_RECEIVER = f"{PACKAGE}/.ControlConfigReceiver"
CLICK_ACTION = f"{PACKAGE}.SET_CLICK_PACKAGES"
# every PiPup accessibility service a reinstall may switch off
ACCESSIBILITY_COMPONENTS = (ACCESSIBILITY_COMPONENT, CONTROL_COMPONENT)
# seconds between switching the control service off and on again (see _restart_control_service)
CONTROL_RESTART_PAUSE = 2
DEVICE_APK = "/data/local/tmp/pipup.apk"

OWN_KEY = "pipup_adbkey"
ANDROIDTV_KEY = "androidtv_adbkey"

# connect: the TCP part; an unknown key additionally waits AUTH_TIMEOUT for the
# "Allow debugging?" answer on the TV
CONNECT_TIMEOUT = 10.0
# How long one command may stay silent: `pm install` prints nothing until it is
# done (~5 s on a Fire TV, over 10 s on a TCL). adb-shell caps the transport
# timeout (the wait for the next bytes) at the read timeout - `min(transport,
# read)` in _AdbTransactionInfo - so both have to be raised; with the library's
# default read timeout every wait was cut to 10 s (1.30.0, found on a TCL).
# The long read timeout is safe because of _EofSafeTransport below.
SHELL_TIMEOUT = 120.0
# everything adb-shell raises while talking to a device, plus socket errors
_ADB_ERRORS = (
    AdbCommandFailureException,
    AdbConnectionError,
    AdbTimeoutError,
    InvalidChecksumError,
    InvalidCommandError,
    InvalidResponseError,
    PushFailedError,
    TcpTimeoutException,
    OSError,
)
# shared APK downloads, keyed by URL: N TVs = one download
_APK_CACHE_KEY = "apk_cache"
_APK_LOCK_KEY = "apk_lock"
_KEY_LOCK_KEY = "adb_key_lock"


# what the user reads when an ADB install fails (Install button, log)
REASONS: dict[str, str] = {
    "cannot_connect": "cannot reach ADB on the TV (is network debugging on?)",
    "not_authorized": (
        "the TV did not allow debugging from Home Assistant: open the integration's "
        "options, Advanced install, and save to get the prompt again"
    ),
    "no_androidtv_key": "the Android TV integration's ADB key does not exist",
    "key_unreadable": "the ADB key file cannot be read",
    "connection_lost": "the ADB connection dropped",
    "push_failed": "copying the APK to the TV failed",
    "signature_mismatch": (
        "another build of PiPup with a different signature is installed; it has to "
        "be uninstalled by hand (this loses the app's device id)"
    ),
    "install_failed": "pm install failed",
    "grant_failed": "the TV did not accept the change",
    "release_unavailable": "could not fetch the latest release (offline or rate limited?)",
    "no_apk": "the latest release has no APK",
    "download_failed": "downloading the APK failed",
    "checksum_mismatch": "the downloaded APK does not match the release's sha256",
    "app_not_back": "the app did not start again after the install",
    "app_update_not_landed": "the app's own update did not land within 15 minutes",
    "app_unreachable": "the app on the TV did not answer",
    "unexpected": "an unexpected error",
    "tv_in_use": (
        "the app did not start again by itself and the TV is in use, so it was not "
        "opened; it starts the next time the TV is switched on"
    ),
}


def component_list(value: str) -> list[str]:
    """Entries of a ':'-separated component setting; "null" or empty = none.

    A "null" entry inside the list is dropped too: `settings get` prints null for an
    unset setting, and the readme's hand-written append before app 0.32.0 kept it
    (`null:nl.rogro82.pipup/...`, PiPup #83). The next write leaves it out.
    """
    return [
        part.strip()
        for part in value.split(":")
        if part.strip() and part.strip() != "null"
    ]


def full_component(component: str) -> str:
    """`pkg/.Class` (short form) as `pkg/pkg.Class`, so both forms compare equal."""
    pkg, _, cls = component.partition("/")
    return f"{pkg}/{pkg}{cls}" if cls.startswith(".") else component


def has_component(value: str, component: str) -> bool:
    """Whether a component setting lists `component`, in either form."""
    return any(full_component(c) == component for c in component_list(value))


class AdbError(Exception):
    """ADB install failed; `reason` is a key of REASONS."""

    def __init__(self, reason: str, detail: str = "") -> None:
        message = REASONS.get(reason, reason)
        super().__init__(f"{message}: {detail}" if detail else message)
        self.reason = reason
        self.detail = detail


def requires_remote(data: dict[str, Any]) -> bool:
    """Whether the app's own update needs a press on the remote on this TV.

    `silent` is authoritative (app >= 0.12.1: false on Android < 12, where an
    install cannot complete without a remote press); fall back to the OS version
    for older apps that do not report it. Unknown = no.
    """
    silent = (data.get("update") or {}).get("silent")
    if silent is not None:
        return not silent
    android = str((data.get("device") or {}).get("android") or "")
    major = android.split(".")[0]
    return major.isdigit() and int(major) < 12


def effective_method(options: dict[str, Any], data: dict[str, Any]) -> str:
    """App or ADB for this TV: Auto = app on Android 12+ (silent), ADB below.

    The default stays the app's self-update: ADB needs network debugging and an
    accepted prompt on the TV, so it is only used once someone chose it (and the
    options flow tested it). `update.silent` from the app is authoritative (app >=
    0.12.1); older apps only report the Android version. Unknown = app.
    """
    method = options.get(CONF_INSTALL_METHOD, DEFAULT_INSTALL_METHOD)
    if method != INSTALL_METHOD_AUTO:
        return method
    silent = (data.get("update") or {}).get("silent")
    if silent is None:
        android = str((data.get("device") or {}).get("android") or "")
        major = android.split(".")[0]
        if not major.isdigit():
            return INSTALL_METHOD_APP
        silent = int(major) >= 12
    return INSTALL_METHOD_APP if silent else INSTALL_METHOD_ADB


def auto_update_installs(options: dict[str, Any], data: dict[str, Any]) -> bool:
    """Whether the nightly automatic update can install on this TV by itself.

    Switched on, and either ADB or an app self-update that needs no press on the
    remote. The nightly tick asks this, and so does the X-HA-PiPup-Auto-Update header
    that keeps the app's own Install popup away (app >= 0.30.1, PiPup #76).
    """
    if not options.get(CONF_AUTO_UPDATE, False):
        return False
    return effective_method(options, data) == INSTALL_METHOD_ADB or not requires_remote(
        data
    )


def adb_ready(options: dict[str, Any], data: dict[str, Any]) -> bool:
    """Whether this TV has an ADB setup in ha-pipup that the options flow tested.

    Only then may a service call open ADB by itself (pressing OK, waking the screen):
    a TV that does not know our key would show "Allow debugging?" to whoever is
    watching. Tested = the install uses ADB, or media access / the click list were
    applied over ADB.
    """
    return (
        effective_method(options, data) == INSTALL_METHOD_ADB
        or bool(options.get(CONF_CLICK_PACKAGES))
        or bool(options.get(CONF_MEDIA_ACCESS))
    )


# Keys a service call may send over ADB. Fixed names only: the app's answer says
# which key it needs, and nothing from the network goes into a shell command.
KEYCODE_DPAD_CENTER = "KEYCODE_DPAD_CENTER"
KEYCODE_WAKEUP = "KEYCODE_WAKEUP"
# 1.32.0b4 (PiPup #83): the app's media key names and the same key over ADB. ADB presses
# the key in the app on screen, as the remote does; used when the app sees no media session.
ADB_MEDIA_KEYS: dict[str, str] = {
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "play": "KEYCODE_MEDIA_PLAY",
    "pause": "KEYCODE_MEDIA_PAUSE",
    "stop": "KEYCODE_MEDIA_STOP",
    "next": "KEYCODE_MEDIA_NEXT",
    "previous": "KEYCODE_MEDIA_PREVIOUS",
    "rewind": "KEYCODE_MEDIA_REWIND",
    "fast_forward": "KEYCODE_MEDIA_FAST_FORWARD",
}
_ADB_KEYS = (KEYCODE_DPAD_CENTER, KEYCODE_WAKEUP, *ADB_MEDIA_KEYS.values())
# the key is authorised already (adb_ready): no long wait for a prompt
KEY_AUTH_TIMEOUT = 5.0


async def async_adb_key(
    hass: HomeAssistant, host: str, options: dict[str, Any], key: str
) -> None:
    """Send one key to the TV over ADB (`input keyevent`); raises AdbError."""
    if key not in _ADB_KEYS:
        raise ValueError(f"not a key ha-pipup sends: {key}")
    async with PiPupAdb(hass, host, options) as adb:
        await adb.connect(KEY_AUTH_TIMEOUT)
        await adb.shell(f"input keyevent {key}")


def _load_signer(path: str) -> PythonRSASigner:
    """Read (and if needed create) a key pair. Blocking: run in the executor."""
    if not os.path.isfile(path):
        keygen(path)
        # keygen writes with the process umask; the private key is for us alone
        os.chmod(path, 0o600)
    return PythonRSASigner.FromRSAKeyPath(path)


async def async_get_signer(hass: HomeAssistant, which: str) -> PythonRSASigner:
    """The key ha-pipup signs with: its own by default, or the Android TV one.

    The Android TV key is only read, never created: if that integration is not set
    up there is nothing to reuse, and creating its file would confuse it later.
    """
    if which == ADB_KEY_ANDROIDTV:
        path = hass.config.path(STORAGE_DIR, ANDROIDTV_KEY)
        if not await hass.async_add_executor_job(os.path.isfile, path):
            raise AdbError("no_androidtv_key")
    else:
        path = hass.config.path(STORAGE_DIR, OWN_KEY)
    # two TVs on first use must not both generate a key: one would then trust a
    # public key that the other overwrote
    lock = hass.data.setdefault(DOMAIN, {}).setdefault(_KEY_LOCK_KEY, asyncio.Lock())
    async with lock:
        try:
            return await hass.async_add_executor_job(_load_signer, path)
        except Exception as err:  # noqa: BLE001 - only file parsing happens here
            # a missing .pub next to the key, a damaged or unreadable file (rsa,
            # pyasn1 and cryptography each raise their own parse errors)
            raise AdbError("key_unreadable", f"{path}: {err}") from err


class _EofSafeTransport(TcpTransportAsync):
    """TCP transport that reports a socket the TV closed, instead of spinning on it.

    At EOF `StreamReader.read` returns b"" at once without yielding, and adb-shell
    keeps calling it until its read timeout expires - with a two-minute timeout
    that would block Home Assistant's event loop for two minutes.
    """

    async def bulk_read(self, numbytes, transport_timeout_s):
        data = await super().bulk_read(numbytes, transport_timeout_s)
        if not data and numbytes:
            raise AdbConnectionError("the TV closed the ADB connection")
        return data


class PiPupAdb:
    """One ADB session to a TV; use as `async with`."""

    def __init__(
        self, hass: HomeAssistant, host: str, options: dict[str, Any]
    ) -> None:
        self._hass = hass
        self._host = host
        self._port = int(options.get(CONF_ADB_PORT) or DEFAULT_ADB_PORT)
        self._key = options.get(CONF_ADB_KEY)
        self._device: AdbDeviceAsync | None = None

    async def connect(self, auth_timeout: float) -> None:
        """Connect, mapping the failures to what the user has to fix.

        adb-shell raises timeouts both for "nothing listens" and for "nobody
        answered the prompt". The auth callback tells them apart: it only runs once
        the TV rejected our signature and is showing "Allow debugging?".
        """
        signer = await async_get_signer(self._hass, self._key)
        device = AdbDeviceAsync(
            _EofSafeTransport(self._host, self._port),
            default_transport_timeout_s=CONNECT_TIMEOUT,
        )
        prompted = False

        def _prompted(_device) -> None:
            nonlocal prompted
            prompted = True

        try:
            await device.connect(
                rsa_keys=[signer],
                auth_timeout_s=auth_timeout,
                auth_callback=_prompted,
            )
        except (DeviceAuthError, InvalidResponseError) as err:
            await device.close()
            raise AdbError("not_authorized", str(err)) from err
        except _ADB_ERRORS as err:
            await device.close()
            if prompted:
                raise AdbError("not_authorized", str(err)) from err
            raise AdbError("cannot_connect", str(err)) from err
        except BaseException:
            # cancelled or something unforeseen: never leave the socket open
            await device.close()
            raise
        self._device = device

    async def __aenter__(self) -> PiPupAdb:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._device is not None:
            await self._device.close()
            self._device = None

    async def shell(self, command: str) -> str:
        """Run one shell command, output stripped."""
        assert self._device is not None
        try:
            out = await self._device.shell(
                command,
                transport_timeout_s=SHELL_TIMEOUT,
                read_timeout_s=SHELL_TIMEOUT,
                timeout_s=SHELL_TIMEOUT,
            )
        except _ADB_ERRORS as err:
            raise AdbError("connection_lost", str(err)) from err
        return (out or "").strip()

    async def _remove_apk(self) -> None:
        """Best effort: never let the clean-up hide the error that got us here."""
        try:
            await self.shell(f"rm -f {DEVICE_APK}")
        except AdbError as err:
            _LOGGER.debug("Could not remove %s: %s", DEVICE_APK, err)

    async def install(self, apk: bytes) -> None:
        """`install.sh`, minus the parts that need a human: install + app-ops."""
        assert self._device is not None
        # read before the install: which of PiPup's accessibility services and the
        # media listener the user had on (a reinstall may switch them off)
        accessibility = await self.shell(
            "settings get secure enabled_accessibility_services"
        )
        listeners = await self.shell("settings get secure enabled_notification_listeners")
        try:
            try:
                await self._device.push(
                    BytesIO(apk),
                    DEVICE_APK,
                    transport_timeout_s=SHELL_TIMEOUT,
                    read_timeout_s=SHELL_TIMEOUT,
                )
            except _ADB_ERRORS as err:
                # also a full /data/local/tmp (FAIL from the sync service)
                raise AdbError("push_failed", str(err)) from err
            out = await self.shell(f"pm install -r {DEVICE_APK}")
        finally:
            # also after a failed push: a partial APK must not stay on the TV
            await self._remove_apk()
        if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out or "signatures do not match" in out:
            # never uninstall to get past this: it would wipe the app's device id
            raise AdbError("signature_mismatch", out)
        if "Success" not in out:
            raise AdbError("install_failed", out)

        # the app-ops a reinstall resets and the app cannot set itself
        await self.shell(f"appops set {PACKAGE} SYSTEM_ALERT_WINDOW allow")
        await self.shell(f"appops set {PACKAGE} REQUEST_INSTALL_PACKAGES allow")
        # TCL only; elsewhere "Unknown operation string", which is fine
        await self.shell(f"cmd appops set {PACKAGE} android:auto_start allow")
        was_on = [c for c in ACCESSIBILITY_COMPONENTS if has_component(accessibility, c)]
        if was_on:
            current = await self.shell("settings get secure enabled_accessibility_services")
            missing = [c for c in was_on if not has_component(current, c)]
            if missing:
                merged = ":".join(component_list(current) + missing)
                await self.shell(f"settings put secure enabled_accessibility_services {merged}")
                await self.shell("settings put secure accessibility_enabled 1")
            if CONTROL_COMPONENT in was_on and CONTROL_COMPONENT not in missing:
                # survived the update: Android keeps binding it with the old version's
                # capabilities (app 0.32.0-beta.2 added tapping), so switch it off and on
                await self._restart_control_service()
        if has_component(listeners, MEDIA_LISTENER_COMPONENT):
            current = await self.shell("settings get secure enabled_notification_listeners")
            if not has_component(current, MEDIA_LISTENER_COMPONENT):
                await self.set_media_access(True)

    async def set_media_access(self, allow: bool) -> None:
        """Grant or withdraw the media session access (app >= 0.32.0).

        It is a notification-listener grant; the app's listener reads no notifications
        and only exists for MediaSessionManager. A low-RAM device ignores the grant;
        the app then reports `media.reason: low_ram`.
        """
        verb = "allow_listener" if allow else "disallow_listener"
        out = await self.shell(f"cmd notification {verb} {MEDIA_LISTENER_COMPONENT}")
        if any(word in out for word in ("Unknown", "Error", "Exception", "Can't find")):
            # no `cmd notification` (old Android): write the setting itself
            current = await self.shell("settings get secure enabled_notification_listeners")
            rest = [
                c for c in component_list(current)
                if full_component(c) != MEDIA_LISTENER_COMPONENT
            ]
            await self._put_components(
                "enabled_notification_listeners",
                rest + [MEDIA_LISTENER_COMPONENT] if allow else rest,
            )

    async def set_click_packages(self, packages: list[str]) -> None:
        """Set the apps PiPup may press views in, and switch its control service.

        The list goes to the app over a shell-only broadcast (the receiver requires
        WRITE_SECURE_SETTINGS from the sender). A non-empty list switches the
        separate control service on, an empty one switches it off; the user's other
        accessibility services, PiPup's screen-off service included, stay as they are.
        """
        value = ",".join(packages)
        out = await self.shell(
            f"am broadcast -f 0x20 -n {CLICK_RECEIVER} -a {CLICK_ACTION} "
            f'--es packages "{value}"'
        )
        if "result=1" not in out:
            # no receiver: an app older than 0.32.0
            raise AdbError("grant_failed", out)
        current = await self.shell("settings get secure enabled_accessibility_services")
        enabled = has_component(current, CONTROL_COMPONENT)
        if packages and enabled:
            # already on: off and on again, so it gets the installed app's capabilities
            await self._restart_control_service()
        elif packages and not enabled:
            await self._put_components(
                "enabled_accessibility_services",
                component_list(current) + [CONTROL_COMPONENT],
            )
            await self.shell("settings put secure accessibility_enabled 1")
        elif not packages and enabled:
            await self._put_components(
                "enabled_accessibility_services",
                [c for c in component_list(current) if full_component(c) != CONTROL_COMPONENT],
            )

    async def _restart_control_service(self) -> None:
        """Switch PiPup's control service off and on, leaving every other service as is.

        After an app update Android keeps binding an enabled accessibility service with
        the info of the old version: going from app 0.32.0-beta.1 to beta.2 the service
        could not tap until it was restarted. The pause lets Android see both changes.
        """
        current = await self.shell("settings get secure enabled_accessibility_services")
        others = [c for c in component_list(current) if full_component(c) != CONTROL_COMPONENT]
        await self._put_components("enabled_accessibility_services", others)
        await asyncio.sleep(CONTROL_RESTART_PAUSE)
        await self._put_components("enabled_accessibility_services", others + [CONTROL_COMPONENT])
        await self.shell("settings put secure accessibility_enabled 1")

    async def _put_components(self, setting: str, components: list[str]) -> None:
        """Write a ':'-separated component setting; an empty list deletes it."""
        if components:
            await self.shell(f"settings put secure {setting} {':'.join(components)}")
        else:
            await self.shell(f"settings delete secure {setting}")

    async def start_service(self) -> None:
        """Start the service in the background: does not touch the screen."""
        await self.shell(f"am start-foreground-service -n {SERVICE_COMPONENT}")

    async def asleep(self) -> bool:
        """True only on a positive "Asleep"/"Dozing": anything else may be in use."""
        power = await self.shell("dumpsys power | grep mWakefulness=")
        return any(f"mWakefulness={state}" in power for state in ("Asleep", "Dozing"))

    async def start_activity(self) -> None:
        """Fallback for a vendor guard that froze the background start (TCL).

        Wakes the TV, opens the app, goes Home, and puts the TV back to sleep if it
        was asleep - the same as `install.sh --wake`, plus the way back. Only on a
        positive "Asleep"/"Dozing": a screensaver (Dreaming) or an answer we do not
        understand may be someone watching, and that TV must stay on.
        """
        asleep = await self.asleep()
        await self.shell("input keyevent KEYCODE_WAKEUP")
        await self.shell(f"am start -n {ACTIVITY_COMPONENT}")
        await asyncio.sleep(3)
        await self.shell("input keyevent KEYCODE_HOME")
        if asleep:
            await self.shell("input keyevent KEYCODE_SLEEP")


@dataclass
class ReleaseApk:
    """The APK asset of a release, as the releases API describes it."""

    url: str
    sha256: str | None


def release_apk(release: dict[str, Any]) -> ReleaseApk | None:
    """Pick the APK asset; `digest` ("sha256:<hex>") is set by GitHub since 2025."""
    for asset in release.get("assets") or []:
        url = asset.get("browser_download_url") or ""
        if url.endswith(".apk"):
            digest = asset.get("digest") or ""
            sha256 = digest[7:].lower() if digest.startswith("sha256:") else None
            return ReleaseApk(url, sha256)
    return None


async def async_download_apk(hass: HomeAssistant, apk: ReleaseApk) -> bytes:
    """Download once, verify, and share the bytes between TVs."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    cache: dict[str, bytes] = domain_data.setdefault(_APK_CACHE_KEY, {})
    lock = domain_data.setdefault(_APK_LOCK_KEY, asyncio.Lock())
    async with lock:
        if apk.url in cache:
            return cache[apk.url]
        session = async_get_clientsession(hass)
        try:
            async with session.get(
                apk.url, timeout=aiohttp.ClientTimeout(total=300)
            ) as resp:
                if resp.status != 200:
                    raise AdbError("download_failed", f"HTTP {resp.status}")
                data = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AdbError("download_failed", str(err)) from err
        if apk.sha256 is not None:
            actual = await hass.async_add_executor_job(_sha256, data)
            if actual != apk.sha256:
                raise AdbError("checksum_mismatch", f"{actual} != {apk.sha256}")
        else:
            # a LAN mirror's releases.json may lack digests; pm install still refuses
            # an APK with another signature, so this is integrity, not authenticity
            _LOGGER.warning("No sha256 digest for %s; installing unverified", apk.url)
        # only the latest APK is worth keeping
        cache.clear()
        cache[apk.url] = data
        return data


def forget_apk(hass: HomeAssistant) -> None:
    """Drop the cached APK (~8 MB) once nothing needs it any more."""
    hass.data.get(DOMAIN, {}).get(_APK_CACHE_KEY, {}).clear()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
