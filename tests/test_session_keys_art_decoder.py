"""1.32.0b4: media keys to the session or over ADB (PiPup #83), artwork (ha-pipup #61),
the video decoder (ha-pipup #60) and the poster mode (PiPup #87)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.pipup.adb import ADB_MEDIA_KEYS, AdbError
from custom_components.pipup.api import PiPupClient, PiPupError, PiPupUnsupportedError
from custom_components.pipup.const import DOMAIN
from custom_components.pipup.coordinator import PiPupCoordinator
from custom_components.pipup.services import SHOW_SCHEMA, build_device_payload

from .test_adb import STATE, _section, _setup
from .test_media_click import BASE_URL, PLEX, SESSION, TV, _player, tvs  # noqa: F401 - fixture

ADB = {"install_method": "adb"}
NO_SESSION = {"access": True, "package": None}


# ------------------------------------------------------------------ key routing


def _coordinator(data: dict, options: dict) -> PiPupCoordinator:
    coordinator = PiPupCoordinator.__new__(PiPupCoordinator)
    coordinator.hass = MagicMock()
    coordinator.data = data
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.options = options
    coordinator.config_entry.title = "TV"
    coordinator.client = MagicMock()
    coordinator.client.host = "192.168.1.21"
    coordinator.client.key = AsyncMock(return_value={"ok": True, "via": "session"})
    return coordinator


async def test_session_goes_to_the_app_even_with_adb() -> None:
    """With a session the app reaches the player itself (transport controls)."""
    coordinator = _coordinator({**TV, "media": SESSION}, ADB)
    key = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", key):
        answer = await coordinator.async_send_key("pause")
    coordinator.client.key.assert_awaited_once_with("pause")
    key.assert_not_awaited()
    assert answer["via"] == "session"


@pytest.mark.parametrize("data", [{**TV, "media": NO_SESSION}, dict(TV)])  # nothing, older app
async def test_no_session_with_adb_presses_the_key_over_adb(data) -> None:
    coordinator = _coordinator(data, ADB)
    key = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", key):
        answer = await coordinator.async_send_key("play_pause")
    coordinator.client.key.assert_not_awaited()
    key.assert_awaited_once()
    assert key.await_args.args[1:] == ("192.168.1.21", ADB, "KEYCODE_MEDIA_PLAY_PAUSE")
    assert answer == {"ok": True, "message": "sent play_pause over ADB", "via": "adb"}


async def test_no_session_without_adb_goes_to_the_app() -> None:
    """No tested ADB setup: never open ADB (it could show "Allow debugging?")."""
    coordinator = _coordinator({**TV, "media": NO_SESSION}, {})
    key = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", key):
        await coordinator.async_send_key("play")
    coordinator.client.key.assert_awaited_once_with("play")
    key.assert_not_awaited()


async def test_home_and_back_always_go_to_the_app() -> None:
    coordinator = _coordinator({**TV, "media": NO_SESSION}, ADB)
    key = AsyncMock()
    with patch("custom_components.pipup.coordinator.async_adb_key", key):
        await coordinator.async_send_key("home")
    coordinator.client.key.assert_awaited_once_with("home")
    key.assert_not_awaited()


async def test_adb_failure_is_reported() -> None:
    coordinator = _coordinator({**TV, "media": NO_SESSION}, ADB)
    with patch(
        "custom_components.pipup.coordinator.async_adb_key",
        AsyncMock(side_effect=AdbError("unreachable")),
    ), pytest.raises(PiPupError, match="stop over ADB failed: unreachable"):
        await coordinator.async_send_key("stop")


def test_every_app_media_key_has_an_adb_key() -> None:
    assert set(ADB_MEDIA_KEYS) == {
        "play_pause", "play", "pause", "stop", "next", "previous", "rewind", "fast_forward",
    }
    assert all(v.startswith("KEYCODE_MEDIA_") for v in ADB_MEDIA_KEYS.values())


@pytest.mark.parametrize(
    ("method", "key"),
    [
        ("async_media_play", "play"),
        ("async_media_pause", "pause"),
        ("async_media_play_pause", "play_pause"),
        ("async_media_stop", "stop"),
        ("async_media_next_track", "next"),
        ("async_media_previous_track", "previous"),
    ],
)
async def test_player_buttons_use_the_routing(method: str, key: str) -> None:
    player = _player({**TV, "media": SESSION})
    player.coordinator.async_send_key = AsyncMock(return_value={"ok": True})
    await getattr(player, method)()
    player.coordinator.async_send_key.assert_awaited_once_with(key)


async def test_send_key_service_uses_the_routing(hass: HomeAssistant, tvs) -> None:  # noqa: F811
    sent: list[tuple[str, str]] = []

    async def fake(self, key):
        sent.append((self.config_entry.title, key))
        return {"ok": True}

    with patch.object(PiPupCoordinator, "async_send_key", fake):
        await hass.services.async_call(DOMAIN, "send_key", {"key": "pause"}, blocking=True)
    assert sorted(sent) == [("New TV", "pause"), ("Prev TV", "pause")]


# ------------------------------------------------------------------ artwork


def test_artwork_hash_follows_the_app() -> None:
    assert _player({**TV, "media": {**SESSION, "artKey": "9f3a1c"}}).media_image_hash == "9f3a1c"
    # an app before 0.32.0-beta.4 has no artKey; no session or no artwork: no picture
    assert _player({**TV, "media": SESSION}).media_image_hash is None
    assert _player({**TV, "media": {**SESSION, "artKey": None}}).media_image_hash is None
    assert _player({**TV, "media": {**NO_SESSION, "artKey": "x"}}).media_image_hash is None


async def test_artwork_is_read_from_the_app() -> None:
    player = _player({**TV, "media": {**SESSION, "artKey": "9f3a1c"}})
    player.coordinator.client.media_art = AsyncMock(return_value=(b"\xff\xd8jpeg", "image/jpeg"))
    assert await player.async_get_media_image() == (b"\xff\xd8jpeg", "image/jpeg")
    player.coordinator.client.media_art.assert_awaited_once_with(package=PLEX)


async def test_no_artwork_without_a_key_or_when_reading_fails() -> None:
    old = _player({**TV, "media": SESSION})
    old.coordinator.client.media_art = AsyncMock()
    assert await old.async_get_media_image() == (None, None)
    old.coordinator.client.media_art.assert_not_awaited()

    gone = _player({**TV, "media": {**SESSION, "artKey": "9f3a1c"}})
    gone.coordinator.client.media_art = AsyncMock(side_effect=PiPupError("session ended"))
    assert await gone.async_get_media_image() == (None, None)


@pytest.fixture
async def client(hass: HomeAssistant, aioclient_mock) -> PiPupClient:
    return PiPupClient(async_get_clientsession(hass), "192.168.1.21", 7979)


async def test_client_media_art(client, aioclient_mock) -> None:
    aioclient_mock.get(
        f"{BASE_URL}/media/art", content=b"\xff\xd8jpeg", headers={"Content-Type": "image/jpeg"}
    )
    assert await client.media_art(package=PLEX) == (b"\xff\xd8jpeg", "image/jpeg")
    assert dict(aioclient_mock.mock_calls[0][1].query) == {"package": PLEX}

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}/media/art", status=404, json={"ok": False, "message": "no artwork"})
    assert await client.media_art() == (None, None)


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [
        (501, {"ok": False, "message": "media session access is not granted"}, "not granted"),
        (400, "invalid request: unknown uri: /media/art", "needs >= 0.32.0-beta.4"),
    ],
)
async def test_client_media_art_unsupported(client, aioclient_mock, status, body, match) -> None:
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
    aioclient_mock.get(f"{BASE_URL}/media/art", status=status, **kwargs)
    with pytest.raises(PiPupUnsupportedError, match=match):
        await client.media_art()


# ------------------------------------------------------------------ video decoder


def _video(extra: dict | None = None, opts: dict | None = None, **kwargs) -> dict:
    data = SHOW_SCHEMA({"title": "Doorbell", "video_url": "rtsp://cam/sub", **(extra or {})})
    return build_device_payload(data, opts or {}, None, **kwargs)["media"]


@pytest.mark.parametrize(
    ("extra", "opts", "expected"),
    [
        ({}, {}, None),  # nothing set: the app's own rule
        ({}, {"default_video_decoder": "auto"}, None),
        ({}, {"default_video_decoder": "software"}, True),
        ({}, {"default_video_decoder": "hardware"}, False),
        ({"software_decoder": True}, {}, True),
        ({"software_decoder": False}, {"default_video_decoder": "software"}, False),  # call wins
        ({"software_decoder": True}, {"default_video_decoder": "hardware"}, True),
    ],
)
def test_software_decoder(extra, opts, expected) -> None:
    video = _video(extra, opts)["video"]
    assert video.get("softwareDecoder", None) is expected
    assert ("softwareDecoder" in video) is (expected is not None)


def test_software_decoder_for_a_camera_stream() -> None:
    media = build_device_payload(
        {"title": "Doorbell"}, {"default_video_decoder": "software"}, "rtsp://go2rtc/doorbell"
    )["media"]
    assert media["video"]["softwareDecoder"] is True


def test_software_decoder_only_for_the_video_route() -> None:
    """WHEP, web and the grid play in a WebView: no decoder to choose."""
    opts = {"default_video_decoder": "software"}
    for extra in (
        {"whep_url": "http://g/api/webrtc?src=x"},
        {"web_url": "http://g/stream.html?src=x"},
        {"grid": ["http://g/api/webrtc?src=a", "http://g/api/webrtc?src=b"]},
        {"image_url": "http://g/latest.jpg"},
    ):
        data = SHOW_SCHEMA({"title": "t", "software_decoder": True, **extra})
        media = build_device_payload(data, opts, None)["media"]
        kind = next(iter(media))
        assert "softwareDecoder" not in media[kind], kind


async def test_options_save_the_video_decoder(hass: HomeAssistant) -> None:
    entry, patchers = await _setup(hass, dict(STATE))
    try:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        schema = result["data_schema"].schema
        field = next(k for k in schema if k == "default_video_decoder")
        assert field.default() == "auto"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"default_video_decoder": "software", **_section()}
        )
        assert result["type"] == "create_entry"
        assert entry.options["default_video_decoder"] == "software"
        with pytest.raises(vol.Invalid):
            schema[field]("gpu")
    finally:
        for p in patchers:
            p.stop()


# ------------------------------------------------------------------ poster mode


@pytest.mark.parametrize(
    ("extra", "kind"),
    [
        ({"video_url": "rtsp://cam/main"}, "video"),
        ({"web_url": "http://g/stream.html?src=x"}, "web"),
        ({"whep_url": "http://g/api/webrtc?src=x"}, "whep"),
    ],
)
def test_poster_mode_goes_to_the_stream(extra, kind) -> None:
    data = SHOW_SCHEMA({
        "title": "t", "poster_url": "http://f/latest.jpg", "poster_mode": "FIT", **extra,
    })
    media = build_device_payload(data, {}, None)["media"]
    assert media[kind]["posterMode"] == "fit"
    assert media[kind]["poster"] == "http://f/latest.jpg"


def test_poster_mode_with_the_automatic_camera_poster() -> None:
    data = SHOW_SCHEMA({"title": "t", "poster_mode": "fill"})
    media = build_device_payload(
        data, {}, None, "http://ha/api/camera_proxy_stream/camera.x", poster_uri="http://ha/still"
    )["media"]
    assert media["web"]["posterMode"] == "fill"


def test_poster_mode_left_out_when_not_given() -> None:
    data = SHOW_SCHEMA({"title": "t", "whep_url": "http://g/api/webrtc?src=x",
                        "poster_url": "http://f/latest.jpg"})
    assert "posterMode" not in build_device_payload(data, {}, None)["media"]["whep"]
    image = SHOW_SCHEMA({"title": "t", "image_url": "http://f/a.jpg", "poster_mode": "fit"})
    assert "posterMode" not in build_device_payload(image, {}, None)["media"]["image"]


def test_poster_mode_values() -> None:
    for mode in ("fit", "fill", "stretch", "center"):
        assert SHOW_SCHEMA({"title": "t", "poster_mode": mode})["poster_mode"] == mode
    with pytest.raises(vol.Invalid):
        SHOW_SCHEMA({"title": "t", "poster_mode": "zoom"})
