"""Overlays: named web popups pinned over the TV picture (1.20.0).

Each overlay is a config subentry of the TV entry. Its settings are entities (page,
size, look) kept in one Store per entry; the Show switch reads the pushed /state, so
it is on exactly while the overlay's popup is on the TV. Nothing polls: a replaced,
expired or closed popup arrives as a push and turns the switch off. With app >= 0.24.0
several popups are up at once, so another popup no longer replaces an overlay (1.21.0).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import PiPupError
from .const import (
    CONF_OVERLAY_PAGES,
    CONF_OVERLAY_POPUP_ID,
    DOMAIN,
    OVERLAY_CUSTOM_PAGE,
    OVERLAY_DEFAULTS,
    OVERLAY_LAYOUT_CUSTOM,
    OVERLAY_LAYOUTS,
    OVERLAY_OPTIMISTIC_SECONDS,
    OVERLAY_REDRAW_DELAY,
    POSITIONS,
    SIGNAL_OVERLAY_UPDATED,
    SUBENTRY_OVERLAY,
)
from .coordinator import PiPupCoordinator, popup_ids

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
# why the app removed a popup (push `reason`), as the status says it
_REASONS = {
    "expired": "time up",
    "cancelled": "closed",
    "button": "closed by a button",
    "back": "closed with Back on the remote",
    "watchdog": "cleaned up by the app",
}


def parse_pages(text: str | None) -> dict[str, str]:
    """Read the options' page list: one "Name | URL" line each, in order."""
    pages: dict[str, str] = {}
    for line in (text or "").splitlines():
        name, sep, url = line.partition("|")
        name, url = name.strip(), url.strip()
        if sep and name and url and name != OVERLAY_CUSTOM_PAGE:
            pages[name] = url
    return pages


def overlay_subentries(entry: ConfigEntry) -> list[ConfigSubentry]:
    """The entry's overlay subentries."""
    return [
        sub for sub in entry.subentries.values() if sub.subentry_type == SUBENTRY_OVERLAY
    ]


def _now_text() -> str:
    return dt_util.now().strftime("%-I:%M %p")


