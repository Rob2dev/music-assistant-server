"""Tests for the bridge that drives a real Music Assistant player."""

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.ytcast_receiver.const import (
    CLIENT_YT,
    PLAYER_STATUS_IDLE,
    PLAYER_STATUS_LOADING,
    PLAYER_STATUS_PAUSED,
    PLAYER_STATUS_PLAYING,
    PLAYER_STATUS_STOPPED,
)
from music_assistant.providers.ytcast_receiver.mass_player import (
    START_GRACE_SECONDS,
    MassPlayer,
    _status_from_playback_state,
    _video_id_from_uri,
)
from music_assistant.providers.ytcast_receiver.message import Message
from music_assistant.providers.ytcast_receiver.types import Video, Volume

TARGET = "player_1"


def _make_player(target: str | None = TARGET) -> tuple[MassPlayer, MagicMock]:
    mass = MagicMock()
    mass.player_queues.play_media = AsyncMock()
    mass.player_queues.play_index = AsyncMock()
    for cmd in ("cmd_seek", "cmd_pause", "cmd_play", "cmd_stop", "cmd_volume_set"):
        setattr(mass.players, cmd, AsyncMock())
    mass.players.cmd_volume_mute = AsyncMock()
    ma_player = MagicMock()
    ma_player.volume_level = 40
    ma_player.volume_muted = False
    ma_player.group_volume = 40
    ma_player.group_volume_muted = False
    # like the Universal Player: no usable elapsed time of its own, the queue has the position
    ma_player.current_media.corrected_elapsed_time = 0.0
    ma_player.current_media.duration = 200
    mass.players.get_player.return_value = ma_player
    mass.player_queues.get.return_value.corrected_elapsed_time = 10.0
    return MassPlayer(mass, lambda: target, logging.getLogger("test.ytcast")), mass


async def test_position_comes_from_queue_with_player_fallback() -> None:
    """The queue's position is used; the player's only when the queue has no current item."""
    player, mass = _make_player()
    assert await player.do_get_position() == 10.0

    mass.player_queues.get.return_value.current_item = None
    mass.players.get_player.return_value.current_media.corrected_elapsed_time = 7.0
    assert await player.do_get_position() == 7.0


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("ytmusic://track/abc123", "abc123"),
        ("ytmusic--Hj5peZ2R://track/abc123", "abc123"),
        ("ytmusic://album/abc123", None),
        ("spotify://track/abc123", None),
        ("ytmusic://track/", None),
        ("not-a-uri", None),
    ],
)
def test_video_id_from_uri(uri: str, expected: str | None) -> None:
    """Video ids are extracted from (instance-qualified) ytmusic track uris only."""
    assert _video_id_from_uri(uri) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (PlaybackState.PLAYING, PLAYER_STATUS_PLAYING),
        (PlaybackState.PAUSED, PLAYER_STATUS_PAUSED),
        (PlaybackState.IDLE, PLAYER_STATUS_IDLE),
    ],
)
def test_status_from_playback_state(state: PlaybackState, expected: int) -> None:
    """MA playback states map onto Lounge API status codes."""
    assert _status_from_playback_state(state) == expected


async def test_do_play_uses_ytmusic_uri_and_seeks() -> None:
    """Playback goes through the ytmusic provider; a start position needs an extra seek."""
    player, mass = _make_player()
    assert await player.do_play(Video(id="abc", client=CLIENT_YT), 42.7)
    mass.player_queues.play_media.assert_awaited_once_with(TARGET, "ytmusic://track/abc")
    mass.players.cmd_seek.assert_awaited_once_with(TARGET, 42)


async def test_do_play_from_start_does_not_seek() -> None:
    """Starting at position 0 doesn't issue a seek."""
    player, mass = _make_player()
    await player.do_play(Video(id="abc", client=CLIENT_YT), 0)
    mass.players.cmd_seek.assert_not_awaited()


async def test_play_unplayable_video_reports_stopped() -> None:
    """A video YouTube Music can't play ends in STOPPED instead of hanging in LOADING."""
    player, mass = _make_player()
    mass.player_queues.play_media.side_effect = MediaNotFoundError("No playable items found")
    assert not await player.play(Video(id="abc", client=CLIENT_YT), 46)
    assert player.status == PLAYER_STATUS_STOPPED
    mass.players.cmd_seek.assert_not_awaited()


