"""Tests for the Lounge API bind query-string bookkeeping."""

from urllib.parse import parse_qs

import pytest

from music_assistant.providers.ytcast_receiver.bind_params import (
    BindParams,
    IncompleteAPIDataError,
)
from music_assistant.providers.ytcast_receiver.message import Message


def _params() -> BindParams:
    return BindParams(
        device_id="device-1",
        screen_name="Music Assistant",
        screen_app="ytcr",
        theme="m",
        brand="Generic",
        model="SmartTV",
    )


def _ready_params() -> BindParams:
    params = _params()
    params.update_with_lounge_token("token")
    params.update_with_message([Message(0, "c", ["SID1", "", 8]), Message(1, "S", "GSESS")])
    return params


def _query(qs: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(qs).items()}


def test_update_with_message_sets_session_ids_and_aid() -> None:
    """'c' and 'S' messages provide the SID and gsessionid; the AID only moves forward."""
    params = _params()
    params.update_with_message(
        [Message(10, "c", ["SID1", "", 8]), Message(4, "S", "GSESS"), Message(None, "noop")]
    )
    assert params.sid == "SID1"
    assert params.gsessionid == "GSESS"
    assert params.aid == 10


def test_query_string_requires_lounge_token() -> None:
    """Without a lounge token no request can be built."""
    with pytest.raises(IncompleteAPIDataError) as err:
        _params().to_query_string("initSession")
    assert err.value.missing == ["loungeIdToken"]


def test_rpc_query_string_requires_session_ids() -> None:
    """The rpc/sendMessage shapes additionally need the SID and gsessionid."""
    params = _params()
    params.update_with_lounge_token("token")
    with pytest.raises(IncompleteAPIDataError) as err:
        params.to_query_string("rpc")
    assert err.value.missing == ["SID", "gsessionid"]


def test_init_session_advances_rid() -> None:
    """Each initSession request uses the current RID and then increments it."""
    params = _params()
    params.update_with_lounge_token("token")
    rid = params.rid
    query = _query(params.to_query_string("initSession"))
    assert query["RID"] == str(rid)
    assert query["loungeIdToken"] == "token"
    assert query["name"] == "Music Assistant"
    assert params.rid == rid + 1


def test_send_message_without_aid_uses_and_advances_counter() -> None:
    """Unsolicited messages use the internal AID counter and advance it."""
    params = _ready_params()
    aid = params.aid
    query = _query(params.to_query_string("sendMessage"))
    assert query["AID"] == str(aid)
    assert query["SID"] == "SID1"
    assert params.aid == aid + 1


def test_send_message_with_aid_echoes_it_without_advancing() -> None:
    """A reply echoes the incoming AID and leaves the counter at that value."""
    params = _ready_params()
    query = _query(params.to_query_string("sendMessage", 20))
    assert query["AID"] == "20"
    assert params.aid == 20


def test_rpc_query_string_shape() -> None:
    """The long-poll rpc request uses the literal RID 'rpc'."""
    query = _query(_ready_params().to_query_string("rpc"))
    assert query["RID"] == "rpc"
    assert query["TYPE"] == "xmlhttp"
    assert query["gsessionid"] == "GSESS"


def test_reset_clears_session_state() -> None:
    """Reset drops all tokens and restarts the AID sequence."""
    params = _ready_params()
    params.aid = 42
    params.reset()
    assert params.sid is None
    assert params.gsessionid is None
    assert params.lounge_id_token is None
    assert params.aid == 3


def test_update_with_mdx_context_restores_device_id() -> None:
    """A stored device id replaces the generated one; an empty one is ignored."""
    params = _params()
    params.update_with_mdx_context(None)
    assert params.id == "device-1"
    params.update_with_mdx_context("stored-device")
    assert params.id == "stored-device"
