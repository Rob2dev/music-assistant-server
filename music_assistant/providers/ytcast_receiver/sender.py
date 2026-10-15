"""
Sender (connected device) info, parsed out of `loungeStatus` message payloads.

Ported from yt-cast-receiver's Sender.ts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .const import CLIENTS, Client
from .types import YtCastError


class SenderDataError(YtCastError):
    """Raised when sender data in a loungeStatus payload is missing required fields."""


@dataclass
class Sender:
    """A device (phone, desktop app, etc.) connected to this Lounge API session."""

    id: str
    name: str
    app: str | None
    client: Client | None
    capabilities: list[str] = field(default_factory=list)
    device: dict[str, Any] = field(default_factory=dict)
    user_name: str | None = None
    user_avatar_uri: str | None = None
    obfuscated_gaia_id: str | None = None
    owner_obfuscated_gaia_id: str | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Sender:
        """
        Parse a `loungeStatus` device entry into a Sender.

        :param data: One entry from the parsed `devices` array of a loungeStatus payload.
        :raises SenderDataError: if required fields (id, name/clientName, a known theme) are missing.
        """
        themes = {c.theme for c in CLIENTS.values()}
        if (
            not data.get("id")
            or not (data.get("name") or data.get("clientName"))
            or data.get("theme") not in themes
        ):
            raise SenderDataError(f"Invalid sender data: {data}")

        device: dict[str, Any] = {}
        if data.get("device"):
            try:
                device = json.loads(data["device"])
            except TypeError, ValueError:
                device = {}

        return cls(
            id=data["id"],
            name=data.get("name") or data.get("clientName") or "Unknown",
            app=data.get("app"),
            client=next((c for c in CLIENTS.values() if c.theme == data.get("theme")), None),
            capabilities=(data.get("capabilities") or "").split(",")
            if data.get("capabilities")
            else [],
            device=device,
            user_name=data.get("user"),
            user_avatar_uri=data.get("userAvatarUri"),
            obfuscated_gaia_id=data.get("obfuscatedGaiaId"),
            owner_obfuscated_gaia_id=data.get("ownerObfuscatedGaiaId"),
        )

    def supports_autoplay(self) -> bool:
        """Return whether this sender's capabilities include autoplay support."""
        return "atp" in self.capabilities

    def supports_mute(self) -> bool:
        """Return whether this sender's app is a desktop client (the only ones that mute properly)."""
        return bool(self.app and self.app.endswith("-desktop"))
