"""
Top-level orchestrator for the Lounge API sessions.

Dispatches incoming Lounge API messages, manages sender connect/disconnect and the active
session, and implements the DIAL `App` interface (`launch()`).

Ported from yt-cast-receiver's YouTubeApp.ts.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from . import message as msg
from .const import (
    AUTOPLAY_MODE_DISABLED,
    AUTOPLAY_MODE_ENABLED,
    AUTOPLAY_MODE_UNSUPPORTED,
    CLIENT_YT,
    CLIENT_YTMUSIC,
    MUTE_POLICY_AUTO,
    MUTE_POLICY_PRESERVE_VOLUME_LEVEL,
    MUTE_POLICY_ZERO_VOLUME_LEVEL,
    PLAYER_STATUS_PLAYING,
    PLAYER_STATUS_STOPPED,
    RESET_ON_DISCONNECT_ALL,
    RESET_ON_DISCONNECT_ALL_EXPLICIT,
)
from .player import PlayerStateEvent
from .sender import Sender, SenderDataError
from .session import Session
from .types import ScreenInfo, Volume

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from logging import Logger

    import aiohttp

    from .message import Message
    from .player import Player


class YouTubeAppError(Exception):
    """Raised when a DIAL launch request can't be handled."""


class YouTubeApp:
    """Owns both (YT/YTMUSIC) Lounge API sessions and dispatches their messages to a Player."""

    def __init__(
        self,
        player: Player,
        screen: ScreenInfo,
        http: aiohttp.ClientSession,
        load_mdx_context: Callable[[str], Awaitable[dict[str, str] | None]],
        save_mdx_context: Callable[[str, dict[str, str]], Awaitable[None]],
        logger: Logger,
        *,
        enable_autoplay_on_connect: bool = True,
        mute_policy: str = MUTE_POLICY_AUTO,
        reset_player_on_disconnect_policy: str = RESET_ON_DISCONNECT_ALL,
    ) -> None:
        """
        Initialize the app (does not start sessions yet - call `start()`).

        :param player: The Player implementation to drive.
        :param screen: How this receiver identifies itself to the YouTube apps.
        :param http: Shared client session for all outbound Lounge API requests.
        :param load_mdx_context: See Session.__init__.
        :param save_mdx_context: See Session.__init__.
        """
        self.name = "YouTube Music Cast Receiver App"
        self.state = "stopped"
        self.allow_stop = False
        self.pid: str | None = None

        self._logger = logger
        self._player = player
        player.on_state = self._handle_player_state_event

        self._sessions: dict[str, Session] = {
            key: Session(
                client,
                screen,
                http,
                self._handle_incoming_message,
                self._on_session_terminate,
                load_mdx_context,
                save_mdx_context,
                logger,
            )
            for key, client in (("YT", CLIENT_YT), ("YTMUSIC", CLIENT_YTMUSIC))
        }
        self._active_session: Session | None = None
        self._connected_senders: list[Sender] = []
        self._implicitly_disconnected_senders: list[Sender] = []

        self._autoplay_mode_on_connect = (
            AUTOPLAY_MODE_ENABLED if enable_autoplay_on_connect else AUTOPLAY_MODE_DISABLED
        )
        self._autoplay_mode_before_unsupported_override: str | None = None
        self._mute_policy = mute_policy
        self._reset_policy = reset_player_on_disconnect_policy

        self.on_terminate: Callable[[Exception], Awaitable[None]] | None = None
        self.on_sender_connect: Callable[[Sender], Awaitable[None]] | None = None
        self.on_sender_disconnect: Callable[[Sender, bool], Awaitable[None]] | None = None

    async def start(
        self,
        get_pid: Callable[[], Awaitable[str | None]],
        save_pid: Callable[[str], Awaitable[None]],
    ) -> None:
        """
        Start both Lounge API sessions.

        :param get_pid: Returns a previously persisted DIAL app pid, or None.
        :param save_pid: Persists a newly generated pid.
        """
        if self.state != "stopped":
            return

        stored_pid = await get_pid()
        if stored_pid:
            self.pid = stored_pid
        else:
            self.pid = str(uuid.uuid4())
            await save_pid(self.pid)

        self.state = "starting"
        try:
            for session in self._sessions.values():
                await session.begin()
        except Exception:
            self.state = "stopped"
            raise
        self.state = "running"

    async def stop(self, error: Exception | None = None) -> None:
        """Stop both sessions, disconnecting any connected senders."""
        if self.state != "running" and not error:
            return
        self.state = "stopping"

        senders = list(self._connected_senders)
        self._connected_senders = []
        self._implicitly_disconnected_senders = []

        await self._player.reset()

        for session in self._sessions.values():
            try:
                await session.end()
            except Exception as err:
                self._logger.warning("Ignoring error while stopping YouTubeApp: %s", err)

        self.state = "stopped"

        for sender in senders:
            if self.on_sender_disconnect:
                await self.on_sender_disconnect(sender, False)

        if error and self.on_terminate:
            await self.on_terminate(error)

    @property
    def connected_senders(self) -> list[Sender]:
        """Currently connected senders."""
        return list(self._connected_senders)

    @property
    def player(self) -> Player:
        """The Player instance this app drives (e.g. for external-update reconciliation)."""
        return self._player

    # -- DIAL App interface --------------------------------------------------

    async def launch(self, launch_data: str) -> str:
        """
        Handle a DIAL launch request (the phone connecting via `POST /apps/YouTube`).

        :param launch_data: URL-encoded body containing `pairingCode` and `theme`.
        """
        parsed = parse_qs(launch_data)
        pairing_code = next(iter(parsed.get("pairingCode", [])), None)
        theme = next(iter(parsed.get("theme", [])), None)
        session = next((s for s in self._sessions.values() if s.client.theme == theme), None)

        if pairing_code and session:
            await self._check_and_switch_active_session(session)
            self._logger.info("Connecting sender through DIAL...")
            await session.register_pairing_code(pairing_code)
            return self.pid or ""

        if not pairing_code:
            raise YouTubeAppError("Invalid launch data: missing pairingCode")
        raise YouTubeAppError(f"Invalid launch data. Unknown theme: {theme}")

    def get_session(self, client_key: str) -> Session:
        """Return the Session for "YT" or "YTMUSIC" (used to drive the manual pairing-code service)."""
        return self._sessions[client_key]

    # -- session/message handling --------------------------------------------

    async def _check_and_switch_active_session(self, target: Session) -> None:
        if self._active_session and self._active_session.client is target.client:
            return

        if not self._active_session:
            self._active_session = target
            return

        self._logger.debug(
            "Target session is for %s clients, whereas active session is for %s clients.",
            target.client.name,
            self._active_session.client.name,
        )
        await self._player.reset()
        old_senders = self._connected_senders
        self._connected_senders = []
        self._implicitly_disconnected_senders = []
        for sender in old_senders:
            if self.on_sender_disconnect:
                await self.on_sender_disconnect(sender, False)
        old_session = self._active_session
        self._active_session = target
        try:
            await old_session.send_message(msg.lounge_screen_disconnected())
        except Exception as err:
            self._logger.error("Error sending message: %s", err)

    async def _on_session_terminate(self, error: Exception) -> None:
        await self.stop(error)

    async def _handle_incoming_message(self, messages: list[Message], session: Session) -> None:
        for message in messages:
            await self._handle_one_message(message, session)

    async def _handle_one_message(self, message: Message, session: Session) -> None:
        aid, name, payload = message.aid, message.name, message.payload
        is_active = session is self._active_session
        client = session.client

        self._logger.debug("-----------------------------------")
        self._logger.debug("(AID: %s) (%s) Incoming message: '%s'", aid, client.name, name)

        send: list[Message] = []

        if name == "getNowPlaying":
            send.append(msg.now_playing(aid, await self._player.get_state() if is_active else None))

        elif name == "loungeStatus":
            send += await self._handle_lounge_status(message, session)

        elif name in ("setPlaylist", "updatePlaylist"):
            if not is_active:
                return
            send += await self._handle_playlist_message(message, session)

        elif name == "next":
            if is_active:
                await self._player.next(aid)
        elif name == "previous":
            if is_active:
                await self._player.previous(aid)
        elif name == "pause":
            if is_active:
                await self._player.pause(aid)
        elif name == "stopVideo":
            if is_active:
                await self._player.stop(aid)
        elif name == "seekTo":
            if is_active:
                await self._player.seek(int(payload.get("newTime", 0)))
        elif name == "getVolume":
            send.append(msg.on_volume_changed(aid, await self._player.get_volume()))
        elif name == "setVolume":
            if is_active:
                await self._handle_set_volume(message)
        elif name == "play":
            if is_active:
                await self._player.resume(aid)
        elif name == "setAutoplayMode":
            if is_active:
                await self._set_autoplay_mode(aid, payload.get("autoplayMode"))
        else:
            self._logger.debug("(AID: %s) (%s) Not handled: '%s'", aid, client.name, name)

        if send:
            try:
                await session.send_message(send)
            except Exception as err:
                self._logger.error("Error sending message: %s", err)

    async def _handle_lounge_status(self, message: Message, session: Session) -> list[Message]:
        aid, payload = message.aid, message.payload
        senders = _parse_lounge_senders(payload, self._logger)
        send: list[Message] = []
        if self.state == "starting":
            if senders:
                self._connected_senders = senders
                self._active_session = session
                await self._set_autoplay_mode_by_sender_capabilities(aid)
                await self._enforce_mute_policy(aid)
                send.append(msg.on_has_previous_next_changed(aid, self._player.get_nav_info()))
                send.append(msg.now_playing(aid, None))
            return send

        handled = False
        details = payload.get("connectionEventDetails") if isinstance(payload, dict) else None
        if details:
            try:
                parsed_details = json.loads(details)
            except TypeError, ValueError:
                parsed_details = None
            device_id = parsed_details.get("deviceId") if parsed_details else None
            if device_id:
                connecting = next((s for s in senders if s.id == device_id), None)
                disconnecting = (
                    next((s for s in self._connected_senders if s.id == device_id), None)
                    if not connecting
                    else None
                )
                if connecting:
                    send += await self._handle_sender_connected(connecting, session, aid)
                    handled = True
                if disconnecting:
                    implicit = parsed_details.get("ui") != "true"
                    send += await self._handle_sender_disconnected(
                        disconnecting, session, implicit, aid
                    )
                    handled = True
        if not handled:
            nav = self._player.get_nav_info() if session is self._active_session else None
            send.append(msg.on_has_previous_next_changed(aid, nav))
            send.append(
                msg.on_autoplay_mode_changed(
                    aid, nav.autoplay_mode if nav else AUTOPLAY_MODE_UNSUPPORTED
                )
            )
        return send

    async def _handle_playlist_message(self, message: Message, session: Session) -> list[Message]:
        aid, name, payload = message.aid, message.name, message.payload
        send: list[Message] = []
        queue_before = list(self._player.queue.video_ids)
        payload_video_ids = (
            (payload.get("videoIds") or "").split(",") if payload.get("videoIds") else []
        )
        autoplay_dismissed = False
        if queue_before and payload_video_ids and queue_before[-1] != payload_video_ids[-1]:
            try:
                await session.send_message(msg.autoplay_up_next(aid, None))
            except Exception as err:
                self._logger.error("Error sending message: %s", err)
            autoplay_dismissed = True

        state_before = self._player.queue.get_state()
        nav_before = self._player.get_nav_info()
        self._player.queue.update_by_message(message)
        state_after = self._player.queue.get_state()
        nav_after = self._player.get_nav_info()

        if not autoplay_dismissed and state_before.autoplay != state_after.autoplay:
            send.append(
                msg.autoplay_up_next(aid, state_after.autoplay.id if state_after.autoplay else None)
            )

        if name == "setPlaylist" and (
            (state_before.current.id if state_before.current else None)
            != (state_after.current.id if state_after.current else None)
            or (
                state_before.current.context.index
                if state_before.current and state_before.current.context
                else None
            )
            != (
                state_after.current.context.index
                if state_after.current and state_after.current.context
                else None
            )
        ):
            if self._player.status != PLAYER_STATUS_STOPPED:
                await self._player.stop(aid)
            if state_after.current:
                await self._player.play(
                    state_after.current, int(payload.get("currentTime", 0) or 0), aid
                )
        elif name == "updatePlaylist" and not state_after.current:
            await self._player.stop(aid)
        else:
            send.append(msg.now_playing(aid, await self._player.get_state()))
            if (
                nav_before.has_next != nav_after.has_next
                or nav_before.has_previous != nav_after.has_previous
            ):
                send.append(msg.on_has_previous_next_changed(aid, nav_after))
        return send

    async def _handle_set_volume(self, message: Message) -> None:
        payload = message.payload
        muted_raw = payload.get("muted")
        muted = str(muted_raw).lower().strip() == "true" if muted_raw is not None else False
        new_volume = Volume(level=int(payload.get("volume", 0)), muted=muted)
        current = await self._player.get_volume()
        if new_volume.level != current.level or new_volume.muted != current.muted:
            await self._player.set_volume(new_volume, message.aid)

    async def _handle_sender_connected(
        self, sender: Sender, session: Session, aid: int | None
    ) -> list[Message]:
        await self._check_and_switch_active_session(session)
        send: list[Message] = []
        if not any(s.id == sender.id for s in self._connected_senders):
            self._logger.info("(%s) Sender connected: %s", session.client.name, sender.name)
            self._implicitly_disconnected_senders = [
                s for s in self._implicitly_disconnected_senders if s.id != sender.id
            ]
            self._connected_senders.append(sender)
            await self._set_autoplay_mode_by_sender_capabilities(aid)
            await self._enforce_mute_policy(aid)

            nav = self._player.get_nav_info()
            state = await self._player.get_state()
            send += [
                msg.on_has_previous_next_changed(aid, nav),
                msg.now_playing(aid, state),
                msg.on_state_change(aid, state),
            ]
            if self.on_sender_connect:
                await self.on_sender_connect(sender)
        return send

    async def _handle_sender_disconnected(
        self, sender: Sender, session: Session, implicit: bool, aid: int | None
    ) -> list[Message]:
        if session is not self._active_session:
            return []
        idx = next((i for i, s in enumerate(self._connected_senders) if s.id == sender.id), -1)
        if idx < 0:
            return []
        self._connected_senders.pop(idx)

        self._logger.info(
            "(%s) Sender disconnected%s: %s",
            session.client.name,
            " (implicit)" if implicit else "",
            sender.name,
        )
        if implicit and not any(s.id == sender.id for s in self._implicitly_disconnected_senders):
            self._implicitly_disconnected_senders.append(sender)

        if not self._connected_senders:
            reset_player = self._reset_policy == RESET_ON_DISCONNECT_ALL
            if (
                self._reset_policy == RESET_ON_DISCONNECT_ALL_EXPLICIT
                and not self._implicitly_disconnected_senders
            ):
                reset_player = True
            if reset_player:
                await self._player.reset()
        elif self._player.autoplay_mode == AUTOPLAY_MODE_UNSUPPORTED:
            await self._set_autoplay_mode_by_sender_capabilities(aid)

        if self._connected_senders:
            await self._enforce_mute_policy(aid)

        if self.on_sender_disconnect:
            await self.on_sender_disconnect(sender, implicit)
        return []

    async def _set_autoplay_mode(self, aid: int | None, value: str) -> None:
        if not self._active_session:
            return
        state_before = self._player.queue.get_state()
        self._player.queue.set_autoplay_mode(value)
        state_after = self._player.queue.get_state()
        send = [msg.on_autoplay_mode_changed(aid, value)]
        if state_before.autoplay != state_after.autoplay:
            send.append(
                msg.autoplay_up_next(aid, state_after.autoplay.id if state_after.autoplay else None)
            )
        try:
            await self._active_session.send_message(send)
        except Exception as err:
            self._logger.error("Error sending message: %s", err)

    async def _set_autoplay_mode_by_sender_capabilities(self, aid: int | None) -> None:
        if not self._connected_senders:
            return
        if not all(s.supports_autoplay() for s in self._connected_senders):
            if self._player.autoplay_mode != AUTOPLAY_MODE_UNSUPPORTED:
                self._autoplay_mode_before_unsupported_override = self._player.autoplay_mode
            mode = AUTOPLAY_MODE_UNSUPPORTED
        elif self._player.autoplay_mode != AUTOPLAY_MODE_UNSUPPORTED:
            mode = self._player.autoplay_mode
        else:
            mode = self._autoplay_mode_before_unsupported_override or self._autoplay_mode_on_connect
        self._autoplay_mode_before_unsupported_override = None
        await self._set_autoplay_mode(aid, mode)

    async def _enforce_mute_policy(self, aid: int | None) -> None:
        if not self._connected_senders:
            return
        if self._mute_policy == MUTE_POLICY_ZERO_VOLUME_LEVEL:
            zero_on_mute = True
        elif self._mute_policy == MUTE_POLICY_PRESERVE_VOLUME_LEVEL:
            zero_on_mute = False
        else:  # AUTO
            zero_on_mute = not all(s.supports_mute() for s in self._connected_senders)
        await self._player.set_zero_volume_level_on_mute(zero_on_mute, aid)

    async def _handle_player_state_event(self, event: PlayerStateEvent) -> None:
        if not self._active_session or not self._connected_senders:
            return
        current, previous, aid = event.current, event.previous, event.aid

        status_changed = position_changed = volume_changed = now_playing_changed = (
            autoplay_changed
        ) = True
        if previous:
            status_changed = previous.status != current.status
            position_changed = previous.position != current.position
            volume_changed = (
                previous.volume.level != current.volume.level
                or previous.volume.muted != current.volume.muted
            )
            now_playing_changed = (
                previous.queue.current.id if previous.queue.current else None
            ) != (
                current.queue.current.id if current.queue.current else None
            ) or previous.queue.id != current.queue.id
            autoplay_changed = previous.queue.autoplay != current.queue.autoplay

        send: list[Message] = []
        if status_changed or position_changed:
            send.append(msg.on_state_change(aid, current))
        if now_playing_changed or (
            status_changed and (not previous or previous.status != PLAYER_STATUS_PLAYING)
        ):
            send.append(msg.now_playing(aid, current))
            send.append(msg.on_has_previous_next_changed(aid, self._player.get_nav_info()))
        if volume_changed:
            send.append(msg.on_volume_changed(aid, current.volume))
        if autoplay_changed:
            send.append(
                msg.autoplay_up_next(
                    aid, current.queue.autoplay.id if current.queue.autoplay else None
                )
            )

        if send:
            if all(m.name == "onVolumeChanged" and m.aid is not None for m in send):
                await self._active_session.send_message(
                    send, defer_key="onVolumeChanged", defer_ms=200
                )
            else:
                try:
                    await self._active_session.send_message(send)
                except Exception as err:
                    self._logger.error("Error sending message: %s", err)


def _parse_lounge_senders(payload: dict[str, Any], logger: Logger) -> list[Sender]:
    try:
        devices = json.loads(payload.get("devices") or "[]")
    except (TypeError, ValueError) as err:
        logger.error("Failed to parse 'devices' property of loungeStatus payload: %s", err)
        return []
    senders = []
    for dev in devices:
        if dev.get("type") != "REMOTE_CONTROL":
            continue
        try:
            senders.append(Sender.parse(dev))
        except SenderDataError as err:
            logger.error("Failed to parse sender data in loungeStatus message: %s", err)
    return senders
