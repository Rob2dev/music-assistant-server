"""Tests for the abstract Player's status bookkeeping around the backend hooks."""

from music_assistant.providers.ytcast_receiver.const import (
    CLIENT_YT,
    PLAYER_STATUS_IDLE,
    PLAYER_STATUS_LOADING,
    PLAYER_STATUS_PAUSED,
    PLAYER_STATUS_PLAYING,
    PLAYER_STATUS_STOPPED,
)
from music_assistant.providers.ytcast_receiver.message import Message
from music_assistant.providers.ytcast_receiver.types import Video, Volume

from .conftest import FakePlayer


def _load_queue(player: FakePlayer, index: int, video_id: str) -> None:
    player.queue.update_by_message(
        Message(
            1,
            "setPlaylist",
            {
                "listId": "PL1",
                "videoIds": "a,b,c",
                "currentIndex": str(index),
                "videoId": video_id,
            },
        )
    )


async def test_play_reports_loading_then_playing(fake_player: FakePlayer) -> None:
    """Playing emits a LOADING and then a PLAYING state change."""
    await fake_player.play(Video(id="a", client=CLIENT_YT), 5, aid=2)
    assert fake_player.calls == [("play", ("a", 5))]
    assert [e.current.status for e in fake_player.events] == [
        PLAYER_STATUS_LOADING,
        PLAYER_STATUS_PLAYING,
    ]
    assert fake_player.events[-1].aid == 2


async def test_pause_only_when_playing(fake_player: FakePlayer) -> None:
    """Pause is ignored unless something is playing."""
    assert not await fake_player.pause()
    assert fake_player.calls == []

    await fake_player.play(Video(id="a", client=CLIENT_YT))
    assert await fake_player.pause()
    assert fake_player.status == PLAYER_STATUS_PAUSED


async def test_resume_paused_and_stopped(fake_player: FakePlayer) -> None:
    """Resume continues a paused video, and restarts the current queue item when stopped."""
    _load_queue(fake_player, 1, "b")
    assert await fake_player.resume()
    assert fake_player.call_names() == ["play"]

    await fake_player.pause()
    await fake_player.resume()
    assert fake_player.call_names() == ["play", "pause", "resume"]
    assert fake_player.status == PLAYER_STATUS_PLAYING


async def test_stop_is_noop_when_idle(fake_player: FakePlayer) -> None:
    """Stopping an idle player doesn't hit the backend."""
    assert await fake_player.stop()
    assert fake_player.calls == []


async def test_next_and_previous(fake_player: FakePlayer) -> None:
    """Next/previous play the neighbouring queue items."""
    _load_queue(fake_player, 1, "b")
    await fake_player.next()
    assert fake_player.calls[-1] == ("play", ("c", 0))
    await fake_player.previous()
    assert fake_player.calls[-1] == ("play", ("b", 0))


async def test_next_at_end_of_queue_stops(fake_player: FakePlayer) -> None:
    """Without a next item (and no autoplay) the player stops."""
    _load_queue(fake_player, 2, "c")
    await fake_player.play(Video(id="c", client=CLIENT_YT))
    assert not await fake_player.next()
    assert fake_player.status == PLAYER_STATUS_STOPPED


async def test_seek_while_paused_resumes(fake_player: FakePlayer) -> None:
    """Seeking a paused video resumes playback afterwards, like a real TV does."""
    assert not await fake_player.seek(10)

    await fake_player.play(Video(id="a", client=CLIENT_YT))
    await fake_player.pause()
    assert await fake_player.seek(30)
    assert fake_player.call_names()[-2:] == ["seek", "resume"]
    assert fake_player.status == PLAYER_STATUS_PLAYING


async def test_zero_volume_on_mute_policy(fake_player: FakePlayer) -> None:
    """With the zero-level policy, muting also sets the level to 0."""
    await fake_player.set_zero_volume_level_on_mute(True, None)
    await fake_player.set_volume(Volume(level=60, muted=True))
    assert fake_player.volume == Volume(level=0, muted=True)


async def test_get_volume_is_clamped(fake_player: FakePlayer) -> None:
    """Out-of-range backend volumes are clamped to 0-100."""
    fake_player.volume = Volume(level=150, muted=False)
    assert (await fake_player.get_volume()).level == 100


async def test_reset_returns_to_idle(fake_player: FakePlayer) -> None:
    """Reset stops playback, clears the queue and ends up idle."""
    _load_queue(fake_player, 1, "b")
    await fake_player.play(Video(id="b", client=CLIENT_YT))
    await fake_player.reset()
    assert fake_player.status == PLAYER_STATUS_IDLE
    assert fake_player.queue.current is None
    assert "stop" in fake_player.call_names()
