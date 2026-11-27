"""TV control: source list, media player features and the launch/key schemas."""
from unittest.mock import MagicMock

import pytest
import voluptuous as vol
from homeassistant.components.media_player import MediaPlayerEntityFeature as F, MediaPlayerState

from custom_components.pipup.media_player import PiPupMediaPlayer, source_apps
from custom_components.pipup.services import LAUNCH_APP_SCHEMA, SEND_KEY_SCHEMA


def test_source_apps_prefers_tv_apps_and_dedups_labels() -> None:
    apps = [
        {"package": "com.netflix.ninja", "label": "Netflix", "tv": True},
        {"package": "com.android.settings", "label": "Settings", "tv": False},
        {"package": "com.other.netflix", "label": "Netflix", "tv": True},
    ]
    assert [a["package"] for a in source_apps(apps)] == ["com.netflix.ninja"]
    # no TV launcher at all (projector): everything
    phone = [{"package": "a.b", "label": "A", "tv": False}]
    assert source_apps(phone) == phone


def _player(data: dict, online: bool = True) -> PiPupMediaPlayer:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.online = online
    player = PiPupMediaPlayer.__new__(PiPupMediaPlayer)
    player.coordinator = coordinator
    player._apps = [{"package": "com.netflix.ninja", "label": "Netflix", "tv": True}]
    return player


BASE = {
    "screenOn": True,
    "power": {"canSleep": False},
    "control": {"launch": True, "keys": ["play_pause", "play", "pause", "next"], "lastLaunched": "com.netflix.ninja"},
    "volume": {"level": 40, "muted": False, "fixed": False, "max": 15},
}


def test_features_follow_the_device() -> None:
    features = _player(BASE).supported_features
    assert features & F.VOLUME_SET and features & F.PLAY and features & F.NEXT_TRACK
    assert not features & F.TURN_OFF          # no screen-off route granted
    assert not features & F.PREVIOUS_TRACK    # not in the key list

    fixed = {**BASE, "volume": {**BASE["volume"], "fixed": True}, "power": {"canSleep": True}}
    features = _player(fixed).supported_features
    assert not features & F.VOLUME_SET
    assert features & F.VOLUME_STEP and features & F.TURN_OFF


def test_state_volume_and_source() -> None:
    player = _player(BASE)
    assert player.state == MediaPlayerState.ON
    assert player.volume_level == 0.4
    assert player.source == "Netflix"
    assert _player({**BASE, "screenOn": False}).state == MediaPlayerState.OFF
    assert _player(BASE, online=False).state == MediaPlayerState.OFF


def test_service_schemas() -> None:
    assert LAUNCH_APP_SCHEMA({"package": "com.netflix.ninja"})["wake"] is True
    with pytest.raises(vol.Invalid):
        LAUNCH_APP_SCHEMA({"package": "rm -rf /"})
    assert SEND_KEY_SCHEMA({"key": "play_pause"})["key"] == "play_pause"
    # 2.1.0b1 (PiPup #95): "power" is a key now; a made-up name is still refused
    with pytest.raises(vol.Invalid):
        SEND_KEY_SCHEMA({"key": "launch_rockets"})
