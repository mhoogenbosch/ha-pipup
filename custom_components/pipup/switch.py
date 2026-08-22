"""Switch: the TV's screen on/off (app >= 0.7.0)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import PiPupError, PiPupUnsupportedError
from .const import (
    POWER_OPTIMISTIC_TTL,
    POWER_SETTLE_DELAY,
    POWER_STATE_OFF,
    POWER_STATE_ON,
)
from .coordinator import PiPupCoordinator
from .entity import PiPupEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the screen switch, if the app supports /power."""
    coordinator: PiPupCoordinator = entry.runtime_data
    # Needs the fork app >= 0.7.0. Older apps have no /power at all, and a switch
    # whose every call fails is worse than no switch.
    if isinstance(coordinator.data.get("power"), dict):
        async_add_entities([PiPupScreenSwitch(coordinator, entry)])


class PiPupScreenSwitch(PiPupEntity, SwitchEntity):
    """Turns the TV's screen on (wake) and off (standby).

    Whether "off" is possible depends on a one-time adb grant on the device (device
    admin or the accessibility fallback); the app reports that as
    ``power.canSleep``. Rather than hiding the switch on devices that lack it, the
    capability is exposed as an attribute and turning off raises an error naming the
    fix — a hidden entity leaves you guessing why it never appeared.
    """

    _attr_translation_key = "screen_power"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: PiPupCoordinator, entry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry, "screen_power")
        self._optimistic: bool | None = None
        self._optimistic_until = dt_util.utcnow()
        self._cancel_settle: callable | None = None

    @property
    def _power(self) -> dict[str, Any]:
        value = self.coordinator.data.get("power")
        return value if isinstance(value, dict) else {}

    @property
    def is_on(self) -> bool | None:
        """Return whether the screen is on; unknown when unreachable.

        The device reads its own state from PowerManager.isInteractive, which lags a
        poll behind a fresh wake, so a just-issued state is trusted briefly - until
        the device confirms it, or the window expires.
        """
        reported = self.coordinator.data.get("screenOn")
        if self._optimistic is not None:
            if dt_util.utcnow() > self._optimistic_until or reported is self._optimistic:
                self._optimistic = None
            else:
                return self._optimistic
        if not self.coordinator.online:
            return None
        return bool(reported) if reported is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose what this device can actually do with its screen."""
        power = self._power
        return {
            "can_sleep": power.get("canSleep"),
            "sleep_method": power.get("sleepMethod"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Wake the TV."""
        await self._set_power(POWER_STATE_ON)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Put the TV in standby."""
        await self._set_power(POWER_STATE_OFF)

    async def _set_power(self, state: str) -> None:
        try:
            await self.coordinator.client.power(state)
        except PiPupUnsupportedError as err:
            raise HomeAssistantError(
                f"{self.entity_id}: this device has no granted way to switch its "
                "screen off. Grant one over adb (see the PiPup readme): "
                "`adb shell dpm set-active-admin nl.rogro82.pipup/.AdminReceiver`, "
                "or enable the accessibility fallback. "
                f"({err})"
            ) from err
        except PiPupError as err:
            raise HomeAssistantError(f"{self.entity_id}: {err}") from err

        self._optimistic = state == POWER_STATE_ON
        self._optimistic_until = dt_util.utcnow() + POWER_OPTIMISTIC_TTL
        self.async_write_ha_state()

        await self.coordinator.async_refresh_soon()
        # ...and once more after the device settled, so the optimistic value is
        # replaced by a measured one instead of just timing out. Cancel any
        # earlier pending settle first, and cancel on removal: a timer that
        # outlives the entity would poke an unloaded coordinator.
        if self._cancel_settle is not None:
            self._cancel_settle()
        self._cancel_settle = async_call_later(
            self.hass, POWER_SETTLE_DELAY, self._settle
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending settle refresh."""
        if self._cancel_settle is not None:
            self._cancel_settle()
            self._cancel_settle = None
        await super().async_will_remove_from_hass()

    @callback
    def _settle(self, _now) -> None:
        self._cancel_settle = None
        self.hass.async_create_task(self.coordinator.async_refresh_soon())
