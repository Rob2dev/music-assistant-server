"""
YouTube Music Cast Receiver plugin for Music Assistant.

Lets the YouTube and YouTube Music Android apps "Cast" directly to a Music Assistant player,
using the same DIAL discovery + Lounge API protocol those apps use to cast to a real TV. The
protocol implementation is a pure-Python port of patrickkfkan/yt-cast-receiver; incoming video
ids are played through the ytmusic provider, which handles the actual stream resolution.

Structurally this mirrors airplay_receiver: a target-player selector (explicit or "auto"), a
receiver identity name, and a persistent background service (DialServer + two Lounge API
Sessions) started in handle_async_init and torn down in unload.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import uuid
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, EventType, PlaybackState, ProviderFeature

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW, MASS_LOGGER_NAME
from music_assistant.models.plugin import PluginProvider

from .const import RESET_ON_DISCONNECT_ALL_EXPLICIT
from .dial_server import DialServer, DialServerOptions
from .mass_player import MassPlayer
from .types import ScreenInfo
from .youtube_app import YouTubeApp

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_RECEIVER_NAME = "receiver_name"
CONF_CAST_STATUS = "cast_status"
DEFAULT_RECEIVER_NAME = "Music Assistant"
PLAYER_ID_AUTO = "__auto__"

SUPPORTED_FEATURES: set[ProviderFeature] = set()


def _dial_port(instance_id: str) -> int:
    """
    Return the DIAL port for a receiver instance.

    Deterministically derived from the instance id (stable across restarts, same idea as
    airplay_receiver's airplay_receiver_port()) so multiple instances of this
    multi_instance provider don't collide on the same hardcoded port.
    """
    digest = hashlib.md5(instance_id.encode(), usedforsecurity=False).hexdigest()
    return 8100 + int(digest, 16) % 400


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return YtCastReceiverProvider(mass, manifest, config)


class YtCastReceiverProvider(PluginProvider):
    """Runs a DIAL + Lounge API receiver and bridges it onto a real MA player."""

    reload_on_streams_network_change = True

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self._default_player_id: str = (
            cast("str", self.get_setup_value(CONF_MASS_PLAYER_ID)) or PLAYER_ID_AUTO
        )
        self._receiver_name = (
            cast("str", self.get_setup_value(CONF_RECEIVER_NAME)) or DEFAULT_RECEIVER_NAME
        )
        self._http: aiohttp.ClientSession | None = None
        self._player: MassPlayer | None = None
        self._app: YouTubeApp | None = None
        self._dial: DialServer | None = None
        self._pairing_codes: dict[str, str] = {}
        self._unsub_player_updated: Callable[[], None] | None = None
        self._unsub_queue_updated: Callable[[], None] | None = None
        self._target_missing_logged = False

    @property
    def instance_name_postfix(self) -> str | None:
        """Return the advertised receiver name as the multi-instance postfix."""
        return self._receiver_name if self._receiver_name != DEFAULT_RECEIVER_NAME else None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return runtime options for this provider, including live pairing codes if available."""
        return (CONF_ENTRY_WARN_PREVIEW, self._status_entry(), *self._pairing_code_entries())

    async def handle_async_init(self) -> None:
        """Start the DIAL server and both Lounge API sessions."""
        # mass.streams.get_source_ip() answers "which interface reaches THIS target" and
        # needs a target_ip to resolve anything - it returns None for our case, since SSDP
        # is broadcast/multicast discovery with no known target ahead of time. We need "what
        # LAN IP should I advertise to whoever's listening", which is a different question;
        # airplay_receiver's mDNS advertisement can shrug off a None here and bind all
        # interfaces, but SSDP/DIAL's LOCATION header needs one concrete, real IP up front.
        host = await _detect_local_ip()
        if not host:
            self.unload_with_error(
                "Could not determine a local IP address to bind the receiver to."
            )
            return

        # self.logger is still the domain-named logger during init; MA swaps in the
        # instance-named one (getChild(self.name)) afterwards. The long-lived components get
        # that same instance logger right away, so their lines can be told apart per receiver.
        logger = logging.getLogger(MASS_LOGGER_NAME).getChild(self.name)
        logger.setLevel(self.logger.level)
        self._http = aiohttp.ClientSession()
        self._player = MassPlayer(self.mass, self._get_target_player_id, logger)
        self._app = YouTubeApp(
            player=self._player,
            screen=ScreenInfo(
                name=self._receiver_name, app="ytcr", brand="Generic", model="SmartTV"
            ),
            http=self._http,
            load_mdx_context=self._load_mdx_context,
            save_mdx_context=self._save_mdx_context,
            logger=logger,
            # only stop playback on an explicit user "disconnect" - an implicit disconnect
            # (phone locked/backgrounded, brief WiFi hiccup) should leave the speaker playing,
            # matching how casting normally behaves elsewhere (Chromecast/Sonos).
            reset_player_on_disconnect_policy=RESET_ON_DISCONNECT_ALL_EXPLICIT,
        )
        await self._app.start(self._get_pid, self._save_pid)
        # keep manual "Link with TV code" pairing available as a fallback to SSDP auto-discovery
        yt_session = self._app.get_session("YT")
        ytmusic_session = self._app.get_session("YTMUSIC")
        yt_session.set_pairing_response_handler(self._log_pairing_code("YouTube"))
        ytmusic_session.set_pairing_response_handler(self._log_pairing_code("YouTube Music"))
        yt_session.pairing.start()
        ytmusic_session.pairing.start()

        dial_opts = DialServerOptions(
            host=host,
            port=_dial_port(self.instance_id),
            friendly_name=self._receiver_name,
            manufacturer="Generic",
            model_name="SmartTV",
            # stable per instance, so the phone sees the same device across restarts and each
            # instance (one per target player) as a separate one
            device_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"ytcast_receiver/{self.instance_id}")),
        )
        self._dial = DialServer(apps={"YouTube": self._app}, options=dial_opts, logger=logger)
        await self._dial.start()

        # notify connected senders when MA moves the target player on its own (track ended
        # naturally, or another controller changed playback) - none of our own command-triggered
        # code paths would otherwise catch this, leaving the phone's UI showing stale info.
        self._unsub_player_updated = self.mass.subscribe(
            self._on_player_updated, EventType.PLAYER_UPDATED
        )
        self._unsub_queue_updated = self.mass.subscribe(
            self._on_queue_updated, EventType.QUEUE_UPDATED
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Tear down the DIAL server and both sessions."""
        if self._unsub_player_updated:
            self._unsub_player_updated()
            self._unsub_player_updated = None
        if self._unsub_queue_updated:
            self._unsub_queue_updated()
            self._unsub_queue_updated = None
        if self._dial:
            await self._dial.stop()
            self._dial = None
        if self._app:
            await self._app.stop()
            self._app = None
        self._player = None
        if self._http:
            await self._http.close()
            self._http = None

    def _status_entry(self) -> ConfigEntry:
        """Build a read-only field showing which devices are casting, and to which player."""
        status = self._status_text()
        return ConfigEntry(
            key=CONF_CAST_STATUS,
            type=ConfigEntryType.STRING,
            default_value=status,
            value=status,
            read_only=True,
            required=False,
        )

    def _status_text(self) -> str:
        senders = self._app.connected_senders if self._app else []
        if not senders:
            return "No device connected"
        devices = ", ".join(
            f"{sender.name} ({sender.client.name})" if sender.client else sender.name
            for sender in senders
        )
        target = self._get_target_player_id()
        player = self.mass.players.get_player(target) if target else None
        if not player:
            return f"{devices} - no target player available"
        status = f"{devices} -> {player.display_name}"
        if player.current_media and player.current_media.title:
            status += f": {player.current_media.title}"
        return status

    def _pairing_code_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Build read-only fields showing the current manual "Link with TV code" pairing codes.

        These refresh automatically: MA resolves get_config_entries() fresh every time the
        provider's options page is opened, so re-opening the page picks up the latest code
        (issued roughly every 5 minutes by PairingCodeRequestService).
        """
        entries = []
        for client_name in ("YouTube", "YouTube Music"):
            code = self._pairing_codes.get(client_name) or "Not yet available"
            entries.append(
                ConfigEntry(
                    key=f"pairing_code_{client_name.lower().replace(' ', '_')}",
                    type=ConfigEntryType.STRING,
                    default_value=code,
                    value=code,
                    read_only=True,
                    required=False,
                )
            )
        return tuple(entries)

    async def _on_player_updated(self, event: MassEvent) -> None:
        if not self._player or event.object_id != self._get_target_player_id():
            return
        await self._player.handle_external_update()

    async def _on_queue_updated(self, event: MassEvent) -> None:
        if not self._player or event.object_id != self._get_target_player_id():
            return
        await self._player.handle_queue_update()

    def _log_pairing_code(self, client_name: str) -> Callable[[str], Awaitable[None]]:
        """Return a handler that logs and stores each newly issued manual pairing code."""

        async def _handler(code: str) -> None:
            self._pairing_codes[client_name] = code
            self.logger.info(
                "Manual pairing code for %s (Settings > Link with TV code in the app): %s",
                client_name,
                code,
            )

        return _handler

    def _get_target_player_id(self) -> str | None:
        if self._default_player_id != PLAYER_ID_AUTO:
            if self.mass.players.get_player(self._default_player_id):
                self._target_missing_logged = False
                return self._default_player_id
            if not self._target_missing_logged:
                # this is polled on every state update; warn once per disappearance, not each time
                self._target_missing_logged = True
                self.logger.warning(
                    "Configured target player '%s' is not available", self._default_player_id
                )
            return None

        all_players = list(self.mass.players.all_players(False, False))
        for player in all_players:
            if player.playback_state == PlaybackState.PLAYING:
                return player.player_id
        if all_players:
            return all_players[0].player_id
        return None

    async def _load_mdx_context(self, key: str) -> dict[str, str] | None:
        # typed Any: setup values are declared as scalars, but older versions stored a raw dict
        stored: Any = self.get_setup_value(key)
        if isinstance(stored, dict):
            # stored as a raw dict by earlier versions of this provider
            return cast("dict[str, str]", stored)
        if not isinstance(stored, str) or not stored:
            return None
        try:
            return cast("dict[str, str]", json.loads(stored))
        except ValueError:
            return None

    async def _save_mdx_context(self, key: str, value: dict[str, str]) -> None:
        self._update_setup_data(key, json.dumps(value))

    async def _get_pid(self) -> str | None:
        return cast("str | None", self.get_setup_value("dial_app_pid"))

    async def _save_pid(self, pid: str) -> None:
        self._update_setup_data("dial_app_pid", pid)


async def _detect_local_ip() -> str | None:
    """
    Return this host's LAN-facing IP, for advertising in SSDP/DIAL responses.

    Uses the standard "connect" a UDP socket to a public address and read back the chosen
    source address" trick - no packets are actually sent, this only consults the routing
    table. Returns None if the host has no route to the public internet (e.g. fully offline).
    """

    def _resolve() -> str | None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.connect(("8.8.8.8", 80))
                return cast("str", sock.getsockname()[0])
            except OSError:
                return None

    return await asyncio.to_thread(_resolve)
