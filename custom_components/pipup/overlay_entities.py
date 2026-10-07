"""Entities of an overlay: Show switch, settings (select, number, switch, text), status.

Each platform module calls the matching ``async_add_*`` helper here, once per overlay
subentry, so an overlay's entities belong to its subentry and go with it.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ANIMATIONS,
    OVERLAY_LAYOUT_CUSTOM,
    OVERLAY_LAYOUTS,
    OVERLAY_SOUNDS,
    POSITIONS,
)
from .entity import PiPupOverlayEntity
from .overlay import OverlayManager

# key: (min, max, step, unit)
_NUMBERS: dict[str, tuple[float, float, float, str | None]] = {
    "width": (100, 1920, 20, "px"),
    "height": (100, 1080, 20, "px"),
    "duration": (0, 240, 5, UnitOfTime.MINUTES),
    "padding": (0, 40, 2, "px"),
    "corner_radius": (0, 40, 2, "px"),
    "border_width": (0, 20, 1, "px"),
    "page_opacity": (10, 100, 5, PERCENTAGE),
    "background_opacity": (0, 100, 5, PERCENTAGE),
}

_SELECTS: dict[str, list[str] | None] = {
    "page": None,  # from the TV's page list
    "layout": [*OVERLAY_LAYOUTS, OVERLAY_LAYOUT_CUSTOM],
    "position": list(POSITIONS),
    "animation": list(ANIMATIONS),
    "sound": list(OVERLAY_SOUNDS),
}

# key: (max length, pattern)
_TEXTS: dict[str, tuple[int, str | None]] = {
    "custom_url": (255, None),
    "title": (60, None),
    "border_color": (7, r"^(#[0-9A-Fa-f]{6})?$"),
    "background_color": (7, r"^#[0-9A-Fa-f]{6}$"),
}

_TOGGLES = ("muted", "transparent", "bring_to_front")


def _each(manager: OverlayManager, add: AddEntitiesCallback, build) -> None:
    for sub_id in manager.subentry_ids:
        add(build(sub_id), config_subentry_id=sub_id)


def async_add_switches(manager: OverlayManager, add: AddEntitiesCallback) -> None:
    """Show switch plus the Muted, Transparent page and Redraw on top toggles."""
    _each(
        manager,
        add,
        lambda sub_id: [
            OverlayShowSwitch(manager, sub_id),
            *(OverlayToggle(manager, sub_id, key) for key in _TOGGLES),
        ],
    )


def async_add_selects(manager: OverlayManager, add: AddEntitiesCallback) -> None:
    """Page, Layout, Position, Animation, Sound."""
    _each(manager, add, lambda sub_id: [OverlaySelect(manager, sub_id, k) for k in _SELECTS])


def async_add_numbers(manager: OverlayManager, add: AddEntitiesCallback) -> None:
    """Size, time and look sliders."""
    _each(manager, add, lambda sub_id: [OverlayNumber(manager, sub_id, k) for k in _NUMBERS])


def async_add_texts(manager: OverlayManager, add: AddEntitiesCallback) -> None:
    """Custom URL, title and colors."""
    _each(manager, add, lambda sub_id: [OverlayText(manager, sub_id, k) for k in _TEXTS])


def async_add_sensors(manager: OverlayManager, add: AddEntitiesCallback) -> None:
    """Status."""
    _each(manager, add, lambda sub_id: [OverlayStatusSensor(manager, sub_id)])


class OverlayShowSwitch(PiPupOverlayEntity, SwitchEntity):
    """On while the overlay is on the TV; on shows it, off takes it away."""

    _attr_name = None  # the device's name: "<name> overlay"
    _attr_icon = "mdi:picture-in-picture-top-right"

    def __init__(self, manager: OverlayManager, sub_id: str) -> None:
        """Initialize the switch."""
        super().__init__(manager, sub_id, "show")

    @property
    def is_on(self) -> bool:
        """Whether the overlay is on the TV."""
        return self.manager.is_on(self.sub_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Popup id and URL, for automations and the dashboard."""
        return {
            "popup_id": self.manager.popup_id(self.sub_id),
            "url": self.manager.url(self.sub_id),
            "since": self.manager.since(self.sub_id),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Show the overlay."""
        await self.manager.async_show(self.sub_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Take the overlay off the TV."""
        await self.manager.async_hide(self.sub_id)


class OverlayToggle(PiPupOverlayEntity, SwitchEntity):
    """A yes/no setting of the overlay."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, manager: OverlayManager, sub_id: str, key: str) -> None:
        """Initialize the toggle."""
        super().__init__(manager, sub_id, key)
        self._key = key

    @property
    def is_on(self) -> bool:
        """Current value."""
        return bool(self._get(self._key))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set it."""
        await self._set(**{self._key: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Clear it."""
        await self._set(**{self._key: False})


class OverlaySelect(PiPupOverlayEntity, SelectEntity):
    """A pick-one setting of the overlay."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, manager: OverlayManager, sub_id: str, key: str) -> None:
        """Initialize the select."""
        super().__init__(manager, sub_id, key)
        self._key = key

    @property
    def options(self) -> list[str]:
        """Choices; the page list comes from the TV's options."""
        fixed = _SELECTS[self._key]
        return fixed if fixed is not None else self.manager.page_options()

    @property
    def current_option(self) -> str | None:
        """Current choice."""
        if self._key == "page":
            return self.manager.page(self.sub_id)
        value = self._get(self._key)
        return value if value in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Store the choice."""
        await self._set(**{self._key: option})


class OverlayNumber(PiPupOverlayEntity, NumberEntity):
    """A slider setting of the overlay."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER

    def __init__(self, manager: OverlayManager, sub_id: str, key: str) -> None:
        """Initialize the number."""
        super().__init__(manager, sub_id, key)
        self._key = key
        low, high, step, unit = _NUMBERS[key]
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float:
        """Current value."""
        return self._get(self._key)

    async def async_set_native_value(self, value: float) -> None:
        """Store the value (whole numbers: every field here is px, %, or minutes)."""
        await self._set(**{self._key: int(round(value))})


class OverlayText(PiPupOverlayEntity, TextEntity):
    """A text setting of the overlay; empty means none."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0

    def __init__(self, manager: OverlayManager, sub_id: str, key: str) -> None:
        """Initialize the text."""
        super().__init__(manager, sub_id, key)
        self._key = key
        self._attr_native_max, self._attr_pattern = _TEXTS[key]

    @property
    def native_value(self) -> str:
        """Current value."""
        return self._get(self._key) or ""

    async def async_set_value(self, value: str) -> None:
        """Store the value."""
        await self._set(**{self._key: value.strip()})


class OverlayStatusSensor(PiPupOverlayEntity, SensorEntity):
    """What the overlay is doing, and why it stopped."""

    _attr_icon = "mdi:information-outline"

    def __init__(self, manager: OverlayManager, sub_id: str) -> None:
        """Initialize the sensor."""
        super().__init__(manager, sub_id, "status")

    @property
    def native_value(self) -> str:
        """Status text."""
        return self.manager.status(self.sub_id)
