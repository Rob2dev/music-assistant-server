"""
Shared data types for the YouTube Music Cast Receiver plugin.

Ported from yt-cast-receiver's Video.ts and the exported interfaces in Player.ts / Playlist.ts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from .const import Client


class YtCastError(Exception):
    """Base class for the errors this plugin raises itself."""


# what a Lounge API call can end in: our own protocol errors, network failures and timeouts,
# and responses that don't parse
LOUNGE_API_ERRORS = (YtCastError, aiohttp.ClientError, TimeoutError, ValueError)


@dataclass(frozen=True)
class ScreenInfo:
    """How this receiver identifies itself to the YouTube apps (name in the Cast picker etc.)."""

    name: str
    app: str
    brand: str
    model: str


@dataclass
class VideoContext:
    """Playlist-position context attached to a Video, mirroring Video.ts's `context` field."""

    playlist_id: str | None = None
    index: int | None = None
    ctt: str | None = None
    params: str | None = None


@dataclass
class Video:
    """A single video reference within a Lounge API session, per yt-cast-receiver's Video.ts."""

    id: str
    client: Client
    context: VideoContext | None = None


@dataclass
class Volume:
    """Player volume state, per yt-cast-receiver's `Volume` interface in Player.ts."""

    level: int
    muted: bool


@dataclass
class PlaylistState:
    """Snapshot of queue state, per yt-cast-receiver's `PlaylistState` interface in Playlist.ts."""

    id: str | None
    video_ids: list[str] = field(default_factory=list)
    previous: Video | None = None
    current: Video | None = None
    next: Video | None = None
    autoplay: Video | None = None


@dataclass
class PlayerState:
    """Full player state snapshot, per yt-cast-receiver's `PlayerState` interface in Player.ts."""

    status: int
    queue: PlaylistState
    position: float
    duration: float
    volume: Volume
    cpn: str


@dataclass
class PlayerNavInfo:
    """Prev/next/autoplay availability, per yt-cast-receiver's `PlayerNavInfo` interface."""

    has_previous: bool
    has_next: bool
    autoplay_mode: str
