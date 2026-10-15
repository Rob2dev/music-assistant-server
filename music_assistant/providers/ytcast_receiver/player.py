"""
Abstract Player: wraps a concrete playback backend with status/queue/state bookkeeping.

Ported from yt-cast-receiver's Player.ts. Concrete implementations (e.g. a bridge onto MA's
player_queues/players controllers) provide the 9 `do_*` methods; this class handles the
status-transition bookkeeping and state-change notification around them so implementations
don't have to.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import (
    AUTOPLAY_MODE_ENABLED,
    PLAYER_STATUS_IDLE,
    PLAYER_STATUS_LOADING,
    PLAYER_STATUS_PAUSED,
    PLAYER_STATUS_PLAYING,
    PLAYER_STATUS_STOPPED,
)
from .playlist import Playlist
from .types import PlayerNavInfo, PlayerState, Volume

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from logging import Logger

    from .types import Video


@dataclass
class PlayerStateEvent:
    """Emitted whenever player state changes, for the orchestrator to relay to senders."""

    aid: int | None
    current: PlayerState
    previous: PlayerState | None


class Player(ABC):
    """Base class implementations subclass to bridge Lounge API playback commands onto real audio."""

    def __init__(self, logger: Logger) -> None:
        """Initialize the player (status starts IDLE, queue starts empty)."""
        self._status = PLAYER_STATUS_IDLE
        self.queue = Playlist()
        self._cpn = uuid.uuid4().hex[:16]
        self._previous_state: PlayerState | None = None
        self._zero_volume_level_on_mute = False
        self._logger = logger
        self.on_state: Callable[[PlayerStateEvent], Awaitable[None]] | None = None

    # -- abstract backend hooks ---------------------------------------------

    @abstractmethod
    async def do_play(self, video: Video, position: float) -> bool:
        """Play `video` starting at `position` seconds; return True on success."""

    @abstractmethod
    async def do_pause(self) -> bool:
        """Pause current playback; return True on success."""

    @abstractmethod
    async def do_resume(self) -> bool:
        """Resume paused playback; return True on success."""

    @abstractmethod
    async def do_stop(self) -> bool:
        """Stop current (or pending) playback; return True on success."""

    @abstractmethod
    async def do_seek(self, position: float) -> bool:
        """Seek to `position` seconds; return True on success."""

    @abstractmethod
    async def do_set_volume(self, volume: Volume) -> bool:
        """Set volume level/mute state; return True on success."""

    @abstractmethod
    async def do_get_volume(self) -> Volume:
        """Return the current volume level/mute state."""

    @abstractmethod
    async def do_get_position(self) -> float:
        """Return the current playback position in seconds."""

    @abstractmethod
    async def do_get_duration(self) -> float:
        """Return the duration of the current video in seconds."""

    # -- public API, mirrors Player.ts's wrapper methods --------------------

    async def play(self, video: Video, position: float = 0, aid: int | None = None) -> bool:
        """Play `video`, stopping current playback first if already playing."""
        if self._status == PLAYER_STATUS_PLAYING:
            await self.stop()
        self._logger.info("Player.play(): %s @ %ss", video.id, position)
        self.queue.set_as_current(video)
        await self._set_status_and_emit(PLAYER_STATUS_LOADING, aid)
        result = await self.do_play(video, position)
        await self._set_status_and_emit(
            PLAYER_STATUS_PLAYING if result else PLAYER_STATUS_STOPPED, aid
        )
        return result

    async def pause(self, aid: int | None = None) -> bool:
        """Pause playback if currently playing."""
        self._logger.info("Player.pause()")
        if self._status != PLAYER_STATUS_PLAYING:
            return False
        result = await self.do_pause()
        if result:
            await self._set_status_and_emit(PLAYER_STATUS_PAUSED, aid)
        return result

    async def resume(self, aid: int | None = None) -> bool:
        """Resume paused playback, or start playing the current queue item if stopped."""
        self._logger.info("Player.resume()")
        if self._status == PLAYER_STATUS_LOADING:
            # a play repeated while (re)starting would otherwise start the video over from 0
            return False
        if self._status == PLAYER_STATUS_PLAYING:
            await self._set_status_and_emit(PLAYER_STATUS_PLAYING, aid)
            return False
        if self._status == PLAYER_STATUS_PAUSED:
            result = await self.do_resume()
            if result:
                await self._set_status_and_emit(PLAYER_STATUS_PLAYING, aid)
            return result
        if self.queue.current:
            return await self.play(self.queue.current)
        return False

    async def stop(self, aid: int | None = None) -> bool:
        """Stop playback (no-op if already stopped/idle)."""
        if self._status in (PLAYER_STATUS_STOPPED, PLAYER_STATUS_IDLE):
            return True
        self._logger.info("Player.stop()")
        result = await self.do_stop()
        if result:
            await self._set_status_and_emit(PLAYER_STATUS_STOPPED, aid)
        return result

    async def seek(self, position: float, aid: int | None = None) -> bool:
        """Seek to `position` seconds (only valid while playing or paused)."""
        if self._status not in (PLAYER_STATUS_PLAYING, PLAYER_STATUS_PAUSED):
            return False
        self._logger.info("Player.seek(): %ss", position)
        previous_state = await self.get_state()
        was_paused = previous_state.status == PLAYER_STATUS_PAUSED
        result = await self.do_seek(position)
        if result:
            if was_paused:
                self._status = PLAYER_STATUS_PAUSED
                return await self.resume(aid)
            await self._set_status_and_emit(PLAYER_STATUS_PLAYING, aid)
        return result

    async def next(self, aid: int | None = None) -> bool:
        """Play the next video in the queue, or stop if there is none."""
        self._logger.info("Player.next()")
        next_video = self.queue.go_next()
        if not next_video:
            self._logger.info("No next video in queue.")
            autoplay_video = (
                self.queue.get_state().autoplay
                if self.autoplay_mode == AUTOPLAY_MODE_ENABLED
                else None
            )
            if autoplay_video:
                self._logger.info("Play autoplay video: %s", autoplay_video.id)
                return await self.play(autoplay_video, 0, aid)
            await self.stop(aid)
            return False
        return await self.play(next_video)

    async def previous(self, aid: int | None = None) -> bool:
        """Play the previous video in the queue, or stop if there is none."""
        self._logger.info("Player.previous()")
        previous_video = self.queue.go_previous()
        if not previous_video:
            self._logger.info("No previous video in queue.")
            await self.stop(aid)
            return False
        return await self.play(previous_video, 0, aid)

    async def set_volume(self, volume: Volume, aid: int | None = None) -> bool:
        """Set volume, enforcing zero-level-on-mute policy if configured."""
        v = Volume(level=volume.level, muted=volume.muted)
        if self._zero_volume_level_on_mute and v.muted and v.level > 0:
            v.level = 0
        self._logger.info("Player.set_volume(): %s", v)
        previous_state = await self.get_state()
        result = await self.do_set_volume(v)
        if result:
            current_state = await self.get_state()
            await self._emit(
                PlayerStateEvent(aid=aid, current=current_state, previous=previous_state)
            )
        return result

    async def reset(self, aid: int | None = None) -> None:
        """Reset the player and queue to idle/empty."""
        self._logger.info("Player.reset()")
        self.queue.reset()
        await self.stop(aid)
        self._previous_state = None
        await self._set_status_and_emit(PLAYER_STATUS_IDLE, aid)

    async def get_volume(self) -> Volume:
        """Return current volume, clamped to 0-100."""
        v = await self.do_get_volume()
        return Volume(level=max(0, min(100, v.level)), muted=v.muted)

    async def get_position(self) -> float:
        """Return current playback position in seconds."""
        return await self.do_get_position()

    async def get_duration(self) -> float:
        """Return current video duration in seconds."""
        return await self.do_get_duration()

    async def set_zero_volume_level_on_mute(self, value: bool, aid: int | None) -> None:
        """Enable/disable the mute policy that zeroes volume level on mute."""
        self._zero_volume_level_on_mute = value
        current = await self.get_volume()
        if value and current.muted and current.level > 0:
            await self.set_volume(current, aid)

    @property
    def status(self) -> int:
        """Current player status code (see const.PLAYER_STATUS_*)."""
        return self._status

    @property
    def autoplay_mode(self) -> str:
        """Current autoplay setting, as negotiated with connected senders."""
        return self.queue.autoplay_mode

    @property
    def cpn(self) -> str:
        """The client playback nonce for the current playback session."""
        return self._cpn

    def get_nav_info(self) -> PlayerNavInfo:
        """Return prev/next/autoplay availability for the current queue state."""
        return PlayerNavInfo(
            has_previous=self.queue.has_previous,
            has_next=self.queue.has_next,
            autoplay_mode=self.autoplay_mode,
        )

    async def get_state(self) -> PlayerState:
        """Return a full snapshot of current player + queue state."""
        return PlayerState(
            status=self._status,
            queue=self.queue.get_state(),
            position=await self.get_position(),
            duration=await self.get_duration(),
            volume=await self.get_volume(),
            cpn=self._cpn,
        )

    async def notify_external_state_change(self, new_status: int | None = None) -> None:
        """Signal a state change not captured by the wrapper methods above (e.g. external stop)."""
        await self._set_status_and_emit(new_status)

    async def _set_status_and_emit(self, status: int | None = None, aid: int | None = None) -> None:
        previous = self._previous_state
        if status is not None:
            self._status = status
        current = await self.get_state()
        self._previous_state = current
        await self._emit(PlayerStateEvent(aid=aid, current=current, previous=previous))

    async def _emit(self, event: PlayerStateEvent) -> None:
        if self.on_state:
            await self.on_state(event)
