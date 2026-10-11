"""Positions: the ordinal table is the wire contract with the app, and new ones are gated."""
from __future__ import annotations

from custom_components.pipup.const import POSITION_MIN_APP_VERSION, POSITIONS
from custom_components.pipup.services import unsupported_position


def test_ordinals_follow_the_app_enum() -> None:
    """Values must be 0..n-1 in declaration order — the app maps them by ordinal."""
    assert list(POSITIONS.values()) == list(range(len(POSITIONS)))
    assert list(POSITIONS) == [
        "top_right",
        "top_left",
        "bottom_right",
        "bottom_left",
        "center",
        "top_center",
        "bottom_center",
        "center_left",
        "center_right",
    ]


def test_every_gated_position_exists() -> None:
    assert set(POSITION_MIN_APP_VERSION) <= set(POSITIONS)


def test_center_side_positions_need_app_0_26() -> None:
    assert unsupported_position("center_left", "0.25.2") == "0.26.0"
    assert unsupported_position("center_right", "0.25.2") == "0.26.0"
    assert unsupported_position("center_left", "0.26.0") is None
    assert unsupported_position("center_right", "1.0.0") is None


def test_original_positions_are_never_gated() -> None:
    for name in ("top_right", "top_left", "bottom_right", "bottom_left", "center"):
        assert unsupported_position(name, "0.1.0") is None


def test_unknown_app_version_is_let_through() -> None:
    assert unsupported_position("center_left", None) is None
