"""Stacking (app >= 0.27.0, PiPup #64): per-call value first, then the per-device default."""
from __future__ import annotations

from custom_components.pipup.services import build_device_payload


def _payload(data: dict, opts: dict | None = None) -> dict:
    return build_device_payload({"title": "t", **data}, opts or {}, None)


def test_no_stack_by_default() -> None:
    assert "stack" not in _payload({})


def test_per_call_stack_is_sent() -> None:
    assert _payload({"stack": "vertical"})["stack"] == "vertical"
    assert _payload({"stack": "horizontal"})["stack"] == "horizontal"


def test_device_default_applies() -> None:
    assert _payload({}, {"default_stack": "vertical"})["stack"] == "vertical"


def test_none_overrides_the_device_default() -> None:
    assert "stack" not in _payload({"stack": "none"}, {"default_stack": "horizontal"})
