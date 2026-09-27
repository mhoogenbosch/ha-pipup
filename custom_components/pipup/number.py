"""Numbers: the settings of each overlay (davbebawy fork, 1.20.0)."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PiPupCoordinator
from .overlay_entities import async_add_numbers


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the overlay numbers."""
    coordinator: PiPupCoordinator = entry.runtime_data
    async_add_numbers(coordinator.overlays, async_add_entities)
