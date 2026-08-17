"""Repair flow for a PiPup TV that is missing its overlay permission."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .api import PiPupError, PiPupUnsupportedError


class OverlayPermissionRepairFlow(RepairsFlow):
    """Walks the user to the screen where the overlay permission is granted.

    Home Assistant cannot grant it and neither can the app: `appops` is shell/system
    territory. What the app *can* do is put the right system screen on the TV, so the
    fix here is "press this, then pick up the remote" - and on devices that have no such
    screen (Fire OS answers those intents with do-nothing placeholders) the flow says so
    and hands over the adb command instead of pretending.
    """

    def __init__(self, entry_id: str) -> None:
        """Initialize the flow."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ):
        """Start the flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ):
        """Ask the TV to show the overlay permission screen."""
        if user_input is None:
            return self.async_show_form(
                step_id="confirm", data_schema=vol.Schema({})
            )

        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")

        coordinator = entry.runtime_data
        try:
            await coordinator.client.fix_permission("overlay")
        except PiPupUnsupportedError:
            return self.async_abort(reason="use_adb")
        except PiPupError:
            return self.async_abort(reason="cannot_connect")

        # The permission is not granted yet - someone still has to confirm it on the TV.
        # Closing the issue here is fine: the coordinator raises it again on the next
        # poll if the device still reports the permission as missing.
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the flow for a repair issue."""
    entry_id = str((data or {}).get("entry_id", ""))
    return OverlayPermissionRepairFlow(entry_id)
