"""
Constants for the YouTube Music Cast Receiver plugin.

Ported from yt-cast-receiver's Constants.ts (https://github.com/patrickkfkan/yt-cast-receiver).
"""

from __future__ import annotations

YOUTUBE_BASE_URL = "https://www.youtube.com"

URL_GENERATE_SCREEN_ID = f"{YOUTUBE_BASE_URL}/api/lounge/pairing/generate_screen_id"
URL_GET_LOUNGE_TOKEN_BATCH = f"{YOUTUBE_BASE_URL}/api/lounge/pairing/get_lounge_token_batch"
URL_REGISTER_PAIRING_CODE = f"{YOUTUBE_BASE_URL}/api/lounge/pairing/register_pairing_code"
URL_GET_PAIRING_CODE = f"{YOUTUBE_BASE_URL}/api/lounge/pairing/get_pairing_code?ctx=pair"
URL_BIND = f"{YOUTUBE_BASE_URL}/api/lounge/bc/bind"

SCREEN_APP = "ytcr"
BRAND = "Generic"
MODEL = "SmartTV"

# PLAYER_STATUSES values match the Lounge API's own numeric player state codes verbatim
# (sent/received on the wire) - do not renumber these.
PLAYER_STATUS_IDLE = -1
PLAYER_STATUS_PLAYING = 1
PLAYER_STATUS_PAUSED = 2
PLAYER_STATUS_LOADING = 3
PLAYER_STATUS_STOPPED = 4

AUTOPLAY_MODE_ENABLED = "ENABLED"
AUTOPLAY_MODE_DISABLED = "DISABLED"
AUTOPLAY_MODE_UNSUPPORTED = "UNSUPPORTED"

MUTE_POLICY_ZERO_VOLUME_LEVEL = "zeroLevel"
MUTE_POLICY_PRESERVE_VOLUME_LEVEL = "preserveLevel"
MUTE_POLICY_AUTO = "auto"

RESET_ON_DISCONNECT_ALL_EXPLICIT = "allExplicitlyDisconnected"
RESET_ON_DISCONNECT_ALL = "allDisconnected"


class Client:
    """Identifies which YouTube app (YouTube proper vs. YouTube Music) a session is for."""

    def __init__(self, key: str, theme: str, name: str) -> None:
        """
        Initialize a Client descriptor.

        :param key: Short identifier ("YT" or "YTMUSIC").
        :param theme: The Lounge API "theme" query param value that selects this client.
        :param name: Human-readable name for logging.
        """
        self.key = key
        self.theme = theme
        self.name = name


CLIENT_YT = Client("YT", "cl", "YouTube")
CLIENT_YTMUSIC = Client("YTMUSIC", "m", "YouTube Music")
CLIENTS = {"YT": CLIENT_YT, "YTMUSIC": CLIENT_YTMUSIC}
