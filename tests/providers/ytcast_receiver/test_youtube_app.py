"""Tests for dispatching incoming Lounge API messages onto the Player."""

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.ytcast_receiver.const import (
    CLIENT_YT,
    PLAYER_STATUS_IDLE,
    PLAYER_STATUS_PAUSED,
    RESET_ON_DISCONNECT_ALL_EXPLICIT,
)
from music_assistant.providers.ytcast_receiver.message import Message
from music_assistant.providers.ytcast_receiver.session import Session, SessionError
from music_assistant.providers.ytcast_receiver.types import ScreenInfo, Video, Volume
from music_assistant.providers.ytcast_receiver.youtube_app import YouTubeApp, YouTubeAppError

from .conftest import FakePlayer

DEVICE = {
    "id": "dev1",
    "name": "Pixel",
    "app": "android-phone",
    "theme": "m",
    "type": "REMOTE_CONTROL",
    "capabilities": "atp",
}


def _make_app(player: FakePlayer) -> YouTubeApp:
    app = YouTubeApp(
        player=player,
        screen=ScreenInfo(name="Music Assistant", app="ytcr", brand="Generic", model="SmartTV"),
        http=MagicMock(),
        load_mdx_context=AsyncMock(return_value=None),
        save_mdx_context=AsyncMock(),
        logger=logging.getLogger("test.ytcast.app"),
        reset_player_on_disconnect_policy=RESET_ON_DISCONNECT_ALL_EXPLICIT,
    )
    for client_key in ("YT", "YTMUSIC"):
        session = app.get_session(client_key)
        session.send_message = AsyncMock(return_value=True)  # type: ignore[method-assign]
        session.register_pairing_code = AsyncMock()  # type: ignore[method-assign]
    app.state = "running"
    return app


def _active(app: YouTubeApp) -> Session:
    session = app.get_session("YTMUSIC")
    app._active_session = session
    return session


def _sent(session: Session) -> list[Message]:
    """Flatten every message passed to the (mocked) send_message."""
    sent: list[Message] = []
    mock = session.send_message
    assert isinstance(mock, AsyncMock)
    for call in mock.await_args_list:
        batch = call.args[0]
        sent += batch if isinstance(batch, list) else [batch]
    return sent


def _lounge_status(devices: list[dict[str, Any]], **details: str) -> Message:
    payload = {
        "devices": json.dumps(devices),
        "connectionEventDetails": json.dumps(details),
    }
    return Message(1, "loungeStatus", payload)


async def test_pause_on_active_session(fake_player: FakePlayer) -> None:
    """A pause message from the active session pauses the player."""
    app = _make_app(fake_player)
    session = _active(app)
    await fake_player.play(Video(id="a", client=CLIENT_YT))
    await app._handle_incoming_message([Message(2, "pause")], session)
    assert fake_player.status == PLAYER_STATUS_PAUSED


async def test_commands_from_inactive_session_are_ignored(fake_player: FakePlayer) -> None:
    """Only the active session may control playback."""
    app = _make_app(fake_player)
    _active(app)
    await fake_player.play(Video(id="a", client=CLIENT_YT))
    await app._handle_incoming_message([Message(2, "pause")], app.get_session("YT"))
    assert "pause" not in fake_player.call_names()


async def test_set_playlist_starts_playback_at_position(fake_player: FakePlayer) -> None:
    """SetPlaylist selects the video and plays it from the phone's current time."""
    app = _make_app(fake_player)
    session = _active(app)
    message = Message(
        3,
        "setPlaylist",
        {
            "listId": "PL1",
            "videoIds": "a,b,c",
            "currentIndex": "1",
            "videoId": "b",
            "currentTime": "12",
        },
    )
    await app._handle_incoming_message([message], session)
    assert fake_player.calls == [("play", ("b", 12))]
    assert fake_player.queue.has_next


