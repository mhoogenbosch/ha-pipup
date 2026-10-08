"""whep_url: payload shape, poster, and the app-version gate."""
from custom_components.pipup.services import build_device_payload, unsupported_media


def test_whep_payload_with_poster() -> None:
    payload = build_device_payload(
        {"whep_url": "http://g:1984/api/webrtc?src=deurbel", "poster_url": "http://f/latest.jpg"},
        {}, None, None, None, None,
    )
    whep = payload["media"]["whep"]
    assert whep["uri"] == "http://g:1984/api/webrtc?src=deurbel"
    assert whep["poster"] == "http://f/latest.jpg"
    assert whep["muted"] is True


def test_whep_needs_app_0_25() -> None:
    payload = {"media": {"whep": {"uri": "x"}}}
    assert unsupported_media(payload, "0.24.0") == ("whep", "0.25.0")
    assert unsupported_media(payload, "0.25.0") is None
    assert unsupported_media(payload, None) is None
    assert unsupported_media({"media": {"web": {"uri": "x"}}}, "0.2.0") is None


def test_version_gate_ignores_build_suffix() -> None:
    payload = {"media": {"whep": {"uri": "x"}}}
    # a release candidate or debug build of 0.25.0 has the 0.25.0 features
    assert unsupported_media(payload, "0.25.0-rc1") is None
    assert unsupported_media(payload, "0.25.0-debug") is None
    assert unsupported_media(payload, "0.24.9-rc1") == ("whep", "0.25.0")
    # unparseable: let through, as before
    assert unsupported_media(payload, "unknown") is None
