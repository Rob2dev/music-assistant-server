"""Tests for the background tasks a Lounge API session and its pairing service run."""

import asyncio
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.ytcast_receiver.const import CLIENT_YTMUSIC
from music_assistant.providers.ytcast_receiver.message import Message
from music_assistant.providers.ytcast_receiver.session import Session
from music_assistant.providers.ytcast_receiver.types import ScreenInfo

if TYPE_CHECKING:
    import pytest


def _make_session(on_messages: AsyncMock) -> Session:
    return Session(
        client=CLIENT_YTMUSIC,
        screen=ScreenInfo(name="Music Assistant", app="ytcr", brand="Generic", model="SmartTV"),
        http=MagicMock(),
        load_mdx_context=AsyncMock(return_value=None),
        save_mdx_context=AsyncMock(),
        on_messages=on_messages,
        on_terminate=AsyncMock(),
        logger=logging.getLogger("test.ytcast.session"),
    )


async def test_message_handler_errors_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A failing message handler surfaces in the log instead of being silently lost."""
    session = _make_session(AsyncMock(side_effect=ValueError("boom")))
    with caplog.at_level(logging.ERROR):
        session._handle_message([Message(1, "pause")])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert "boom" in caplog.text
    assert not session._message_tasks


async def test_end_cancels_pending_message_handling() -> None:
    """Messages still being handled when the session ends are cancelled."""
    started = asyncio.Event()

    async def _slow(_messages: list[Message], _session: Session) -> None:
        started.set()
        await asyncio.sleep(3600)

    session = _make_session(AsyncMock(side_effect=_slow))
    session._status = "running"
    session.send_message = AsyncMock(return_value=True)  # type: ignore[method-assign]
    session._handle_message([Message(1, "pause")])
    await started.wait()
    (task,) = session._message_tasks

    await session.end()
    await asyncio.sleep(0)
    assert task.cancelled()


async def test_pairing_stop_cancels_running_request() -> None:
    """Stopping the pairing service cancels a code request that is still running."""
    session = _make_session(AsyncMock())
    started = asyncio.Event()

    async def _slow() -> None:
        started.set()
        await asyncio.sleep(3600)

    session.pairing._get_code_and_emit = _slow  # type: ignore[method-assign]
    session.pairing.start()
    await started.wait()
    task = session.pairing._fetch_task
    assert task is not None

    session.pairing.stop()
    await asyncio.sleep(0)
    assert task.cancelled()