async def test_get_volume_replies_with_same_aid(fake_player: FakePlayer) -> None:
    """GetVolume is answered with an onVolumeChanged carrying the request's AID."""
    app = _make_app(fake_player)
    session = _active(app)
    await app._handle_incoming_message([Message(9, "getVolume")], session)
    replies = [m for m in _sent(session) if m.name == "onVolumeChanged"]
    assert len(replies) == 1
    assert replies[0].aid == 9
    assert replies[0].payload == {"volume": 50, "muted": False}


async def test_set_volume_parses_payload(fake_player: FakePlayer) -> None:
    """SetVolume's string payload is converted and applied once."""
    app = _make_app(fake_player)
    session = _active(app)
    await app._handle_incoming_message(
        [Message(4, "setVolume", {"volume": "20", "muted": "true"})], session
    )
    assert fake_player.volume == Volume(level=20, muted=True)

    # repeating the same volume doesn't hit the backend again
    await app._handle_incoming_message(
        [Message(5, "setVolume", {"volume": "20", "muted": "true"})], session
    )
    assert fake_player.call_names().count("set_volume") == 1


async def test_sender_connect_and_explicit_disconnect(fake_player: FakePlayer) -> None:
    """A connecting sender is tracked; an explicit last disconnect resets the player."""
    app = _make_app(fake_player)
    session = _active(app)
    connected = AsyncMock()
    disconnected = AsyncMock()
    app.on_sender_connect = connected
    app.on_sender_disconnect = disconnected

    await app._handle_incoming_message([_lounge_status([DEVICE], deviceId="dev1")], session)
    assert [s.id for s in app.connected_senders] == ["dev1"]
    connected.assert_awaited_once()
    assert any(m.name == "nowPlaying" for m in _sent(session))

    await fake_player.play(Video(id="a", client=CLIENT_YT))
    await app._handle_incoming_message([_lounge_status([], deviceId="dev1", ui="true")], session)
    assert app.connected_senders == []
    disconnected.assert_awaited_once()
    assert fake_player.status == PLAYER_STATUS_IDLE


async def test_implicit_disconnect_keeps_playing(fake_player: FakePlayer) -> None:
    """A phone that merely goes away (locked, out of WiFi) leaves the music playing."""
    app = _make_app(fake_player)
    session = _active(app)
    await app._handle_incoming_message([_lounge_status([DEVICE], deviceId="dev1")], session)
    await fake_player.play(Video(id="a", client=CLIENT_YT))

    await app._handle_incoming_message([_lounge_status([], deviceId="dev1")], session)
    assert app.connected_senders == []
    assert "stop" not in fake_player.call_names()


async def test_launch_registers_pairing_code(fake_player: FakePlayer) -> None:
    """A DIAL launch registers the pairing code on the session matching the theme."""
    app = _make_app(fake_player)
    app.pid = "pid-1"
    assert await app.launch("pairingCode=1234&theme=m") == "pid-1"
    register = app.get_session("YTMUSIC").register_pairing_code
    assert isinstance(register, AsyncMock)
    register.assert_awaited_once_with("1234")


@pytest.mark.parametrize("launch_data", ["theme=m", "pairingCode=1234&theme=unknown"])
async def test_launch_rejects_invalid_data(fake_player: FakePlayer, launch_data: str) -> None:
    """A launch without pairing code or with an unknown theme is rejected."""
    app = _make_app(fake_player)
    with pytest.raises(YouTubeAppError):
        await app.launch(launch_data)


async def test_start_failure_leaves_app_stopped(fake_player: FakePlayer) -> None:
    """When a session can't be established, the app ends up stopped, not stuck starting."""
    app = _make_app(fake_player)
    app.state = "stopped"
    app.get_session("YT").begin = AsyncMock(  # type: ignore[method-assign]
        side_effect=SessionError("no lounge token")
    )
    with pytest.raises(SessionError):
        await app.start(AsyncMock(return_value="pid"), AsyncMock())
    assert app.state == "stopped"
