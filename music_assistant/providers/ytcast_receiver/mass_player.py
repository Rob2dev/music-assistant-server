"""
Bridges the abstract Player contract onto a real Music Assistant player.

This is new code (no TS analog beyond the abstract Player.ts contract itself) - the whole point
of this port is that video-ID resolution doesn't need reimplementing: MA's existing `ytmusic`
provider already resolves a video id to a playable stream via yt-dlp, so playback just goes
through the normal `player_queues.play_media()` / `players.cmd_*()` APIs. The phone sends
discrete video ids and transport commands rather than a continuous audio feed, which is why this
plugin drives a real MA player instead of exposing a synthetic AudioSource.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import MusicAssistantError

from .const import (
    PLAYER_STATUS_IDLE,
    PLAYER_STATUS_LOADING,
    PLAYER_STATUS_PAUSED,
    PLAYER_STATUS_PLAYING,
    PLAYER_STATUS_STOPPED,
)
from .player import Player
from .types import Volume

if TYPE_CHECKING:
    from collections.abc import Callable
    from logging import Logger

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player as MusicAssistantPlayer

    from .types import Video


# how long a (re)start may take before a non-playing state reported by MA counts again
START_GRACE_SECONDS = 15


class MassPlayer(Player):
    """Drives an MA player via player_queues/players controller calls."""

    def __init__(
        self, mass: MusicAssistant, get_target_player_id: Callable[[], str | None], logger: Logger
    ) -> None:
        """
        Initialize the bridge.

        :param get_target_player_id: Returns the MA player_id to control, or None if no
            suitable target is currently available (e.g. no players configured/online).
        """
        super().__init__(logger)
        self._mass = mass
        self._get_target_player_id = get_target_player_id
        # set by do_seek() only when the player was paused (so Player.seek()'s wrapper is about
        # to call resume() immediately after); consumed by the very next do_resume() call. See
        # do_resume()'s docstring for why this exists.
        self._just_seeked = False
        # the video whose natural end we already advanced past, so one ended queue doesn't
        # trigger next() for every following QUEUE_UPDATED event
        self._ended_video_id: str | None = None
        # position at which *we* paused, None when not paused by us. Pausing can make MA drop
        # the stream (e.g. the Universal Player clears its output protocol), after which it
        # reports idle or even marks the queue as ended - this keeps our pause intact anyway.
        self._paused_at: float | None = None
        # monotonic deadline while a (re)start we issued hasn't been reported as playing yet;
        # MA's play commands return before the player actually runs, and the stale idle state
        # it reports meanwhile must not reach the phone as "stopped"
        self._starting_until: float | None = None

    async def do_play(self, video: Video, position: float) -> bool:
        """Start playback of `video` on the target MA player via the ytmusic provider."""
        self._paused_at = None
        target = self._get_target_player_id()
        if not target:
            self._logger.warning("No target player available; cannot play %s", video.id)
            return False
        uri = f"ytmusic://track/{video.id}"
        try:
            await self._mass.player_queues.play_media(target, uri)
        except MusicAssistantError as err:
            # e.g. a regular (non-music) YouTube video that YouTube Music can't play
            self._logger.warning("Unable to play %s: %s", video.id, err)
            return False
        if position:
            # play_media always starts at 0 - a mid-track resume needs an explicit follow-up seek.
            await self._mass.players.cmd_seek(target, int(position))
        self._starting_until = time.monotonic() + START_GRACE_SECONDS
        return True

    async def do_pause(self) -> bool:
        """Pause the target player."""
        self._starting_until = None
        target = self._get_target_player_id()
        if not target:
            return False
        position = await self.do_get_position()
        await self._mass.players.cmd_pause(target)
        self._paused_at = position
        return True

    async def do_resume(self) -> bool:
        """
        Resume the target player.

        Skips the actual command when it immediately follows a seek issued while paused:
        MA's cmd_seek() always restarts playback under the hood (player_queues.seek() ->
        play_index()) regardless of prior paused state, so Player.seek()'s wrapper calling
        resume() right after would otherwise issue a second, near-simultaneous cmd_play() -
        two overlapping stream restarts for one user action, audible as a glitch/hiccup.

        When MA dropped the stream during our pause (the player no longer reports paused),
        a plain cmd_play() would restart the track (or an ended queue) from the beginning, so
        the video is started again at the position where it was paused instead.
        """
        if self._just_seeked:
            self._just_seeked = False
            return True
        target = self._get_target_player_id()
        if not target:
            return False
        paused_at, self._paused_at = self._paused_at, None
        player = self._get_player()
        current = self.queue.current
        if (
            paused_at is not None
            and current
            and player
            and player.playback_state != PlaybackState.PAUSED
        ):
            self._logger.info("Stream was dropped while paused; restarting at %ss", int(paused_at))
            # restarting takes a few seconds: report loading, so the phone doesn't resend play
            await self.notify_external_state_change(PLAYER_STATUS_LOADING)
            if not await self._restart_at(current, paused_at):
                await self.notify_external_state_change(PLAYER_STATUS_STOPPED)
                return False
            return True
        await self._mass.players.cmd_play(target)
        return True

    async def do_stop(self) -> bool:
        """Stop the target player."""
        self._paused_at = None
        self._starting_until = None
        target = self._get_target_player_id()
        if not target:
            return True
        await self._mass.players.cmd_stop(target)
        return True

    async def do_seek(self, position: float) -> bool:
        """Seek the target player to `position` seconds."""
        target = self._get_target_player_id()
        if not target:
            return False
        await self._mass.players.cmd_seek(target, int(position))
        # MA's seek restarts playback, so this is no longer a pause of ours
        self._paused_at = None
        if self.status == PLAYER_STATUS_PAUSED:
            self._just_seeked = True
        return True

    async def do_set_volume(self, volume: Volume) -> bool:
        """Set volume level and mute state on the target player."""
        target = self._get_target_player_id()
        if not target:
            return False
        await self._mass.players.cmd_volume_set(target, volume.level)
        await self._mass.players.cmd_volume_mute(target, volume.muted)
        return True

    async def do_get_volume(self) -> Volume:
        """
        Return the target player's current volume/mute state.

        Uses the group volume, which for a group/syncgroup is derived from its members (a
        group has no volume_level of its own) and for a regular player is its own volume.
        Reporting 0 instead makes the phone's volume buttons compute every step from 0.
        """
        player = self._get_player()
        if not player:
            return Volume(level=100, muted=False)
        level = player.group_volume
        if level is None:
            level = player.volume_level
        muted = player.group_volume_muted
        if muted is None:
            muted = player.volume_muted
        return Volume(level=level or 0, muted=bool(muted))

    async def do_get_position(self) -> float:
        """
        Return the current (live-corrected) playback position within the video.

        Taken from the queue, which tracks the position within the current item (including
        seeks); a player's own elapsed time isn't reliable for that - the Universal Player
        e.g. reports 0 while playing a queue flow stream.
        """
        target = self._get_target_player_id()
        queue = self._mass.player_queues.get(target) if target else None
        if queue and queue.current_item:
            return queue.corrected_elapsed_time
        player = self._get_player()
        if player and player.current_media:
            position = player.current_media.corrected_elapsed_time
            if position is not None:
                return position
        return 0.0

    async def do_get_duration(self) -> float:
        """Return the duration of the target player's currently loaded media."""
        player = self._get_player()
        if player and player.current_media and player.current_media.duration:
            return player.current_media.duration
        return 0.0

    async def handle_external_update(self) -> None:
        """
        React to an MA-side player state change we didn't cause ourselves.

        Called from the provider's PLAYER_UPDATED event subscription (see __init__.py) - this
        is what makes the phone's UI reflect state changes MA makes on its own (a track ending
        naturally and MA moving on, or someone else controlling the player), which none of our
        own command-triggered code paths (do_play/do_pause/etc.) would ever see.
        """
        if self.status == PLAYER_STATUS_LOADING:
            # play_media() takes a few seconds; MA reports the stale (often idle) state of the
            # previous item meanwhile. Passing that on makes the phone think loading failed and
            # resend the video, so the state is only reported again once do_play() returns.
            return
        player = self._get_player()
        if not player:
            return
        if self._paused_at is not None:
            if player.playback_state != PlaybackState.PLAYING:
                # still our pause, even if MA dropped the stream and now reports idle
                return
            # resumed from MA itself
            self._paused_at = None
        if self._starting_until is not None:
            if (
                player.playback_state != PlaybackState.PLAYING
                and time.monotonic() < self._starting_until
            ):
                return
            self._starting_until = None

        raw_uri = player.current_media.uri if player.current_media else None
        video_id = _video_id_from_uri(raw_uri) if raw_uri else None
        in_tracked_queue = video_id in self.queue.video_ids if video_id else None
        self._logger.debug(
            "handle_external_update: current_media.uri=%r parsed_video_id=%r "
            "in_tracked_queue=%s (tracked current=%r)",
            raw_uri,
            video_id,
            in_tracked_queue,
            self.queue.current.id if self.queue.current else None,
        )
        if video_id:
            self.queue.sync_external_current(video_id)

        await self.notify_external_state_change(_status_from_playback_state(player.playback_state))

    async def handle_queue_update(self) -> None:
        """
        Advance to the phone's next video when the cast video played to its end.

        MA only holds the single video we started, so its queue ends with it; it is then up to
        us to continue with the sender's playlist (or autoplay), like a real cast receiver does.
        A user stop doesn't mark the queue as ended, so that doesn't advance; neither does a
        queue MA marks as ended while we paused it (see `_paused_at`).
        """
        target = self._get_target_player_id()
        queue = self._mass.player_queues.get(target) if target else None
        current = self.queue.current
        if (
            not queue
            or not queue.ended
            or not current
            or self.status == PLAYER_STATUS_LOADING
            or self._paused_at is not None
        ):
            self._ended_video_id = None
            return
        item = queue.current_item
        video_id = _video_id_from_uri(item.uri) if item and item.uri else None
        if video_id != current.id or self._ended_video_id == video_id:
            return
        self._ended_video_id = video_id
        self._logger.info("Video %s ended, continuing with the next one", video_id)
        await self.next()

    async def _restart_at(self, video: Video, position: float) -> bool:
        """
        Start `video` again at `position`, in a single step when MA's queue still holds it.

        play_index() with a seek position starts the stream once, at the right spot; do_play()
        resolves the video again and then seeks, which restarts the stream a second time.
        """
        target = self._get_target_player_id()
        queue = self._mass.player_queues.get(target) if target else None
        item = queue.current_item if queue else None
        if (
            target
            and queue
            and queue.current_index is not None
            and item
            and item.uri
            and _video_id_from_uri(item.uri) == video.id
        ):
            try:
                await self._mass.player_queues.play_index(
                    target, queue.current_index, seek_position=int(position)
                )
                self._starting_until = time.monotonic() + START_GRACE_SECONDS
                return True
            except MusicAssistantError as err:
                self._logger.debug("Restart from queue failed, playing again: %s", err)
        return await self.do_play(video, position)

    def _get_player(self) -> MusicAssistantPlayer | None:
        target = self._get_target_player_id()
        return self._mass.players.get_player(target) if target else None


def _video_id_from_uri(uri: str) -> str | None:
    """
    Extract the video id from a `ytmusic` track URI, tolerating any instance suffix.

    MA accepts the bare "ytmusic" domain when *starting* playback (it falls back to the
    configured instance), but reports back current_media using the real, fully-qualified
    instance id - e.g. "ytmusic--Hj5peZ2R://track/<id>", not "ytmusic://track/<id>". An
    exact-prefix match against the bare domain silently misses every real-world URI.
    """
    if "://" not in uri:
        return None
    provider, rest = uri.split("://", 1)
    if not provider.startswith("ytmusic") or "/" not in rest:
        return None
    media_type, item_id = rest.split("/", 1)
    if media_type != "track" or not item_id:
        return None
    return item_id


def _status_from_playback_state(state: PlaybackState) -> int:
    if state == PlaybackState.PLAYING:
        return PLAYER_STATUS_PLAYING
    if state == PlaybackState.PAUSED:
        return PLAYER_STATUS_PAUSED
    return PLAYER_STATUS_IDLE
