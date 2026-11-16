"""Media player: launch apps, media keys and volume on the TV (app >= 0.29.0).

Without media session access this entity is a remote control, not a now-playing card:
state is the screen (on/off), volume is the TV's media volume, the source list is the
installed apps, and the transport buttons send media keys to whatever app holds the
media session.

With media session access (app >= 0.32.0, one ADB grant; PiPup #83) the app reports
the active session of any app - package, playing/paused, title, position, duration -
and can seek it. The entity then shows what is playing and offers SEEK. Which app is
in front is still unknown; only the app that holds the media session is.

1.32.0b4 (app >= 0.32.0-beta.4): the transport buttons reach the session itself, the
session's artwork is the entity picture (PiPup #83, ha-pipup #61), and a TV without a
session but with a tested ADB setup gets the media keys over ADB.

1.33.0 (app >= 0.33.0, PiPup #88 and #89): with usage access the app reports which app
is in front, also when nothing plays; that is the source, and the app id and name when
no session is active. With the screenshot switched on, the picture of the screen is the
entity picture while nothing with artwork plays.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import PiPupError, PiPupUnsupportedError
from .const import POWER_STATE_OFF
from .coordinator import PiPupCoordinator
from .entity import PiPupEntity, screen_token

_LOGGER = logging.getLogger(__name__)

# The installed apps rarely change; re-read them at most this often (and on demand
# when a source is not found).
APPS_REFRESH_INTERVAL = timedelta(hours=6)

# A re-read position within this of where the last one would be by now is the same
# position: the app stamps positionUpdatedAt with every answer, and taking each new
# stamp would write a new state on every poll and push while something plays.
POSITION_TOLERANCE_MS = 1500

# Without artwork the entity picture is the screen (1.33.0): a new picture at most this
# often, and only when the state is written anyway (a poll or a push).
SCREEN_PICTURE_SECONDS = 30

# the app's media session states (GET /media, /state.media)
MEDIA_STATES = {
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "buffering": MediaPlayerState.BUFFERING,
    "stopped": MediaPlayerState.IDLE,
}

KEY_FOR_FEATURE = {
    "play": MediaPlayerEntityFeature.PLAY,
    "pause": MediaPlayerEntityFeature.PAUSE,
    "stop": MediaPlayerEntityFeature.STOP,
    "next": MediaPlayerEntityFeature.NEXT_TRACK,
    "previous": MediaPlayerEntityFeature.PREVIOUS_TRACK,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the media player when the app has TV control (0.29.0)."""
    coordinator: PiPupCoordinator = entry.runtime_data
    if isinstance(coordinator.data.get("control"), dict):
        async_add_entities([PiPupMediaPlayer(coordinator, entry)])


