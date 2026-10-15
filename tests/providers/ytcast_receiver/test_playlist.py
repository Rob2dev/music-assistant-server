"""Tests for the queue state kept in sync with the phone."""

from typing import Any

from music_assistant.providers.ytcast_receiver.const import CLIENT_YTMUSIC
from music_assistant.providers.ytcast_receiver.message import Message
from music_assistant.providers.ytcast_receiver.playlist import Playlist

VIDEO_IDS = "a,b,c,d"


def _set_playlist(current_index: int, video_id: str, **extra: Any) -> Message:
    payload = {
        "listId": "PL1",
        "videoIds": VIDEO_IDS,
        "currentIndex": str(current_index),
        "videoId": video_id,
        "theme": "m",
        **extra,
    }
    return Message(1, "setPlaylist", payload)


def test_set_playlist_selects_current_with_neighbours() -> None:
    """SetPlaylist selects the current video and derives previous/next from the index."""
    playlist = Playlist()
    playlist.update_by_message(_set_playlist(1, "b"))
    state = playlist.get_state()
    assert state.id == "PL1"
    assert state.video_ids == ["a", "b", "c", "d"]
    assert state.current is not None
    assert state.current.id == "b"
    assert state.current.client is CLIENT_YTMUSIC
    assert state.previous is not None
    assert state.previous.id == "a"
    assert state.next is not None
    assert state.next.id == "c"


def test_last_item_has_no_next() -> None:
    """The last queue item has a previous but no next video."""
    playlist = Playlist()
    playlist.update_by_message(_set_playlist(3, "d"))
    assert playlist.is_last
    assert playlist.has_previous
    assert not playlist.has_next
    assert playlist.go_next() is None


def test_first_item_is_selectable() -> None:
    """The phone sends currentIndex as a string, so index "0" still selects the first video."""
    playlist = Playlist()
    playlist.update_by_message(_set_playlist(0, "a"))
    assert playlist.current is not None
    assert playlist.current.id == "a"
    assert not playlist.has_previous


def test_go_next_and_previous_walk_the_queue() -> None:
    """Navigation moves `current` through the tracked video ids."""
    playlist = Playlist()
    playlist.update_by_message(_set_playlist(1, "b"))
    next_video = playlist.go_next()
    assert next_video is not None
    assert next_video.id == "c"
    previous_video = playlist.go_previous()
    assert previous_video is not None
    assert previous_video.id == "b"


def test_update_playlist_reindexes_current() -> None:
    """When the phone reorders the queue, the current video keeps playing at its new index."""
    playlist = Playlist()
    playlist.update_by_message(_set_playlist(1, "b"))
    playlist.update_by_message(Message(2, "updatePlaylist", {"listId": "PL1", "videoIds": "x,y,b"}))
    assert playlist.current is not None
    assert playlist.current.id == "b"
    assert playlist.current.context is not None
    assert playlist.current.context.index == 2
    assert playlist.is_last


def test_update_playlist_drops_removed_current() -> None:
    """If the current video was removed from the queue, nothing is current anymore."""
    playlist = Playlist()
    playlist.update_by_message(_set_playlist(1, "b"))
    playlist.update_by_message(Message(2, "updatePlaylist", {"listId": "PL1", "videoIds": "a,c"}))
    assert playlist.current is None


def test_empty_list_id_clears_queue() -> None:
    """A message without listId clears the whole queue."""
    playlist = Playlist()
    playlist.update_by_message(_set_playlist(1, "b"))
    playlist.update_by_message(Message(2, "updatePlaylist", {}))
    assert playlist.id is None
    assert playlist.length == 0
    assert playlist.current is None


def test_other_messages_are_ignored() -> None:
    """Only setPlaylist/updatePlaylist change the queue."""
    playlist = Playlist()
    playlist.update_by_message(Message(1, "pause", {"listId": "PL1"}))
    assert playlist.id is None


def test_sync_external_current_inside_queue() -> None:
    """MA advancing to a queued video moves `current` there with correct neighbours."""
    playlist = Playlist()
    playlist.update_by_message(_set_playlist(1, "b"))
    playlist.sync_external_current("c")
    assert playlist.current is not None
    assert playlist.current.id == "c"
    assert playlist.has_previous
    assert playlist.has_next


def test_sync_external_current_outside_queue() -> None:
    """An unknown video becomes current without navigation until the phone re-syncs."""
    playlist = Playlist()
    playlist.update_by_message(_set_playlist(1, "b"))
    playlist.sync_external_current("zzz")
    assert playlist.current is not None
    assert playlist.current.id == "zzz"
    assert playlist.current.client is CLIENT_YTMUSIC
    assert not playlist.has_previous
    assert not playlist.has_next


def test_reset_clears_everything() -> None:
    """Reset empties the queue and resets the autoplay mode."""
    playlist = Playlist()
    playlist.update_by_message(_set_playlist(1, "b"))
    playlist.set_autoplay_mode("ENABLED")
    playlist.reset()
    assert playlist.current is None
    assert playlist.length == 0
    assert playlist.autoplay_mode == "UNSUPPORTED"
