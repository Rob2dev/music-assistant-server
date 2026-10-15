"""Shared fixtures for the YouTube Music Cast Receiver tests."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from music_assistant.providers.ytcast_receiver.player import Player, PlayerStateEvent
from music_assistant.providers.ytcast_receiver.types import Volume

if TYPE_CHECKING:
    from music_assistant.providers.ytcast_receiver.types import Video


class FakePlayer(Player):
    """In-memory Player backend that records every backend call it receives."""

    def __init__(self) -> None:
        """Initialize with full volume and an empty call log."""
        super().__init__(logging.getLogger("test.ytcast_receiver.player"))
        self.calls: list[tuple[str, object]] = []
        self.events: list[PlayerStateEvent] = []
        self.volume = Volume(level=50, muted=False)
        self.on_state = self._record_event

    async def do_play(self, video: Video, position: float) -> bool:
        """Record a play call."""
        self.calls.append(("play", (video.id, position)))
        return True

    async def do_pause(self) -> bool:
        """Record a pause call."""
        self.calls.append(("pause", None))
        return True

    async def do_resume(self) -> bool:
        """Record a resume call."""
        self.calls.append(("resume", None))
        return True

    async def do_stop(self) -> bool:
        """Record a stop call."""
        self.calls.append(("stop", None))
        return True

    async def do_seek(self, position: float) -> bool:
        """Record a seek call."""
        self.calls.append(("seek", position))
        return True

    async def do_set_volume(self, volume: Volume) -> bool:
        """Record and apply a volume change."""
        self.calls.append(("set_volume", volume))
        self.volume = volume
        return True

    async def do_get_volume(self) -> Volume:
        """Return the stored volume."""
        return self.volume

    async def do_get_position(self) -> float:
        """Return a fixed position."""
        return 0.0

    async def do_get_duration(self) -> float:
        """Return a fixed duration."""
        return 200.0

    def call_names(self) -> list[str]:
        """Return just the names of the recorded backend calls."""
        return [name for name, _ in self.calls]

    async def _record_event(self, event: PlayerStateEvent) -> None:
        self.events.append(event)


@pytest.fixture
def fake_player() -> FakePlayer:
    """Return a fresh FakePlayer."""
    return FakePlayer()
