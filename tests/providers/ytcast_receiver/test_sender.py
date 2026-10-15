"""Tests for parsing connected senders out of loungeStatus payloads."""

import json
from typing import Any

import pytest

from music_assistant.providers.ytcast_receiver.const import CLIENT_YT, CLIENT_YTMUSIC
from music_assistant.providers.ytcast_receiver.sender import Sender, SenderDataError


def _device(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "dev1",
        "name": "Pixel",
        "app": "android-phone-1.0",
        "theme": "m",
        "capabilities": "que,atp,mus",
        "device": json.dumps({"os": "Android"}),
    }
    data.update(overrides)
    return data


def test_parse_valid_sender() -> None:
    """All known fields are mapped, including the client derived from the theme."""
    sender = Sender.parse(_device())
    assert sender.id == "dev1"
    assert sender.name == "Pixel"
    assert sender.client is CLIENT_YTMUSIC
    assert sender.capabilities == ["que", "atp", "mus"]
    assert sender.device == {"os": "Android"}


def test_parse_uses_client_name_as_fallback() -> None:
    """Senders without a `name` fall back to `clientName`."""
    sender = Sender.parse(_device(name=None, clientName="YouTube app", theme="cl"))
    assert sender.name == "YouTube app"
    assert sender.client is CLIENT_YT


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": None},
        {"name": None},
        {"theme": "unknown"},
    ],
)
def test_parse_rejects_incomplete_data(overrides: dict[str, Any]) -> None:
    """Missing id/name or an unknown theme raise SenderDataError."""
    with pytest.raises(SenderDataError):
        Sender.parse(_device(**overrides))


def test_parse_tolerates_invalid_device_json() -> None:
    """An unparsable `device` field degrades to an empty dict instead of failing."""
    assert Sender.parse(_device(device="{not json")).device == {}


def test_capabilities() -> None:
    """Autoplay support comes from the capabilities; mute support from a desktop app."""
    phone = Sender.parse(_device())
    assert phone.supports_autoplay()
    assert not phone.supports_mute()

    desktop = Sender.parse(_device(capabilities="", app="web-desktop"))
    assert not desktop.supports_autoplay()
    assert desktop.supports_mute()
