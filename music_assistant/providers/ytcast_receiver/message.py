"""
Lounge API message parsing and construction.

Ported from yt-cast-receiver's Message.ts. Incoming messages arrive as lines shaped like
``[AID,["name",payload...]]`` from the RPC (bind) connection; outgoing messages are built here
and serialized by Session before being POSTed back to the ``bind`` endpoint.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .const import (
    PLAYER_STATUS_LOADING,
    PLAYER_STATUS_PAUSED,
    PLAYER_STATUS_PLAYING,
)

if TYPE_CHECKING:
    from .types import PlayerNavInfo, PlayerState, Volume

# Matches one `[AID,["name",payload...]]` frame per yt-cast-receiver's Message.parseIncoming.
# Non-greedy on both the name and the trailing payload so consecutive frames on the same
# line are split correctly.
_INCOMING_RE = re.compile(r'\[(\d+),\["(.+?)"(?:,(.*?))?\]\]')


@dataclass
class Message:
    """A single Lounge API message, incoming or outgoing."""

    aid: int | None
    name: str
    payload: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Coerce a falsy payload (notably None, from messages with no payload segment) to {}."""
        if not self.payload:
            self.payload = {}


def parse_incoming(data: str) -> list[Message]:
    """
    Parse raw text received on the RPC (bind) connection into Message objects.

    :param data: Unprocessed chunk of text from the RPC connection (may contain
        multiple frames and/or embedded newlines).
    """
    # newlines inside a payload string would otherwise break the frame boundary the
    # regex expects, so they are stripped first exactly as the TS implementation does.
    flat = data.replace("\n", "").replace("\r", "")
    messages = []
    for m in _INCOMING_RE.finditer(flat):
        aid = int(m.group(1))
        name = m.group(2)
        payload: Any = None
        if m.group(3) is not None:
            parsed = json.loads(f"[{m.group(3)}]")
            payload = parsed[0] if isinstance(parsed, list) and len(parsed) == 1 else parsed
        messages.append(Message(aid=aid, name=name, payload=payload))
    return messages


def now_playing(aid: int | None, player_state: PlayerState | None) -> Message:
    """Build an outgoing 'nowPlaying' message describing the currently playing video and state."""
    payload: dict[str, Any] = {}
    if player_state and player_state.queue.current:
        current = player_state.queue.current
        payload.update(
            {
                "currentTime": player_state.position,
                "duration": player_state.duration,
                "cpn": player_state.cpn,
                "loadedTime": 0,
                "videoId": current.id,
                "state": player_state.status,
                "seekableStartTime": 0,
                "seekableEndTime": player_state.duration,
            }
        )
        context = current.context
        if context:
            if context.playlist_id:
                payload["listId"] = context.playlist_id
            if context.index is not None:
                payload["currentIndex"] = context.index
            if context.ctt:
                payload["ctt"] = context.ctt
            if context.params:
                payload["params"] = context.params
    if player_state and player_state.status in (
        PLAYER_STATUS_PLAYING,
        PLAYER_STATUS_PAUSED,
        PLAYER_STATUS_LOADING,
    ):
        payload["loadedTime"] = player_state.duration
    return Message(aid=aid, name="nowPlaying", payload=payload)


def on_state_change(aid: int | None, player_state: PlayerState) -> Message:
    """Build an outgoing 'onStateChange' message reporting current playback state."""
    payload: dict[str, Any] = {
        "state": player_state.status,
        "currentTime": player_state.position,
        "duration": player_state.duration,
        "loadedTime": 0,
        "seekableStartTime": 0,
        "seekableEndTime": player_state.duration,
        "cpn": player_state.cpn,
    }
    if player_state.status in (PLAYER_STATUS_PLAYING, PLAYER_STATUS_PAUSED, PLAYER_STATUS_LOADING):
        payload["loadedTime"] = player_state.duration
    return Message(aid=aid, name="onStateChange", payload=payload)


def on_volume_changed(aid: int | None, volume: Volume) -> Message:
    """Build an outgoing 'onVolumeChanged' message."""
    return Message(
        aid=aid, name="onVolumeChanged", payload={"volume": volume.level, "muted": volume.muted}
    )


def on_autoplay_mode_changed(aid: int | None, autoplay_mode: str) -> Message:
    """Build an outgoing 'onAutoplayModeChanged' message."""
    return Message(aid=aid, name="onAutoplayModeChanged", payload={"autoplayMode": autoplay_mode})


def on_has_previous_next_changed(aid: int | None, nav_info: PlayerNavInfo | None) -> Message:
    """Build an outgoing 'onHasPreviousNextChanged' message."""
    return Message(
        aid=aid,
        name="onHasPreviousNextChanged",
        payload={
            "hasPrevious": bool(nav_info and nav_info.has_previous),
            "hasNext": bool(nav_info and nav_info.has_next),
        },
    )


def autoplay_up_next(aid: int | None, video_id: str | None) -> Message:
    """Build an outgoing 'autoplayUpNext' message."""
    return Message(
        aid=aid, name="autoplayUpNext", payload={"videoId": video_id} if video_id else None
    )


def lounge_screen_disconnected() -> Message:
    """Build the outgoing 'loungeScreenDisconnected' message sent when a session ends."""
    return Message(aid=None, name="loungeScreenDisconnected", payload={})
