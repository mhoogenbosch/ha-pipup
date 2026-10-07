"""Link the PiPup device to the TV it runs on (device page: "connected via <TV>").

PiPup is an app on an Android TV, so its device belongs under the TV's own device.
The TV is found among the devices of the Android TV integrations configured on the
same IP address. A TV usually has several integrations on that address (ADB, the
remote protocol, notifications, AirPlay), so only the integrations that represent
the TV itself are considered, in a fixed order of preference. The user can also pick
the parent in the options; that choice always wins.

Runs at setup, when a device is added to the registry (a TV integration set up
later), and when the options change. Never on a poll or push.

Idea from PR #31 by @aarya123.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import CONF_PARENT_DEVICE, DOMAIN

_LOGGER = logging.getLogger(__name__)

# integrations whose device IS the TV, most specific first
PARENT_DOMAINS: tuple[str, ...] = ("androidtv_remote", "androidtv")


@callback
def async_find_parent(hass: HomeAssistant, host: str | None) -> str | None:
    """Return the device id of the TV on `host`, or None."""
    if not host:
        return None
    registry = dr.async_get(hass)
    candidates: list[tuple[int, str, str]] = []
    for rank, domain in enumerate(PARENT_DOMAINS):
        for other in hass.config_entries.async_entries(domain, include_ignore=False):
            if (other.data or {}).get(CONF_HOST) != host:
                continue
            for device in dr.async_entries_for_config_entry(registry, other.entry_id):
                created = device.created_at.isoformat() if device.created_at else ""
                candidates.append((rank, created, device.id))
    # deterministic: preferred integration first, then the oldest device
    return min(candidates)[2] if candidates else None


@callback
def async_link_parent(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set (or clear) via_device_id on this entry's device."""
    from . import _async_get_own_device  # local: __init__ imports this module

    registry = dr.async_get(hass)
    device = _async_get_own_device(
        registry, (DOMAIN, entry.unique_id or entry.entry_id), entry.entry_id
    )
    if device is None:
        return
    chosen = entry.options.get(CONF_PARENT_DEVICE) or None
    if chosen and (chosen == device.id or registry.async_get(chosen) is None):
        chosen = None  # itself, or a device that no longer exists: fall back to auto
    parent = chosen or async_find_parent(hass, entry.data.get(CONF_HOST))
    if device.via_device_id != parent:
        registry.async_update_device(device.id, via_device_id=parent)
        _LOGGER.debug("%s: parent device %s", entry.title, parent)


@callback
def async_track_parent(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Link now, and again whenever a device is added to the registry."""
    async_link_parent(hass, entry)

    @callback
    def _is_create(data: dr.EventDeviceRegistryUpdatedData) -> bool:
        return data["action"] == "create"

    @callback
    def _created(event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
        async_link_parent(hass, entry)

    entry.async_on_unload(
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED, _created, event_filter=_is_create
        )
    )
