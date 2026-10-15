"""
Manual pairing code ("Link with TV" code) polling service.

Ported from yt-cast-receiver's PairingCodeRequestService.ts. Refreshes a manual pairing code
every 5 minutes for as long as the service runs; used as the discovery fallback when a phone
can't (or won't) find the receiver via SSDP/DIAL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import aiohttp

from .const import URL_GET_PAIRING_CODE

if TYPE_CHECKING:
    from logging import Logger

    from .bind_params import BindParams

_REFRESH_INTERVAL = 300  # 5 minutes
_REFRESH_INTERVAL_SHORT = 30  # when the API isn't ready yet (no lounge token/screen id)


class PairingCodeRequestService:
    """Polls YouTube for a manual pairing code and hands it to a callback as it changes."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        screen_id_getter: Callable[[], str | None],
        screen_name: str,
        screen_app: str,
        bind_params: BindParams,
        on_response: Callable[[str], Awaitable[None]],
        on_error: Callable[[Exception], Awaitable[None]],
        logger: Logger,
    ) -> None:
        """
        Initialize the service (does not start polling yet - call `start()`).

        :param http: Shared client session.
        :param screen_id_getter: Returns the current screen id, or None if not yet obtained.
        :param bind_params: Used to read the current lounge token and device id.
        :param on_response: Awaited with each newly obtained pairing code.
        :param on_error: Awaited (and the service stopped) if a request ultimately fails.
        """
        self._http = http
        self._screen_id_getter = screen_id_getter
        self._screen_name = screen_name
        self._screen_app = screen_app
        self._bind_params = bind_params
        self._on_response = on_response
        self._on_error = on_error
        self._logger = logger
        self._status = "stopped"
        self._timer_task: asyncio.Task[None] | None = None
        self._fetch_task: asyncio.Task[None] | None = None

    @property
    def status(self) -> str:
        """Return 'running' or 'stopped'."""
        return self._status

    def start(self) -> None:
        """Start polling for a pairing code."""
        if self._status == "running":
            return
        self._status = "running"
        self._fetch_task = asyncio.create_task(self._get_code_and_emit())
        self._fetch_task.add_done_callback(self._on_fetch_done)

    def stop(self) -> None:
        """Stop polling."""
        if self._status == "stopped":
            return
        self._clear_timer()
        if self._fetch_task:
            self._fetch_task.cancel()
            self._fetch_task = None
        self._status = "stopped"

    def _on_fetch_done(self, task: asyncio.Task[None]) -> None:
        if task is self._fetch_task:
            self._fetch_task = None
        if not task.cancelled() and (err := task.exception()):
            self._logger.error("Pairing code request failed: %s", err, exc_info=err)

    def _clear_timer(self) -> None:
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    def _start_timer(self, interval: int = _REFRESH_INTERVAL) -> None:
        self._clear_timer()

        async def _wait_and_refresh() -> None:
            await asyncio.sleep(interval)
            await self._get_code_and_emit()

        self._timer_task = asyncio.create_task(_wait_and_refresh())

    async def _get_code_and_emit(self) -> None:
        self._clear_timer()

        screen_id = self._screen_id_getter()
        if not self._bind_params.lounge_id_token or not screen_id:
            # API not ready yet - retry sooner than the normal 5 minute cadence.
            self._start_timer(_REFRESH_INTERVAL_SHORT)
            return

        data = {
            "access_type": "permanent",
            "app": self._screen_app,
            "lounge_token": self._bind_params.lounge_id_token,
            "screen_id": screen_id,
            "screen_name": self._screen_name,
            "device_id": self._bind_params.id,
        }

        error: Exception | None = None
        try:
            async with self._http.post(URL_GET_PAIRING_CODE, data=data) as response:
                code = (await response.text()).strip()
        except aiohttp.ClientError as err:
            error = err
        else:
            await self._on_response(code)
            self._start_timer()

        if error:
            self.stop()
            await self._on_error(error)
