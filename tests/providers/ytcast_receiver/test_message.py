"""Tests for Lounge API message parsing and construction."""

from music_assistant.providers.ytcast_receiver import message as msg
from music_assistant.providers.ytcast_receiver.const import (
    CLIENT_YT,
    PLAYER_STATUS_PLAYING,
    PLAYER_STATUS_STOPPED,
)
from music_assistant.providers.ytcast_receiver.types import (
    PlayerNavInfo,
    PlayerState,
    PlaylistState,
    Video,
    VideoContext,
    Volume,
)


def _state(status: int, current: Video | None) -> PlayerState:
    return PlayerState(
        status=status,
        queue=PlaylistState(id="PL1", current=current),
        position=12.5,
        duration=200.0,
        volume=Volume(level=40, muted=False),
        cpn="cpn123",
    )


def test_parse_incoming_single_frame_with_dict_payload() -> None:
    """A frame with one JSON object payload becomes a Message with that dict as payload."""
    messages = msg.parse_incoming('[5,["setVolume",{"volume":"30","muted":"false"}]]')
    assert len(messages) == 1
    assert messages[0].aid == 5
    assert messages[0].name == "setVolume"
    assert messages[0].payload == {"volume": "30", "muted": "false"}


def test_parse_incoming_multiple_frames_and_newlines() -> None:
    """Consecutive frames are split correctly, also when the chunk contains newlines."""
    data = '[1,["pause"]]\n[2,["seekTo",{"newTime":"42"}]]\r\n[3,["play"]]'
    messages = msg.parse_incoming(data)
    assert [(m.aid, m.name) for m in messages] == [(1, "pause"), (2, "seekTo"), (3, "play")]
    assert messages[1].payload == {"newTime": "42"}


def test_parse_incoming_without_payload_defaults_to_empty_dict() -> None:
    """A message without a payload segment gets an empty dict, so `.get()` is always safe."""
    messages = msg.parse_incoming('[7,["getNowPlaying"]]')
    assert messages[0].payload == {}


def test_parse_incoming_multi_value_payload_stays_a_list() -> None:
    """A 'c' frame carries several values; they're kept as a list (BindParams reads [0])."""
    messages = msg.parse_incoming('[0,["c","SIDVALUE","",8]]')
    assert messages[0].name == "c"
    assert messages[0].payload == ["SIDVALUE", "", 8]


def test_parse_incoming_ignores_garbage() -> None:
    """Text that contains no frames yields no messages."""
    assert msg.parse_incoming("noop") == []


def test_now_playing_without_state_is_empty() -> None:
    """With nothing playing, nowPlaying carries an empty payload."""
    message = msg.now_playing(3, None)
    assert message.name == "nowPlaying"
    assert message.aid == 3
    assert message.payload == {}


def test_now_playing_includes_video_and_context() -> None:
    """NowPlaying reports the current video, position and its playlist context."""
    video = Video(
        id="vid1",
        client=CLIENT_YT,
        context=VideoContext(playlist_id="PL1", index=2, ctt="ctt", params="p"),
    )
    payload = msg.now_playing(1, _state(PLAYER_STATUS_PLAYING, video)).payload
    assert payload["videoId"] == "vid1"
    assert payload["currentTime"] == 12.5
    assert payload["state"] == PLAYER_STATUS_PLAYING
    assert payload["listId"] == "PL1"
    assert payload["currentIndex"] == 2
    assert payload["ctt"] == "ctt"
    assert payload["params"] == "p"
    # loaded time is only reported as the full duration while something is loaded
    assert payload["loadedTime"] == 200.0


def test_on_state_change_loaded_time_zero_when_stopped() -> None:
    """A stopped player reports no loaded time."""
    payload = msg.on_state_change(None, _state(PLAYER_STATUS_STOPPED, None)).payload
    assert payload["state"] == PLAYER_STATUS_STOPPED
    assert payload["loadedTime"] == 0


def test_on_has_previous_next_changed() -> None:
    """Navigation flags are reported as booleans, and default to False without nav info."""
    nav = PlayerNavInfo(has_previous=False, has_next=True, autoplay_mode="ENABLED")
    assert msg.on_has_previous_next_changed(1, nav).payload == {
        "hasPrevious": False,
        "hasNext": True,
    }
    assert msg.on_has_previous_next_changed(1, None).payload == {
        "hasPrevious": False,
        "hasNext": False,
    }


def test_on_volume_changed() -> None:
    """Volume level and mute state are passed through."""
    payload = msg.on_volume_changed(1, Volume(level=25, muted=True)).payload
    assert payload == {"volume": 25, "muted": True}


def test_autoplay_up_next_without_video() -> None:
    """Clearing the autoplay suggestion sends an empty payload."""
    assert msg.autoplay_up_next(1, None).payload == {}
    assert msg.autoplay_up_next(1, "vid9").payload == {"videoId": "vid9"}