def source_apps(apps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The apps worth listing as sources.

    A TV lists its TV apps (LEANBACK_LAUNCHER); phone-launcher entries are mostly
    system tools there. A device without any TV app (a plain-Android projector) lists
    everything. Duplicate labels keep the first, so a source name stays unambiguous.
    """
    tv = [a for a in apps if a.get("tv")]
    chosen = tv or apps
    seen: set[str] = set()
    result = []
    for app in chosen:
        label = str(app.get("label") or app.get("package"))
        if label in seen:
            continue
        seen.add(label)
        result.append({**app, "label": label})
    return result


class PiPupMediaPlayer(PiPupEntity, MediaPlayerEntity):
    """Remote control for the TV: apps, media keys, volume, screen."""

    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.TV

    def __init__(self, coordinator: PiPupCoordinator, entry) -> None:
        """Initialize the media player."""
        super().__init__(coordinator, entry, "media_player")
        self._entry = entry
        self._apps: list[dict[str, Any]] = []
        self._apps_read_at = None
        # (package, state, position ms, valid at) of the last real position change
        self._position: tuple[str, str, float, datetime] | None = None

    async def async_added_to_hass(self) -> None:
        """Read the app list once the entity is in place."""
        await super().async_added_to_hass()
        await self._async_refresh_apps()

    async def _async_refresh_apps(self, force: bool = False) -> None:
        now = dt_util.utcnow()
        if (
            not force
            and self._apps_read_at is not None
            and now - self._apps_read_at < APPS_REFRESH_INTERVAL
        ):
            return
        try:
            self._apps = source_apps(await self.coordinator.client.apps())
            self._apps_read_at = now
        except PiPupError as err:
            # a TV in standby (Fire TV sticks drop off the network): keep the old list
            _LOGGER.debug("%s: app list not read: %s", self.entity_id, err)

    def _handle_coordinator_update(self) -> None:
        """Re-read the app list now and then, piggybacking on state updates."""
        if self.coordinator.online and (
            self._apps_read_at is None
            or dt_util.utcnow() - self._apps_read_at >= APPS_REFRESH_INTERVAL
        ):
            self.hass.async_create_task(self._async_refresh_apps())
        super()._handle_coordinator_update()

    # ------------------------------------------------------------ state

    @property
    def _control(self) -> dict[str, Any]:
        value = self.coordinator.data.get("control")
        return value if isinstance(value, dict) else {}

    @property
    def _volume(self) -> dict[str, Any]:
        value = self.coordinator.data.get("volume")
        return value if isinstance(value, dict) else {}

    @property
    def _power(self) -> dict[str, Any]:
        value = self.coordinator.data.get("power")
        return value if isinstance(value, dict) else {}

    @property
    def _media(self) -> dict[str, Any]:
        """The app's media block: access, and the active session (app >= 0.32.0)."""
        value = self.coordinator.data.get("media")
        return value if isinstance(value, dict) else {}

    @property
    def _session(self) -> dict[str, Any] | None:
        """The active media session, or None (no access, nothing active, older app)."""
        media = self._media
        return media if media.get("access") is True and media.get("package") else None

    @property
    def _foreground(self) -> dict[str, Any] | None:
        """The app in front (app >= 0.33.0 with usage access), or None."""
        value = self.coordinator.data.get("foreground")
        return value if isinstance(value, dict) and value.get("package") else None

    def _label(self, package: str | None) -> str | None:
        """The source name of a package, when it is a known source."""
        if package is None:
            return None
        return next((a["label"] for a in self._apps if a.get("package") == package), None)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """What this TV can do; volume set only where the volume is not fixed."""
        features = (
            MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.SELECT_SOURCE
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.VOLUME_MUTE
        )
        if self._power.get("canSleep"):
            features |= MediaPlayerEntityFeature.TURN_OFF
        if self._volume and not self._volume.get("fixed"):
            features |= MediaPlayerEntityFeature.VOLUME_SET
        keys = self._control.get("keys") or []
        for key, feature in KEY_FOR_FEATURE.items():
            if key in keys:
                features |= feature
        session = self._session
        if self._media.get("access") is True and (
            session is None or session.get("canSeek") is not False
        ):
            features |= MediaPlayerEntityFeature.SEEK
        return features

    @property
    def state(self) -> MediaPlayerState | None:
        """On while the screen is on; off in standby (also when unreachable).

        Fire TV sticks drop off the network in standby, so unreachable reads as off.
        With a media session (app >= 0.32.0) a screen that is on shows the session's
        state instead: playing, paused, buffering, or idle when it stopped.
        """
        if not self.coordinator.online:
            return MediaPlayerState.OFF
        screen = self.coordinator.data.get("screenOn")
        if screen is None:
            return None
        if not screen:
            return MediaPlayerState.OFF
        session = self._session
        if session is not None:
            return MEDIA_STATES.get(session.get("state"), MediaPlayerState.ON)
        return MediaPlayerState.ON

    @property
    def volume_level(self) -> float | None:
        """Media volume 0..1."""
        level = self._volume.get("level")
        return level / 100 if isinstance(level, (int, float)) else None

    @property
    def is_volume_muted(self) -> bool | None:
        """Whether the media stream is muted."""
        muted = self._volume.get("muted")
        return bool(muted) if muted is not None else None

    @property
    def source_list(self) -> list[str]:
        """Installed apps, by label."""
        return [a["label"] for a in self._apps]

    @property
    def source(self) -> str | None:
        """The app in front; without usage access the app PiPup itself launched last.

        The last launch is not necessarily what is on screen: someone may have picked
        another app with the remote since.
        """
        front = self._foreground
        if front is not None:
            return self._label(front["package"]) or front.get("label") or front["package"]
        return self._label(self._control.get("lastLaunched"))

    @property
    def media_title(self) -> str | None:
        """Title of the active media session."""
        return (self._session or {}).get("title")

    @property
    def media_artist(self) -> str | None:
        """Artist of the active media session."""
        return (self._session or {}).get("artist")

    @property
    def media_album_name(self) -> str | None:
        """Album of the active media session."""
        return (self._session or {}).get("album")

    @property
    def media_duration(self) -> int | None:
        """Duration in seconds; None when the app does not report one."""
        duration = (self._session or {}).get("durationMs")
        return round(duration / 1000) if isinstance(duration, (int, float)) else None

    def _position_now(self) -> tuple[float, datetime] | None:
        """(position ms, valid at), kept from the last real change.

        A new reading replaces the kept one only when the app, the state or the
        position itself changed (a seek, a pause, more than POSITION_TOLERANCE_MS
        off the extrapolation), so the frontend's counter runs on smoothly and a poll
        writes no new state.
        """
        session = self._session or {}
        position = session.get("positionMs")
        stamp = session.get("positionUpdatedAt")
        at = dt_util.parse_datetime(stamp) if isinstance(stamp, str) else None
        if not isinstance(position, (int, float)) or at is None:
            self._position = None
            return None
        package, state = session.get("package"), session.get("state")
        kept = getattr(self, "_position", None)  # entities built without __init__ (tests)
        if kept is not None and kept[:2] == (package, state):
            speed = session.get("speed") if state == "playing" else 0
            speed = speed if isinstance(speed, (int, float)) else 1.0
            expected = kept[2] + (at - kept[3]).total_seconds() * 1000 * speed
            if abs(position - expected) <= POSITION_TOLERANCE_MS:
                return kept[2], kept[3]
        self._position = (package, state, float(position), at)
        return float(position), at

    @property
    def media_position(self) -> int | None:
        """Position in seconds at media_position_updated_at."""
        now = self._position_now()
        return round(now[0] / 1000) if now else None

    @property
    def media_position_updated_at(self) -> datetime | None:
        """When the position was valid (the frontend counts on from there)."""
        now = self._position_now()
        return now[1] if now else None

    @property
    def media_image_hash(self) -> str | None:
        """The app's key for the session's artwork (app >= 0.32.0-beta.4, ha-pipup #61).

        None without artwork, without a session, and with an older app: no picture then.
        The key changes with the picture, so HA's image proxy fetches it anew.
        """
        key = (self._session or {}).get("artKey")
        if key:
            return str(key)
        if self._screen_picture:
            # 1.33.0: the screen itself; the hash moves on so the picture is fetched anew
            return f"screen-{int(dt_util.utcnow().timestamp()) // SCREEN_PICTURE_SECONDS}"
        return None

    @property
    def _screen_picture(self) -> bool:
        """Whether the screen can stand in for artwork: switched on, and the screen is on."""
        entry = getattr(self, "_entry", None)  # entities built without __init__ (tests)
        if entry is None or screen_token(entry) is None:
            return False
        return (
            self.coordinator.online
            and bool(self.coordinator.data.get("screenOn"))
            and self._control.get("screen") is not False
        )

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """The artwork from the app (GET /media/art); HA serves it through its proxy."""
        if self.media_image_hash is None:
            return None, None
        if not (self._session or {}).get("artKey"):
            try:
                picture = await self.coordinator.client.screen(screen_token(self._entry))
            except PiPupError as err:
                _LOGGER.debug("%s: no picture of the screen: %s", self.entity_id, err)
                return None, None
            return (picture, "image/jpeg") if picture else (None, None)
        try:
            return await self.coordinator.client.media_art(package=self.app_id)
        except PiPupError as err:
            # a session that just ended, a TV that went to standby: no picture
            _LOGGER.debug("%s: artwork not read: %s", self.entity_id, err)
            return None, None

    @property
    def app_id(self) -> str | None:
        """The package that holds the media session; without one, the app in front."""
        session = self._session
        if session is not None:
            return session.get("package")
        return (self._foreground or {}).get("package")

    @property
    def app_name(self) -> str | None:
        """Name of that app: its source name, else what the app in front calls itself."""
        pkg = self.app_id
        if pkg is None:
            return None
        label = self._label(pkg)
        if label is None and self._session is None:
            label = (self._foreground or {}).get("label")
        return label

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the TV reports about its controls."""
        attrs = {
            "keys": self._control.get("keys"),
            "last_launched": self._control.get("lastLaunched"),
            "volume_fixed": self._volume.get("fixed"),
            "volume_steps": self._volume.get("max"),
        }
        # app >= 0.32.0: whether the ADB grants for PiPup #83 are in place
        media = self._media
        if media:
            attrs["media_access"] = (
                "granted" if media.get("access") is True
                else media.get("reason") or "not_granted"
            )
        if "click" in self._control:
            attrs["click_service"] = bool(self._control.get("click"))
            attrs["click_packages"] = self._control.get("clickPackages") or []
        return attrs

    # ------------------------------------------------------------ actions

    async def _call(self, coro) -> dict[str, Any]:
        try:
            result = await coro
        except PiPupUnsupportedError as err:
            raise HomeAssistantError(f"{self.entity_id}: this TV cannot do that: {err}") from err
        except PiPupError as err:
            raise HomeAssistantError(f"{self.entity_id}: {err}") from err
        await self.coordinator.async_refresh_soon()
        return result

    async def async_turn_on(self) -> None:
        """Wake the TV (over ADB as well when the app could not; 1.32.0b3)."""
        await self._call(self.coordinator.async_screen_on())

    async def async_turn_off(self) -> None:
        """Put the TV in standby."""
        await self._call(self.coordinator.client.power(POWER_STATE_OFF))

    async def async_select_source(self, source: str) -> None:
        """Start the app with this label (or package name)."""
        app = self._find_app(source)
        if app is None:
            await self._async_refresh_apps(force=True)
            app = self._find_app(source)
        package = app["package"] if app else source if "." in source else None
        if package is None:
            raise HomeAssistantError(f"{self.entity_id}: no app called '{source}'")
        await self._call(self.coordinator.client.launch(package=package))

    def _find_app(self, source: str) -> dict[str, Any] | None:
        return next(
            (a for a in self._apps if source in (a.get("label"), a.get("package"))), None
        )

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the media volume (not on a fixed-volume box)."""
        await self._call(self.coordinator.client.volume(level=round(volume * 100)))

    async def async_volume_up(self) -> None:
        """One volume step up."""
        await self._call(self.coordinator.client.volume(step="up"))

    async def async_volume_down(self) -> None:
        """One volume step down."""
        await self._call(self.coordinator.client.volume(step="down"))

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute the media stream."""
        await self._call(self.coordinator.client.volume(mute="on" if mute else "off"))

    # The transport buttons go through coordinator.async_send_key (1.32.0b4): the app
    # sends them to the media session (app >= 0.32.0-beta.4), or ADB presses the key
    # when the app sees no session (PiPup #83: Plex ignored the media-button event).

    async def async_media_play(self) -> None:
        """Send play."""
        await self._call(self.coordinator.async_send_key("play"))

    async def async_media_pause(self) -> None:
        """Send pause."""
        await self._call(self.coordinator.async_send_key("pause"))

    async def async_media_play_pause(self) -> None:
        """Send play/pause: PiPup does not know which of the two applies."""
        await self._call(self.coordinator.async_send_key("play_pause"))

    async def async_media_stop(self) -> None:
        """Send stop."""
        await self._call(self.coordinator.async_send_key("stop"))

    async def async_media_seek(self, position: float) -> None:
        """Seek the active media session to `position` seconds (app >= 0.32.0)."""
        await self._call(
            self.coordinator.client.seek(
                ms=max(0, round(position * 1000)), package=self.app_id
            )
        )

    async def async_media_next_track(self) -> None:
        """Send next."""
        await self._call(self.coordinator.async_send_key("next"))

    async def async_media_previous_track(self) -> None:
        """Send previous."""
        await self._call(self.coordinator.async_send_key("previous"))