async def test_commands_without_target_player() -> None:
    """Without a target player, commands fail gracefully (stop counts as done)."""
    player, mass = _make_player(target=None)
    assert not await player.do_play(Video(id="abc", client=CLIENT_YT), 0)
    assert not await player.do_pause()
    assert await player.do_stop()
    mass.player_queues.play_media.assert_not_awaited()


async def test_resume_after_paused_seek_is_skipped() -> None:
    """MA's seek already restarts playback, so the follow-up resume must not play again."""
    player, mass = _make_player()
    await player.play(Video(id="abc", client=CLIENT_YT))
    await player.pause()
    await player.seek(30)
    mass.players.cmd_seek.assert_awaited_with(TARGET, 30)
    mass.players.cmd_play.assert_not_awaited()

    # a later, regular resume does reach the player
    mass.players.get_player.return_value.playback_state = PlaybackState.PAUSED
    await player.pause()
    await player.resume()
    mass.players.cmd_play.assert_awaited_once_with(TARGET)


async def test_handle_external_update_syncs_queue() -> None:
    """A track change made by MA itself is reflected in the tracked queue and status."""
    player, mass = _make_player()
    player.queue.update_by_message(
        Message(
            1,
            "setPlaylist",
            {"listId": "PL1", "videoIds": "a,b,c", "currentIndex": "1", "videoId": "b"},
        )
    )
    ma_player = mass.players.get_player.return_value
    ma_player.current_media.uri = "ytmusic--abc://track/c"
    ma_player.playback_state = PlaybackState.PLAYING

    await player.handle_external_update()
    assert player.queue.current is not None
    assert player.queue.current.id == "c"
    assert player.status == PLAYER_STATUS_PLAYING


def _set_playlist(player: MassPlayer) -> None:
    player.queue.update_by_message(
        Message(
            1,
            "setPlaylist",
            {"listId": "PL1", "videoIds": "a,b,c", "currentIndex": "0", "videoId": "a"},
        )
    )


async def test_handle_external_update_ignored_while_loading() -> None:
    """MA's stale state during play_media() must not reach the phone as a failed load."""
    player, mass = _make_player()
    player._status = PLAYER_STATUS_LOADING
    mass.players.get_player.return_value.playback_state = PlaybackState.IDLE
    await player.handle_external_update()
    assert player.status == PLAYER_STATUS_LOADING


async def test_ended_queue_advances_to_next_video() -> None:
    """When the cast video plays to its end, the next video of the phone's playlist starts."""
    player, mass = _make_player()
    _set_playlist(player)
    await player.play(Video(id="a", client=CLIENT_YT))
    queue = mass.player_queues.get.return_value
    queue.ended = True
    queue.current_item.uri = "ytmusic--abc://track/a"

    await player.handle_queue_update()
    mass.player_queues.play_media.assert_awaited_with(TARGET, "ytmusic://track/b")
    assert player.queue.current is not None
    assert player.queue.current.id == "b"

    # a repeated QUEUE_UPDATED for the same ended queue doesn't skip ahead again
    mass.player_queues.play_media.reset_mock()
    queue.current_item.uri = "ytmusic--abc://track/b"
    await player.handle_queue_update()
    await player.handle_queue_update()
    assert mass.player_queues.play_media.await_count == 1


async def test_queue_not_ended_or_other_media_does_not_advance() -> None:
    """A queue that didn't end, or ended on something we didn't cast, is left alone."""
    player, mass = _make_player()
    _set_playlist(player)
    await player.play(Video(id="a", client=CLIENT_YT))
    mass.player_queues.play_media.reset_mock()
    queue = mass.player_queues.get.return_value
    queue.current_item.uri = "ytmusic--abc://track/a"

    queue.ended = False
    await player.handle_queue_update()
    queue.ended = True
    queue.current_item.uri = "spotify://track/xyz"
    await player.handle_queue_update()
    mass.player_queues.play_media.assert_not_awaited()


