"""
Session lifecycle: screen id, lounge token, RPC connection, ordered outgoing messages.

Ported from yt-cast-receiver's Session.ts. One Session exists per Client (YouTube and YouTube
Music each get their own, run concurrently - the phone's "cast to" theme selects which one
becomes active).

Simplification vs. the TS original: outgoing messages use a single-flight `asyncio.Lock` instead
of porting the full generic `AsyncTaskQueue` utility. This preserves in-order delivery (the
property that actually matters for the Lounge API) without the extra machinery; the one behavior
intentionally dropped is AID renumbering for messages queued-but-unsent across a token refresh,
which only matters for messages queued during the sub-second refresh window - acceptable for v1.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import aiohttp

from .bind_params import BindParams, IncompleteAPIDataError
from .const import (
    URL_BIND,
    URL_GENERATE_SCREEN_ID,
    URL_GET_LOUNGE_TOKEN_BATCH,
    URL_REGISTER_PAIRING_CODE,
)
from .message import Message, lounge_screen_disconnected, parse_incoming
from .pairing import PairingCodeRequestService
from .rpc_connection import RPCConnection
from .types import LOUNGE_API_ERRORS, YtCastError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from logging import Logger

    from .const import Client
    from .types import ScreenInfo

_FALLBACK_REFRESH_SECONDS = 13 * 24 * 60 * 60  # 13 days, matches the TS fallback


class SessionError(YtCastError):
    """Wraps an underlying error encountered while establishing/refreshing/ending a session."""


class Session:
    """Owns one Lounge API session (screen id, lounge token, RPC connection) for one Client."""

    def __init__(
        self,
        client: Client,
        screen: ScreenInfo,
        http: aiohttp.ClientSession,
        on_messages: Callable[[list[Message], Session], Coroutine[Any, Any, None]],
        on_terminate: Callable[[Exception], Awaitable[None]],
        load_mdx_context: Callable[[str], Awaitable[dict[str, str] | None]],
        save_mdx_context: Callable[[str, dict[str, str]], Awaitable[None]],
        logger: Logger,
    ) -> None:
        """
        Initialize the session (does not connect yet - call `begin()`).

        :param client: Which YouTube app (YT or YTMUSIC) this session is for.
        :param screen: How this receiver identifies itself to the YouTube apps.
        :param http: Shared client session for all outbound requests.
        :param on_messages: Awaited with each batch of incoming Messages once established.
        :param on_terminate: Awaited if the session dies irrecoverably.
        :param load_mdx_context: Given a storage key, returns a previously persisted
            {"deviceId", "screenId"} dict, or None. Lets the receiver keep the same identity
            across MA restarts instead of re-pairing every time.
        :param save_mdx_context: Persists the current {"deviceId", "screenId"} under a storage key.
        """
        self.client = client
        self._screen_name = screen.name
        self._screen_app = screen.app
        self._screen_id: str | None = None
        self._http = http
        self._on_messages = on_messages
        self._on_terminate = on_terminate
        self._load_mdx_context = load_mdx_context
        self._save_mdx_context = save_mdx_context
        self._logger = logger

        self._bind_params = BindParams(
            device_id=_new_device_id(),
            screen_name=screen.name,
            screen_app=screen.app,
            theme=client.theme,
            brand=screen.brand,
            model=screen.model,
        )
        self._rpc: RPCConnection | None = None
        self._ofs = 0
        self._send_lock = asyncio.Lock()
        self._deferred: dict[str, asyncio.Task[None]] = {}
        # incoming message batches being handled; tracked so they can be cancelled on end()
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._status = "stopped"
        self._token_refresh_task: asyncio.Task[None] | None = None

        self.pairing = PairingCodeRequestService(
            http=http,
            screen_id_getter=lambda: self._screen_id,
            screen_name=screen.name,
            screen_app=screen.app,
            bind_params=self._bind_params,
            on_response=self._on_pairing_response,
            on_error=self._on_pairing_error,
            logger=logger,
        )
        self._on_pairing_response_cb: Callable[[str], Awaitable[None]] | None = None

    def set_pairing_response_handler(self, handler: Callable[[str], Awaitable[None]]) -> None:
        """Register the callback invoked with each newly obtained manual pairing code."""
        self._on_pairing_response_cb = handler

    async def begin(self) -> None:
        """Establish (or re-establish) the session: screen id, lounge token, RPC connection."""
        is_refreshing = self._status == "refreshing"
        if self._status != "stopped" and not is_refreshing:
            return
        if not is_refreshing:
            self._status = "starting"

        storage_key = f"mdxContext.{self.client.theme}"
        screen_id_from_store = False
        if not is_refreshing and not self._screen_id:
            screen_id_from_store = await self._restore_mdx_context(storage_key)

        error: Exception | None = None
        try:
            await self._init_session(screen_id_from_store)
        except IncompleteAPIDataError as err:
            error = SessionError(f"({self.client.name}) Query string test failed: {err.missing}")
        except LOUNGE_API_ERRORS as err:
            error = SessionError(f"({self.client.name}) Failed to establish session: {err}")

        if not error:
            try:
                await self._connect_rpc()
                if not is_refreshing:
                    self._status = "running"
            except LOUNGE_API_ERRORS as err:
                error = SessionError(f"({self.client.name}) Failed to establish session: {err}")

        if error:
            if is_refreshing:
                await self.end(error)
            else:
                try:
                    await self.end()
                finally:
                    raise error
        else:
            self._logger.debug(
                "(%s) Saving MDX context to data store: deviceId=%s screenId=%s",
                self.client.name,
                self._bind_params.id,
                self._screen_id,
            )
            await self._save_mdx_context(
                storage_key, {"deviceId": self._bind_params.id, "screenId": self._screen_id or ""}
            )

    async def end(self, error: Exception | None = None) -> None:
        """Tear down the session (best-effort notify remote, then reset local state)."""
        if self._status in ("stopped", "stopping"):
            return
        self._status = "stopping"

        try:
            self.pairing.stop()
            if self._rpc:
                self._rpc.close()
                self._rpc = None
            self._clear_deferred()
            self._cancel_message_tasks()
            try:
                await self.send_message(lounge_screen_disconnected())
            except LOUNGE_API_ERRORS as err:
                self._logger.debug(
                    "(%s) Could not notify remote of disconnect: %s", self.client.name, err
                )
        except LOUNGE_API_ERRORS as err:
            if not error:
                raise SessionError(f"({self.client.name}) Error while ending session") from err
        finally:
            self._clear_token_refresh_timer()
            self._bind_params.reset()
            self._status = "stopped"

        if error:
            await self._on_terminate(error)

    async def restart(self) -> None:
        """End and re-begin the session, preserving pairing-code-service state."""
        self._logger.debug("(%s) Restarting session...", self.client.name)
        pairing_was_running = self.pairing.status == "running"
        try:
            await self.end()
            await self.begin()
            if pairing_was_running:
                self.pairing.start()
        except LOUNGE_API_ERRORS as err:
            raise SessionError(f"({self.client.name}) Error while restarting session") from err

    async def register_pairing_code(self, code: str) -> None:
        """
        Register a manually-entered ("Link with TV") pairing code with this session.

        :param code: The code the user typed into the YouTube app.
        """
        self._logger.debug("(%s) Registering pairing code: %s", self.client.name, code)
        if not self._screen_id:
            raise IncompleteAPIDataError("Missing screenId to register pairing code", ["screenId"])
        data = {
            "access_type": "permanent",
            "app": self._screen_app,
            "pairing_code": code,
            "screen_id": self._screen_id,
            "screen_name": self._screen_name,
            "device_id": self._bind_params.id,
        }
        async with self._http.post(URL_REGISTER_PAIRING_CODE, data=data) as response:
            if not response.ok:
                raise SessionError(
                    f"({self.client.name}) Failed to register pairing code: {response.status}"
                )
        self._logger.debug("(%s) Pairing code registered.", self.client.name)

    async def send_message(
        self, messages: Message | list[Message], defer_key: str | None = None, defer_ms: int = 0
    ) -> bool:
        """
        Send one or more outgoing messages, in order relative to other send_message calls.

        :param messages: The Message(s) to send.
        :param defer_key: If set, coalesce rapid repeated sends under the same key (e.g. volume
            drag events) - only the last call within `defer_ms` of each other actually sends.
        :param defer_ms: Debounce window in milliseconds, used with `defer_key`.
        """
        if defer_key:
            existing = self._deferred.pop(defer_key, None)
            if existing:
                existing.cancel()

            async def _deferred_send() -> None:
                await asyncio.sleep(defer_ms / 1000)
                self._deferred.pop(defer_key, None)
                await self._do_send(messages)

            self._deferred[defer_key] = asyncio.create_task(_deferred_send())
            return True

        return await self._do_send(messages)

    async def _on_pairing_response(self, code: str) -> None:
        if self._on_pairing_response_cb:
            await self._on_pairing_response_cb(code)

    async def _on_pairing_error(self, error: Exception) -> None:
        self._logger.error("(%s) Pairing code service error: %s", self.client.name, error)

    async def _refresh_after(self, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        try:
            await self._refresh_lounge_token()
        except LOUNGE_API_ERRORS as err:
            await self.end(err)

    async def _refresh_lounge_token(self) -> None:
        self._status = "refreshing"
        self._logger.debug("(%s) Refreshing lounge token...", self.client.name)
        self._clear_token_refresh_timer()

        old_rpc = self._rpc
        pairing_was_running = self.pairing.status == "running"
        self.pairing.stop()
        self._bind_params.reset()

        try:
            await self.begin()
        finally:
            self._logger.debug("(%s) Closing old RPC connection...", self.client.name)
            if old_rpc:
                old_rpc.close()

        if pairing_was_running:
            self.pairing.start()

        self._status = "running"

    async def _on_rpc_terminate(self, error: Exception) -> None:
        if self._status in ("stopping", "stopped"):
            # expected while shutting down: the HTTP session closes under the connection
            self._logger.debug(
                "(%s) RPC connection closed on shutdown: %s", self.client.name, error
            )
            return
        self._logger.error(
            "(%s) RPC connection terminated due to error: %s", self.client.name, error
        )
        try:
            await self._refresh_lounge_token()
        except LOUNGE_API_ERRORS as err:
            self._logger.error("(%s) Error refreshing lounge token: %s", self.client.name, err)

    def _handle_message(self, messages: list[Message]) -> None:
        self._bind_params.update_with_message(messages)
        task = asyncio.create_task(self._on_messages(messages, self))
        self._message_tasks.add(task)
        task.add_done_callback(self._on_message_task_done)

    def _on_message_task_done(self, task: asyncio.Task[None]) -> None:
        self._message_tasks.discard(task)
        if not task.cancelled() and (err := task.exception()):
            self._logger.error(
                "(%s) Error handling incoming messages: %s", self.client.name, err, exc_info=err
            )

    def _cancel_message_tasks(self) -> None:
        # end() can run from within a message handler (e.g. a disconnect), never cancel that one
        current = asyncio.current_task()
        for task in self._message_tasks:
            if task is not current:
                task.cancel()

    async def _restore_mdx_context(self, storage_key: str) -> bool:
        stored = await self._load_mdx_context(storage_key)
        self._logger.debug(
            "(%s) Configuring session with stored MDX context: %s", self.client.name, stored
        )
        if not stored:
            return False
        self._bind_params.update_with_mdx_context(stored.get("deviceId"))
        if not stored.get("screenId"):
            return False
        self._screen_id = stored["screenId"]
        return True

    async def _init_session(self, screen_id_from_store: bool) -> None:
        self._ofs = 0
        if not self._screen_id:
            self._screen_id = await self._generate_screen_id()

        try:
            token = await self._get_lounge_token()
        except LOUNGE_API_ERRORS:
            if not screen_id_from_store:
                raise
            self._logger.error(
                "(%s) Failed to obtain lounge token with stored screen id (%s); "
                "generating a fresh one and retrying...",
                self.client.name,
                self._screen_id,
            )
            self._screen_id = await self._generate_screen_id()
            token = await self._get_lounge_token()

        self._bind_params.update_with_lounge_token(token["loungeToken"])

        self._clear_token_refresh_timer()
        refresh_ms = token.get("refreshIntervalInMillis") or _FALLBACK_REFRESH_SECONDS * 1000
        self._token_refresh_task = asyncio.create_task(self._refresh_after(refresh_ms / 1000))

        init_messages = await self._get_init_session_messages()
        for m in init_messages:
            if m.name in ("c", "S"):
                self._bind_params.update_with_message(m)
            elif m.name == "loungeStatus":
                await self._on_messages([m], self)

        # Sanity check that we now have everything needed to talk on the rpc channel.
        self._bind_params.to_query_string("rpc")

    async def _connect_rpc(self) -> None:
        self._rpc = RPCConnection(
            http=self._http,
            bind_params=self._bind_params,
            on_messages=self._handle_message,
            on_terminate=self._on_rpc_terminate,
            logger=self._logger,
        )
        await self._rpc.connect()
        self._logger.debug("(%s) Session established.", self.client.name)

    async def _generate_screen_id(self) -> str:
        async with self._http.get(URL_GENERATE_SCREEN_ID) as response:
            screen_id = (await response.text()).strip()
        self._logger.debug("(%s) Obtained screen ID: %s", self.client.name, screen_id)
        return screen_id

    async def _get_lounge_token(self) -> dict[str, Any]:
        if not self._screen_id:
            raise IncompleteAPIDataError("Missing screenId to get lounge token", ["screenId"])
        async with self._http.post(
            URL_GET_LOUNGE_TOKEN_BATCH, data={"screen_ids": self._screen_id}
        ) as response:
            body = await response.json(content_type=None)
        screens = body.get("screens") if isinstance(body, dict) else None
        token = screens[0] if isinstance(screens, list) and screens else None
        if not isinstance(token, dict) or not token.get("loungeToken"):
            raise SessionError(f"({self.client.name}) Unexpected lounge token response: {body}")
        self._logger.debug("(%s) Obtained lounge token: %s", self.client.name, token)
        return token

    async def _get_init_session_messages(self) -> list[Message]:
        url = f"{URL_BIND}?{self._bind_params.to_query_string('initSession')}"
        async with self._http.post(url, data={"count": "0"}) as response:
            body = await response.text()
        messages = parse_incoming(body)
        self._logger.debug(
            "(%s) Received messages for establishing session: %s", self.client.name, messages
        )
        return messages

    async def _do_send(self, sc: Message | list[Message]) -> bool:
        messages = sc if isinstance(sc, list) else [sc]
        aid = messages[0].aid if messages else None

        async with self._send_lock:
            url = f"{URL_BIND}?{self._bind_params.to_query_string('sendMessage', aid)}"
            post_data = self._build_send_payload(messages)
            names = " + ".join(m.name for m in messages)
            self._logger.debug(
                "%s(%s) Sending message(s) '%s' with payload: %s",
                f"(AID: {aid}) " if aid else "",
                self.client.name,
                names,
                post_data,
            )
            try:
                async with self._http.post(url, data=post_data) as response:
                    ok = response.ok
                    status = response.status
            except aiohttp.ClientError as err:
                raise SessionError(
                    f"({self.client.name}) Connection error sending '{names}'"
                ) from err
            self._logger.debug(
                "%s(%s) Response received for '%s'. Status: %s",
                f"(AID: {aid}) " if aid else "",
                self.client.name,
                names,
                status,
            )
            if not ok:
                raise SessionError(
                    f"({self.client.name}) Bad response ({status}) sending '{names}'"
                )
            return True

    def _build_send_payload(self, states: list[Message]) -> dict[str, object]:
        payload: dict[str, object] = {"count": len(states), "ofs": str(self._ofs)}
        for index, state in enumerate(states):
            prefix = f"req{index}_"
            payload[f"{prefix}_sc"] = state.name
            for key, value in (state.payload or {}).items():
                payload[f"{prefix}{key}"] = value
        self._ofs += len(states)
        return payload

    def _clear_deferred(self) -> None:
        for task in self._deferred.values():
            task.cancel()
        self._deferred.clear()

    def _clear_token_refresh_timer(self) -> None:
        if self._token_refresh_task:
            self._token_refresh_task.cancel()
            self._token_refresh_task = None


def _new_device_id() -> str:
    return str(uuid.uuid4())
