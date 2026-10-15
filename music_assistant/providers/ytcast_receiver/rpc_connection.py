"""
Long-lived streaming connection to the Lounge API 'bind' endpoint.

Ported from yt-cast-receiver's RPCConnection.ts. Google's bind protocol streams newline-delimited
``[AID,["name",payload]]`` frames over a long-lived chunked HTTP response; this class reads that
stream, parses complete lines into Messages, and auto-reconnects when the remote end closes the
connection (which happens routinely - Google recycles these connections periodically).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import aiohttp

from .const import URL_BIND
from .message import Message, parse_incoming

if TYPE_CHECKING:
    from logging import Logger

    from .bind_params import BindParams

MAX_RETRIES = 3


class RPCConnectionError(Exception):
    """Raised when the RPC connection cannot be established after retries."""


class RPCConnection:
    """Manages the long-poll connection that delivers incoming Lounge API messages."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        bind_params: BindParams,
        on_messages: Callable[[list[Message]], None],
        on_terminate: Callable[[Exception], Awaitable[None]],
        logger: Logger,
    ) -> None:
        """
        Initialize the connection (does not connect yet - call `connect()`).

        :param http: Shared client session used for the streaming GET.
        :param bind_params: Session's query-string state; read fresh on every (re)connect.
        :param on_messages: Called synchronously with each batch of parsed Messages.
        :param on_terminate: Awaited when reconnection ultimately fails; the connection is
            dead at that point and the owning Session must re-establish everything.
        :param logger: Logger to use.
        """
        self._http = http
        self._bind_params = bind_params
        self._on_messages = on_messages
        self._on_terminate = on_terminate
        self._logger = logger
        self._status = "disconnected"
        self._response: aiohttp.ClientResponse | None = None
        self._task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        """Establish the connection if not already connected/connecting."""
        if self._status == "disconnected":
            await self._do_connect()

    def close(self) -> None:
        """Close the connection; does not trigger auto-reconnect."""
        if self._status in ("connected", "connecting", "reconnecting"):
            self._logger.debug("Closing RPC connection...")
            self._status = "disconnecting"
            if self._response:
                self._response.close()
            if self._task:
                self._task.cancel()

    async def _do_connect(self, is_reconnect: bool = False, retry: int = 0) -> None:
        self._status = "reconnecting" if is_reconnect else "connecting"
        url = f"{URL_BIND}?{self._bind_params.to_query_string('rpc')}"
        self._logger.debug("Connecting to RPC URL: %s", url)

        try:
            response = await self._http.get(
                url, timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            self._logger.error("RPC connection error: %s", err)
            retry += 1
            if retry <= MAX_RETRIES:
                self._logger.error("Retrying %d / %d", retry, MAX_RETRIES)
                await self._do_connect(is_reconnect, retry)
                return
            self._status = "disconnected"
            raise RPCConnectionError(f"RPC connection error: {url}") from err

        if response.ok:
            self._logger.debug("RPC connection established.")
            self._status = "connected"
            self._response = response
            self._task = asyncio.create_task(self._read_loop(response))
            return

        self._status = "disconnected"
        response.close()
        raise RPCConnectionError(f"RPC connection request returned bad response: {response.status}")

    async def _read_loop(self, response: aiohttp.ClientResponse) -> None:
        buffer = ""
        try:
            async for chunk in response.content.iter_any():
                if self._status != "connected":
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                *complete_lines, buffer = buffer.split("\n")
                for line in complete_lines:
                    if not line.strip():
                        continue
                    messages = parse_incoming(line)
                    if messages:
                        self._on_messages(messages)
        except asyncio.CancelledError:
            raise
        except aiohttp.ClientError as err:
            self._logger.error("RPC connection reader error: %s", err)
        finally:
            await self._handle_disconnect()

    async def _handle_disconnect(self) -> None:
        if self._status == "disconnected":
            return

        prev_status = self._status
        self._status = "disconnected"
        if self._response:
            self._response.close()
            self._response = None

        if prev_status == "connected":
            self._logger.debug("RPC connection disconnected. Reconnecting...")
            try:
                await self._do_connect(True)
            except Exception as err:
                await self._on_terminate(err)
        else:
            self._logger.debug("RPC connection closed.")
