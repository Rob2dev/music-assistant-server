"""
Session/query-string state for the Lounge API ``bind`` endpoint.

Ported from yt-cast-receiver's BindParams.ts. Tracks the SID/RID/AID/gsessionid bookkeeping
Google's Lounge API expects across the three request shapes (``initSession``, ``sendMessage``,
``rpc``) - getting the AID/RID sequencing wrong silently breaks the session rather than raising
an error, so this mirrors the reference implementation's increment logic exactly.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlencode

from .message import Message

_RID_MIN = 41000
_RID_MAX = 49999


class IncompleteAPIDataError(Exception):
    """Raised when a query string is requested but required session data isn't set yet."""

    def __init__(self, message: str, missing: list[str]) -> None:
        """
        Initialize the error.

        :param message: Human-readable description of what's missing.
        :param missing: The names of the missing fields.
        """
        super().__init__(f"{message}: missing {missing}")
        self.missing = missing


def _generate_zx() -> str:
    return uuid.uuid4().hex[:12]


def _generate_rid() -> int:
    return random.randint(_RID_MIN, _RID_MAX)  # noqa: S311 - request counter seed, not a secret


@dataclass
class DeviceInfo:
    """Device metadata advertised to YouTube, per BindParams.ts's DEFAULT_DEVICE_INFO."""

    brand: str
    model: str
    year: int = 0
    os: str = "Windows"
    os_version: str = "10.0"
    chipset: str = ""
    client_name: str = "TVHTML5"
    dial_additional_data_support_level: str = "unsupported"
    mdx_dial_server_type: str = "MDX_DIAL_SERVER_TYPE_UNKNOWN"

    def to_json_dict(self) -> dict[str, object]:
        """Return the camelCase dict shape the Lounge API expects for `deviceInfo`."""
        return {
            "year": self.year,
            "os": self.os,
            "osVersion": self.os_version,
            "chipset": self.chipset,
            "clientName": self.client_name,
            "dialAdditionalDataSupportLevel": self.dial_additional_data_support_level,
            "mdxDialServerType": self.mdx_dial_server_type,
            "brand": self.brand,
            "model": self.model,
        }


@dataclass
class BindParams:
    """Mutable session/query-string state for one Session's Lounge API connection."""

    device_id: str
    screen_name: str
    screen_app: str
    theme: str
    brand: str
    model: str

    device: str = "LOUNGE_SCREEN"
    obfuscated_gaia_id: str = ""
    capabilities: str = "dsp,mic,dpa,ntb"
    cst: str = "m"
    mdx_version: int = 2
    ver: int = 8
    v: int = 2
    cver: int = 1
    t: int = 1

    lounge_id_token: str | None = None
    sid: str | None = None
    gsessionid: str | None = None

    id: str = field(init=False)
    aid: int = field(init=False, default=3)
    rid: int = field(init=False)
    device_info: DeviceInfo = field(init=False)

    def __post_init__(self) -> None:
        """Finish initializing derived fields (id, device_info, rid)."""
        self.id = self.device_id
        self.device_info = DeviceInfo(brand=self.brand, model=self.model)
        self.rid = _generate_rid()

    def reset(self) -> None:
        """Clear per-session tokens and restart the RID/AID sequence, per BindParams.reset()."""
        self.sid = None
        self.lounge_id_token = None
        self.gsessionid = None
        self.rid = _generate_rid()
        self.aid = 3

    def update_with_mdx_context(self, device_id: str | None) -> None:
        """Adopt a previously-stored device id, if any (restores session identity across restarts)."""
        if device_id:
            self.id = device_id

    def update_with_lounge_token(self, lounge_token: str) -> None:
        """Store the lounge token obtained from `get_lounge_token_batch`."""
        self.lounge_id_token = lounge_token

    def update_with_message(self, messages: Message | list[Message]) -> None:
        """
        Update SID/gsessionid/AID from an incoming 'c'/'S' message (or a batch of messages).

        :param messages: A single Message or list of Messages to fold into session state.
        """
        if isinstance(messages, list):
            for m in messages:
                self.update_with_message(m)
            return

        if messages.name == "c":
            # e.g. [0, ["c", "SID here", "", 8]]
            self.sid = messages.payload[0]
        elif messages.name == "S":
            # e.g. [1, ["S", "gsessionid here"]]
            self.gsessionid = messages.payload

        if messages.aid:
            self.aid = max(self.aid, messages.aid)

    def to_query_string(
        self, kind: Literal["initSession", "sendMessage", "rpc"], aid: int | None = None
    ) -> str:
        """
        Build the query string for the given Lounge API request type.

        :param kind: Which request shape to build params for.
        :param aid: For 'sendMessage', the AID to echo back (matches the incoming message
            this is a response to); if not given the internal counter is used and advanced.
        :raises IncompleteAPIDataError: if required session data (loungeIdToken, and for
            'sendMessage'/'rpc' also SID/gsessionid) hasn't been obtained yet.
        """
        missing = []
        if not self.lounge_id_token:
            missing.append("loungeIdToken")
        if kind in ("sendMessage", "rpc"):
            if not self.sid:
                missing.append("SID")
            if not self.gsessionid:
                missing.append("gsessionid")
        if missing:
            raise IncompleteAPIDataError("Missing data required to construct query string", missing)

        common: dict[str, object] = {
            "device": self.device,
            "id": self.id,
            "obfuscatedGaiaId": self.obfuscated_gaia_id,
            "name": self.screen_name,
            "app": self.screen_app,
            "theme": self.theme,
            "capabilities": self.capabilities,
            "cst": self.cst,
            "mdxVersion": self.mdx_version,
            "loungeIdToken": self.lounge_id_token,
            "VER": self.ver,
            "v": self.v,
            "zx": _generate_zx(),
            "t": self.t,
        }

        params: dict[str, object]
        if kind == "initSession":
            params = {
                **common,
                "deviceInfo": json.dumps(self.device_info.to_json_dict()),
                "RID": self.rid,
                "CVER": self.cver,
            }
            self.rid += 1
        elif kind == "sendMessage":
            self.aid = max(aid, self.aid) if aid and self.aid else (aid or self.aid)
            params = {
                **common,
                "deviceInfo": json.dumps(self.device_info.to_json_dict()),
                "SID": self.sid,
                "RID": self.rid,
                "AID": self.aid,
                "gsessionid": self.gsessionid,
            }
            if not aid:
                self.aid += 1
            self.rid += 1
        else:  # rpc
            params = {
                **common,
                "RID": "rpc",
                "SID": self.sid,
                "CI": 0,
                "AID": self.aid,
                "gsessionid": self.gsessionid,
                "TYPE": "xmlhttp",
            }

        return urlencode(params)