async def test_pause_survives_ma_dropping_the_stream() -> None:
    """MA reporting idle / an ended queue during our pause neither stops nor skips the video."""
    player, mass = _make_player()
    _set_playlist(player)
    await player.play(Video(id="a", client=CLIENT_YT))
    await player.pause()
    mass.player_queues.play_media.reset_mock()

    ma_player = mass.players.get_player.return_value
    ma_player.playback_state = PlaybackState.IDLE
    await player.handle_external_update()
    assert player.status == PLAYER_STATUS_PAUSED

    queue = mass.player_queues.get.return_value
    queue.ended = True
    queue.current_item.uri = "ytmusic--abc://track/a"
    await player.handle_queue_update()
    mass.player_queues.play_media.assert_not_awaited()

    # resuming restarts the dropped stream at the paused position, in one step from MA's queue
    await player.resume()
    mass.player_queues.play_index.assert_awaited_once_with(
        TARGET, queue.current_index, seek_position=10
    )
    mass.player_queues.play_media.assert_not_awaited()
    mass.players.cmd_play.assert_not_awaited()
    assert player.status == PLAYER_STATUS_PLAYING


async def test_restart_without_video_in_ma_queue_plays_again() -> None:
    """When MA's queue no longer holds the video, it is played again and seeked."""
    player, mass = _make_player()
    _set_playlist(player)
    await player.play(Video(id="a", client=CLIENT_YT))
    await player.pause()
    mass.player_queues.play_media.reset_mock()
    mass.players.get_player.return_value.playback_state = PlaybackState.IDLE
    mass.player_queues.get.return_value.current_item.uri = "ytmusic--abc://track/other"

    await player.resume()
    mass.player_queues.play_index.assert_not_awaited()
    mass.player_queues.play_media.assert_awaited_once_with(TARGET, "ytmusic://track/a")
    mass.players.cmd_seek.assert_awaited_once_with(TARGET, 10)


async def test_resume_while_loading_is_ignored() -> None:
    """A play repeated by the phone while restarting doesn't start the video over."""
    player, mass = _make_player()
    await player.play(Video(id="a", client=CLIENT_YT))
    mass.player_queues.play_media.reset_mock()
    player._status = PLAYER_STATUS_LOADING
    assert not await player.resume()
    mass.player_queues.play_media.assert_not_awaited()
    mass.players.cmd_play.assert_not_awaited()


async def test_resume_from_ma_clears_our_pause() -> None:
    """After a resume made in MA itself, a later natural end advances again."""
    player, mass = _make_player()
    _set_playlist(player)
    await player.play(Video(id="a", client=CLIENT_YT))
    await player.pause()
    mass.players.get_player.return_value.playback_state = PlaybackState.PLAYING
    await player.handle_external_update()
    assert player.status == PLAYER_STATUS_PLAYING

    queue = mass.player_queues.get.return_value
    queue.ended = True
    queue.current_item.uri = "ytmusic--abc://track/a"
    await player.handle_queue_update()
    mass.player_queues.play_media.assert_awaited_with(TARGET, "ytmusic://track/b")


async def test_volume_of_group_comes_from_its_members() -> None:
    """A (sync)group has no volume_level of its own; its group volume is reported instead."""
    player, mass = _make_player()
    group = mass.players.get_player.return_value
    group.volume_level = None
    group.volume_muted = None
    group.group_volume = 35
    group.group_volume_muted = True
    assert await player.do_get_volume() == Volume(level=35, muted=True)

    # a player without any volume control at all
    group.group_volume = None
    group.group_volume_muted = None
    assert await player.do_get_volume() == Volume(level=0, muted=False)


async def test_stale_idle_after_start_is_not_reported() -> None:
    """Right after a (re)start MA may still report idle; that isn't passed on as stopped."""
    player, mass = _make_player()
    await player.play(Video(id="a", client=CLIENT_YT))
    ma_player = mass.players.get_player.return_value
    ma_player.playback_state = PlaybackState.IDLE
    await player.handle_external_update()
    assert player.status == PLAYER_STATUS_PLAYING

    # once MA reports playing, later state changes are followed again
    ma_player.playback_state = PlaybackState.PLAYING
    await player.handle_external_update()
    ma_player.playback_state = PlaybackState.IDLE
    await player.handle_external_update()
    assert player.status == PLAYER_STATUS_IDLE


async def test_stale_idle_grace_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A start that never reaches playing doesn't hide MA's state forever."""
    player, mass = _make_player()
    await player.play(Video(id="a", client=CLIENT_YT))
    mass.players.get_player.return_value.playback_state = PlaybackState.IDLE
    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + START_GRACE_SECONDS + 1)
    await player.handle_external_update()
    assert player.status == PLAYER_STATUS_IDLE
