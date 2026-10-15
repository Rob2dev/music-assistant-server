"""
Queue (playlist) state, built from `setPlaylist`/`updatePlaylist` Lounge API messages.

Ported from yt-cast-receiver's Playlist.ts, with one deliberate simplification: the original
resolves next/previous videos by calling YouTube's private `/next` endpoint (via youtubei.js),
which also drives "smart" autoplay continuation beyond the end of the queue. We skip that
entirely - the Lounge API already hands us the full `videoIds` array directly
from the phone, so next/previous here are resolved by walking that local array using the
current item's index. This gives correct hasNext/hasPrevious and in-queue navigation for free,
with no external API calls; the trade-off is no dynamic radio-style continuation past the end
of a finite queue, which is an accepted v1 limitation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import AUTOPLAY_MODE_UNSUPPORTED, CLIENTS
from .types import PlaylistState, Video, VideoContext

if TYPE_CHECKING:
    from .const import Client
    from .message import Message


class Playlist:
    """Queue state for one active session, kept in sync with the phone's queue."""

    def __init__(self) -> None:
        """Initialize an empty queue."""
        self._id: str | None = None
        self._video_ids: list[str] = []
        self._current: Video | None = None
        self._previous: Video | None = None
        self._next: Video | None = None
        self._autoplay_mode: str = AUTOPLAY_MODE_UNSUPPORTED

    @property
    def autoplay_mode(self) -> str:
        """Current autoplay setting, as negotiated with connected senders (not video prediction)."""
        return self._autoplay_mode

    def set_autoplay_mode(self, value: str) -> None:
        """Set the autoplay setting (independent of whether an autoplay video is available)."""
        self._autoplay_mode = value

    def update_by_message(self, message: Message) -> None:
        """
        Apply a `setPlaylist` or `updatePlaylist` message's payload to the queue.

        :param message: A Message with name "setPlaylist" or "updatePlaylist".
        """
        if message.name not in ("setPlaylist", "updatePlaylist"):
            return

        data = message.payload or {}
        client = _client_for_theme(data.get("theme"))

        if not data.get("listId"):
            self._id = None
            self._current = None
            self._next = None
            self._previous = None
            self._video_ids = []
            return

        self._id = data["listId"]
        self._video_ids = data["videoIds"].split(",") if data.get("videoIds") else []

        if message.name == "setPlaylist":
            if data.get("currentIndex") and data.get("videoId"):
                context = VideoContext(
                    playlist_id=data["listId"],
                    index=int(data["currentIndex"]),
                    ctt=data.get("ctt"),
                    params=data.get("params"),
                )
                self._current = Video(id=data["videoId"], client=client, context=context)
            else:
                self._current = None
        elif self._current:  # 'updatePlaylist'
            try:
                index = self._video_ids.index(self._current.id)
            except ValueError:
                self._current = None
            else:
                if not self._current.context:
                    self._current.context = VideoContext()
                self._current.context.playlist_id = data["listId"]
                self._current.context.index = index
                self._current.context.params = data.get("params")

        self._refresh_previous_next()

    def reset(self) -> None:
        """Clear all queue state."""
        self._id = None
        self._video_ids = []
        self._current = None
        self._previous = None
        self._next = None
        self._autoplay_mode = AUTOPLAY_MODE_UNSUPPORTED

    def go_previous(self) -> Video | None:
        """Move `current` to the previous video, if any, and return it."""
        if not self._previous:
            return None
        self._current = self._previous
        self._refresh_previous_next()
        return self._current

    def go_next(self) -> Video | None:
        """Move `current` to the next video, if any, and return it."""
        if not self.has_next:
            return None
        self._current = self._next
        self._refresh_previous_next()
        return self._current

    def set_as_current(self, video: Video) -> None:
        """Force `current` to the given video without walking prev/next (e.g. on videoSelected)."""
        if self._current and self._current.id == video.id:
            return
        self._current = video

    def sync_external_current(self, video_id: str) -> None:
        """
        Reconcile `current` to the video id MA is actually playing.

        Needed when MA's playback has diverged from what we locally track (e.g. MA advanced to a
        track on its own - natural end-of-track progression, or another controller changed
        playback - that our queue tracking, built only from what the phone last sent via
        setPlaylist, didn't predict).

        If video_id is found in our tracked `video_ids`, this is a normal in-queue advance and
        prev/next resolve correctly. If not found, we fall back to a bare Video with no context,
        which degrades hasNext/hasPrevious to False until the next setPlaylist message from the
        phone re-syncs the real queue - better than reporting a stale/wrong "now playing".
        """
        if self._current and self._current.id == video_id:
            return
        client = self._current.client if self._current else CLIENTS["YT"]
        try:
            index = self._video_ids.index(video_id)
        except ValueError:
            self._current = Video(id=video_id, client=client)
            self._previous = None
            self._next = None
            return
        self._current = self._video_at(index, client)
        self._refresh_previous_next()

    def get_state(self) -> PlaylistState:
        """Return an immutable snapshot of the current queue state."""
        return PlaylistState(
            id=self._id,
            video_ids=list(self._video_ids),
            previous=self._previous,
            current=self._current,
            next=self._next if self.has_next else None,
            autoplay=None,  # no Innertube-based radio continuation in this port - see module docstring
        )

    @property
    def id(self) -> str | None:
        """Id of the current queue."""
        return self._id

    @property
    def video_ids(self) -> list[str]:
        """Ids of the videos in the queue."""
        return self._video_ids

    @property
    def length(self) -> int:
        """Number of videos in the queue."""
        return len(self._video_ids)

    @property
    def current(self) -> Video | None:
        """The currently selected video, if any."""
        return self._current

    @property
    def is_last(self) -> bool:
        """Whether `current` is the last item in the queue (or nothing is selected)."""
        index = self._current.context.index if self._current and self._current.context else None
        return index is None or index < 0 or index == self.length - 1

    @property
    def has_previous(self) -> bool:
        """Whether a previous video is available."""
        return self._previous is not None

    @property
    def has_next(self) -> bool:
        """Whether a next video is available."""
        return self._next is not None and not self.is_last

    def _refresh_previous_next(self) -> None:
        self._previous = None
        self._next = None
        index = self._current.context.index if self._current and self._current.context else None
        if index is None or index < 0:
            return

        client = self._current.client if self._current else CLIENTS["YT"]
        if index > 0:
            self._previous = self._video_at(index - 1, client)
        if index + 1 < len(self._video_ids):
            self._next = self._video_at(index + 1, client)

    def _video_at(self, index: int, client: Client) -> Video:
        return Video(
            id=self._video_ids[index],
            client=client,
            context=VideoContext(playlist_id=self._id, index=index),
        )


def _client_for_theme(theme: str | None) -> Client:
    return next((c for c in CLIENTS.values() if c.theme == theme), CLIENTS["YT"])
