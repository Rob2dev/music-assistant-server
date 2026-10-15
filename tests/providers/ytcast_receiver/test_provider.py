"""Tests for the YouTube Music Cast Receiver provider."""

import json
import logging
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.ytcast_receiver import (
    PLAYER_ID_AUTO,
    YtCastReceiverProvider,
    _dial_port,
)


def _make_provider(
    default_player_id: str = PLAYER_ID_AUTO, setup_data: dict[str, Any] | None = None
) -> YtCastReceiverProvider:
    """Build a bare provider wired to a mocked Music Assistant instance."""
    prov = YtCastReceiverProvider.__new__(YtCastReceiverProvider)
    prov.mass = MagicMock()
    prov.logger = logging.getLogger("test.ytcast.provider")
    prov._default_player_id = default_player_id
    prov._target_missing_logged = False
    data = dict(setup_data or {})
    prov.get_setup_value = lambda key, default=None: data.get(key, default)  # type: ignore[method-assign]
    prov._update_setup_data = lambda key, value, *_: data.__setitem__(key, value)  # type: ignore[method-assign]
    prov._test_setup_data = data  # type: ignore[attr-defined]
    return prov


def _players(prov: YtCastReceiverProvider) -> MagicMock:
    return cast("MagicMock", prov.mass.players)


def _ma_player(player_id: str, state: PlaybackState) -> MagicMock:
    player = MagicMock()
    player.player_id = player_id
    player.playback_state = state
    return player


def test_dial_port_is_deterministic() -> None:
    """The DIAL port must be stable across restarts (unlike ``hash()``)."""
    assert _dial_port("ytcast_receiver--test1234") == _dial_port("ytcast_receiver--test1234")
    ports = {_dial_port(f"ytcast_receiver--instance{index}") for index in range(50)}
    assert all(8100 <= port < 8500 for port in ports)
    # different instances should not all collide on one port
    assert len(ports) > 1


def test_target_player_explicit() -> None:
    """An explicitly configured, existing player is always the target."""
    prov = _make_provider("kitchen")
    _players(prov).get_player.return_value = MagicMock()
    assert prov._get_target_player_id() == "kitchen"


def test_target_player_explicit_but_missing(caplog: pytest.LogCaptureFixture) -> None:
    """A configured player that is gone yields no target, warned about once until it's back."""
    prov = _make_provider("kitchen")
    _players(prov).get_player.return_value = None
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            assert prov._get_target_player_id() is None
    assert len(caplog.records) == 1

    _players(prov).get_player.return_value = MagicMock()
    assert prov._get_target_player_id() == "kitchen"
    _players(prov).get_player.return_value = None
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        prov._get_target_player_id()
    assert len(caplog.records) == 1


def test_target_player_auto_prefers_playing() -> None:
    """In auto mode a currently playing player wins over the first one."""
    prov = _make_provider()
    _players(prov).all_players.return_value = [
        _ma_player("idle", PlaybackState.IDLE),
        _ma_player("busy", PlaybackState.PLAYING),
    ]
    assert prov._get_target_player_id() == "busy"


def test_target_player_auto_falls_back_to_first() -> None:
    """In auto mode without anything playing, the first available player is used."""
    prov = _make_provider()
    _players(prov).all_players.return_value = [
        _ma_player("first", PlaybackState.IDLE),
        _ma_player("second", PlaybackState.IDLE),
    ]
    assert prov._get_target_player_id() == "first"

    _players(prov).all_players.return_value = []
    assert prov._get_target_player_id() is None


def test_status_text_without_senders() -> None:
    """Without a running app or connected senders the status says so."""
    prov = _make_provider()
    prov._app = None
    assert prov._status_text() == "No device connected"


def test_status_text_with_sender_and_player() -> None:
    """The status names the casting device, its app, the target player and the current title."""
    prov = _make_provider("kitchen")
    sender = MagicMock()
    sender.name = "Pixel"
    sender.client.name = "YouTube Music"
    prov._app = MagicMock()
    prov._app.connected_senders = [sender]
    player = MagicMock()
    player.display_name = "Kitchen"
    player.current_media.title = "Never Gonna Give You Up"
    _players(prov).get_player.return_value = player
    assert prov._status_text() == "Pixel (YouTube Music) -> Kitchen: Never Gonna Give You Up"

    _players(prov).get_player.return_value = None
    assert prov._status_text() == "Pixel (YouTube Music) - no target player available"


async def test_mdx_context_roundtrip_as_json_string() -> None:
    """The receiver identity is persisted as a JSON string and read back as a dict."""
    prov = _make_provider()
    context = {"deviceId": "dev", "screenId": "screen"}
    await prov._save_mdx_context("mdxContext.m", context)
    assert prov._test_setup_data["mdxContext.m"] == json.dumps(context)  # type: ignore[attr-defined]
    assert await prov._load_mdx_context("mdxContext.m") == context


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # stored as a raw dict by earlier versions of the provider
        ({"deviceId": "dev", "screenId": "screen"}, {"deviceId": "dev", "screenId": "screen"}),
        (None, None),
        ("", None),
        ("{not json", None),
    ],
)
async def test_mdx_context_load_variants(stored: Any, expected: dict[str, str] | None) -> None:
    """Legacy dicts are still readable; missing or corrupt values mean no stored identity."""
    prov = _make_provider(setup_data={"mdxContext.m": stored})
    assert await prov._load_mdx_context("mdxContext.m") == expected