class OverlayManager:
    """Settings, state and popup calls for every overlay of one TV."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, coordinator: PiPupCoordinator
    ) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._store: Store[dict[str, dict[str, Any]]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.overlays.{entry.entry_id}"
        )
        self._settings: dict[str, dict[str, Any]] = {}
        self._status: dict[str, str] = {}
        self._since: dict[str, str | None] = {}
        self._up: dict[str, bool] = {}
        # sub_id -> (wanted on/off, monotonic deadline) until the TV confirms
        self._pending: dict[str, tuple[bool, float]] = {}
        self._redraw: dict[str, Any] = {}
        # the overlay HA is hiding itself, so its status says "hidden", not "closed"
        self._hiding: set[str] = set()
        self.subentry_ids: set[str] = set()
        self.tv_device_id: str | None = None

    # ---- setup ---------------------------------------------------------------

    async def async_setup(self) -> None:
        """Load the settings and start following the pushed state."""
        # the TV's device first, so each overlay's device can hang under it
        self.tv_device_id = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.entry.unique_id or self.entry.entry_id)},
            name=self.entry.title,
        ).id
        stored = await self._store.async_load() or {}
        subs = overlay_subentries(self.entry)
        self.subentry_ids = {sub.subentry_id for sub in subs}
        # settings of removed overlays go; new overlays start from the defaults
        self._settings = {
            sub_id: {**OVERLAY_DEFAULTS, **stored.get(sub_id, {})}
            for sub_id in self.subentry_ids
        }
        for sub in subs:
            up = self._is_up_now(sub.subentry_id)
            self._up[sub.subentry_id] = up
            self._status[sub.subentry_id] = (
                f"Showing since {_now_text()}" if up else "Off"
            )
            self._since[sub.subentry_id] = dt_util.utcnow().isoformat() if up else None
        self._schedule_save()
        self.entry.async_on_unload(self.coordinator.async_add_listener(self._on_state))
        self.entry.async_on_unload(self._cancel_all)

    @callback
    def _cancel_all(self) -> None:
        for cancel in self._redraw.values():
            cancel()
        self._redraw.clear()

    # ---- reading -------------------------------------------------------------

    def subentry(self, sub_id: str) -> ConfigSubentry:
        """The subentry of an overlay."""
        return self.entry.subentries[sub_id]

    def popup_id(self, sub_id: str) -> str:
        """The popup id this overlay draws with."""
        return self.subentry(sub_id).data[CONF_OVERLAY_POPUP_ID]

    @property
    def pages(self) -> dict[str, str]:
        """This TV's page list, name -> URL."""
        return parse_pages(self.entry.options.get(CONF_OVERLAY_PAGES))

    def page_options(self) -> list[str]:
        """Names for the Page select: the list, then Custom URL."""
        return [*self.pages, OVERLAY_CUSTOM_PAGE]

    def get(self, sub_id: str, key: str) -> Any:
        """One setting of an overlay."""
        return self._settings.get(sub_id, OVERLAY_DEFAULTS).get(key)

    def page(self, sub_id: str) -> str | None:
        """The selected page, or the first one when none was picked yet."""
        page = self.get(sub_id, "page")
        options = self.page_options()
        if page in options:
            return page
        return options[0] if page is None else None

    def url(self, sub_id: str) -> str | None:
        """The URL the overlay shows."""
        page = self.page(sub_id)
        if page == OVERLAY_CUSTOM_PAGE:
            return self.get(sub_id, "custom_url") or None
        return self.pages.get(page) if page else None

    def status(self, sub_id: str) -> str:
        """Human text: what the overlay is doing and why."""
        return self._status.get(sub_id, "Off")

    def since(self, sub_id: str) -> str | None:
        """When the popup went up (ISO time), while it is up."""
        return self._since.get(sub_id)

    def is_on(self, sub_id: str) -> bool:
        """True while the overlay's popup is on the TV (or was just asked for)."""
        pending = self._pending.get(sub_id)
        if pending is not None:
            wanted, deadline = pending
            if self.hass.loop.time() < deadline and wanted != self._up.get(sub_id):
                return wanted
            self._pending.pop(sub_id, None)
        return self._up.get(sub_id, False)

    def _is_up_now(self, sub_id: str) -> bool:
        return self.popup_id(sub_id) in popup_ids(self.coordinator.data)

    # ---- writing settings -----------------------------------------------------

    async def async_set(self, sub_id: str, **values: Any) -> None:
        """Change settings; a change while the overlay is up redraws it."""
        settings = self._settings.setdefault(sub_id, dict(OVERLAY_DEFAULTS))
        layout_keys = {"width", "height", "position"}
        if "layout" in values and values["layout"] in OVERLAY_LAYOUTS:
            width, height, position = OVERLAY_LAYOUTS[values["layout"]]
            values = {"width": width, "height": height, "position": position, **values}
        elif layout_keys & values.keys() and settings.get("layout") in OVERLAY_LAYOUTS:
            merged = {**settings, **values}
            preset = OVERLAY_LAYOUTS[settings["layout"]]
            if (merged["width"], merged["height"], merged["position"]) != preset:
                values = {**values, "layout": OVERLAY_LAYOUT_CUSTOM}
        changed = {k: v for k, v in values.items() if settings.get(k) != v}
        if not changed:
            return
        settings.update(changed)
        self._schedule_save()
        self._notify(sub_id)
        # "Redraw on top" says how the next redraw goes; it changes nothing on screen
        if self.is_on(sub_id) and changed.keys() - {"bring_to_front"}:
            self._schedule_redraw(sub_id)

    def _schedule_save(self) -> None:
        self._store.async_delay_save(lambda: self._settings, 2)

    def _schedule_redraw(self, sub_id: str) -> None:
        if cancel := self._redraw.pop(sub_id, None):
            cancel()

        @callback
        def _fire(_now) -> None:
            self._redraw.pop(sub_id, None)
            self.hass.async_create_task(self._async_redraw(sub_id))

        self._redraw[sub_id] = async_call_later(self.hass, OVERLAY_REDRAW_DELAY, _fire)

    async def _async_redraw(self, sub_id: str) -> None:
        try:
            await self.async_show(sub_id)
        except HomeAssistantError as err:
            _LOGGER.warning("PiPup overlay %s: redraw failed: %s",
                            self.subentry(sub_id).title, err)

    # ---- popup calls ----------------------------------------------------------

    def payload(self, sub_id: str) -> dict[str, Any]:
        """The /notify body for this overlay."""
        s = self._settings.get(sub_id, OVERLAY_DEFAULTS)
        url = self.url(sub_id)
        alpha = round(max(0, min(100, s["background_opacity"])) * 2.55)
        background = (s["background_color"] or "#000000").lstrip("#")
        payload: dict[str, Any] = {
            "id": self.popup_id(sub_id),
            "duration": int(s["duration"]) * 60,
            "position": POSITIONS.get(s["position"], 0),
            "padding": int(s["padding"]),
            "opacity": round(s["page_opacity"] / 100, 2),
            "cornerRadius": s["corner_radius"],
            "borderWidth": int(s["border_width"]),
            "backgroundColor": f"#{alpha:02X}{background}",
            "media": {
                "web": {
                    "uri": url,
                    "width": int(s["width"]),
                    "height": int(s["height"]),
                    "muted": bool(s["muted"]),
                }
            },
        }
        if s["transparent"]:
            payload["media"]["web"]["transparent"] = True
        if s["bring_to_front"]:
            payload["bringToFront"] = True
        if s["title"]:
            payload["title"] = s["title"]
        if s["border_color"]:
            payload["borderColor"] = s["border_color"]
        if s["animation"] != "none":
            payload["animation"] = s["animation"]
        if s["sound"] == "chime":
            payload["sound"] = "default"
        return payload

    async def async_show(self, sub_id: str) -> None:
        """Draw (or redraw) the overlay on the TV."""
        if not self.url(sub_id):
            self._set_status(sub_id, "No page: pick one, or set the custom URL")
            raise HomeAssistantError(
                f"{self.subentry(sub_id).title} overlay: no page URL"
            )
        self._pending[sub_id] = (True, self.hass.loop.time() + OVERLAY_OPTIMISTIC_SECONDS)
        self._notify(sub_id)
        try:
            await self.coordinator.client.notify(self.payload(sub_id))
        except PiPupError as err:
            self._pending.pop(sub_id, None)
            self._set_status(sub_id, f"TV did not answer at {_now_text()}")
            raise HomeAssistantError(f"{self.subentry(sub_id).title} overlay: {err}") from err
        await self.coordinator.async_refresh_soon()

    async def async_hide(self, sub_id: str) -> None:
        """Take the overlay off the TV (only its own popup)."""
        if cancel := self._redraw.pop(sub_id, None):
            cancel()
        self._pending[sub_id] = (False, self.hass.loop.time() + OVERLAY_OPTIMISTIC_SECONDS)
        if not self._up.get(sub_id):
            self._set_status(sub_id, "Off")
            return
        self._hiding.add(sub_id)
        self._notify(sub_id)
        try:
            await self.coordinator.client.cancel(self.popup_id(sub_id))
        except PiPupError as err:
            self._hiding.discard(sub_id)
            self._pending.pop(sub_id, None)
            self._set_status(sub_id, f"TV did not answer at {_now_text()}")
            raise HomeAssistantError(f"{self.subentry(sub_id).title} overlay: {err}") from err
        await self.coordinator.async_refresh_soon()

    # ---- following the TV -----------------------------------------------------

    @callback
    def _on_state(self) -> None:
        """Compare every overlay with the state the TV just pushed (or was read)."""
        data = self.coordinator.data or {}
        # an app before 0.24.0 shows one popup: another one on screen replaced ours
        one_at_a_time = "popups" not in data
        popup_id = (data.get("popup") or {}).get("id") if data.get("visible") else None
        event = self.coordinator.last_event or {}
        for sub_id in list(self.subentry_ids):
            up = self._is_up_now(sub_id)
            was_up = self._up.get(sub_id, False)
            self._up[sub_id] = up
            if up == was_up:
                continue
            self._pending.pop(sub_id, None)
            if up:
                self._since[sub_id] = dt_util.utcnow().isoformat()
                self._set_status(sub_id, f"Showing since {_now_text()}")
                continue
            self._since[sub_id] = None
            ours = self.popup_id(sub_id)
            if sub_id in self._hiding:
                self._hiding.discard(sub_id)
                text = f"Hidden at {_now_text()}"
            elif one_at_a_time and popup_id:
                text = f"Replaced by popup {popup_id} at {_now_text()}"
            elif event.get("removedId") == ours and event.get("reason"):
                reason = _REASONS.get(event["reason"], event["reason"])
                text = f"Off: {reason} at {_now_text()}"
            else:
                text = f"Gone from the TV at {_now_text()}"
            self._set_status(sub_id, text)

    def _set_status(self, sub_id: str, text: str) -> None:
        self._status[sub_id] = text
        self._notify(sub_id)

    @callback
    def async_pages_changed(self) -> None:
        """The page list changed in the options: refresh every Page select."""
        for sub_id in self.subentry_ids:
            self._notify(sub_id)

    def _notify(self, sub_id: str) -> None:
        async_dispatcher_send(self.hass, SIGNAL_OVERLAY_UPDATED.format(sub_id))
