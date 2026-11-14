"""Camera: a picture of the TV's screen (app >= 0.33.0, PiPup #89).

Only there when the screenshot is switched on in the options (Advanced install): that
makes a token, sends it to the app over ADB and keeps it in the entry. The app takes one
picture per request, on Android 11 and up; protected video comes out black.

No picture while the screen is off or the TV is unreachable: the last one would show
something that is no longer there.
"""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import PiPupError
from .coordinator import PiPupCoordinator
from .entity import PiPupEntity, screen_token

_LOGGER = logging.getLogger(__name__)

# the app's range for width=
MIN_WIDTH = 160
MAX_WIDTH = 1920


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the screen camera when the screenshot is switched on."""
    if screen_token(entry) is not None:
        async_add_entities([PiPupScreenCamera(entry.runtime_data, entry)])


class PiPupScreenCamera(PiPupEntity, Camera):
    """What is on the TV's screen, one picture per request."""

    _attr_translation_key = "screen"

    def __init__(self, coordinator: PiPupCoordinator, entry) -> None:
        """Initialize the camera."""
        PiPupEntity.__init__(self, coordinator, entry, "screen_camera")
        Camera.__init__(self)
        self._entry = entry

    @property
    def available(self) -> bool:
        """Only while the TV answers; the app says in /state whether it can."""
        control = self.coordinator.data.get("control")
        can = control.get("screen") if isinstance(control, dict) else None
        return super().available and self.coordinator.online and can is not False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """One picture of the screen; None while the screen is off."""
        token = screen_token(self._entry)
        if token is None or not self.coordinator.data.get("screenOn"):
            return None
        if width is not None:
            width = max(MIN_WIDTH, min(MAX_WIDTH, int(width)))
        try:
            return await self.coordinator.client.screen(token, width)
        except PiPupError as err:
            # standby between the poll and this request, or a token the app no longer has
            _LOGGER.debug("%s: no picture of the screen: %s", self.entity_id, err)
            return None
