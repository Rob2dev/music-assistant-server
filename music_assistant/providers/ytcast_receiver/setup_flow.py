"""Setup flow for the YouTube Music Cast Receiver plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.models.setup_flow import SetupFlowError

from . import (
    CONF_MASS_PLAYER_ID,
    CONF_RECEIVER_NAME,
    DEFAULT_RECEIVER_NAME,
    PLAYER_ID_AUTO,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """
    Configure the target player and advertised receiver name.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    errors: dict[str, str] | None = None
    while True:
        prefill: dict[str, Any] = {**session.context.values, **setup_data}
        receiver_name = str(prefill.get(CONF_RECEIVER_NAME) or DEFAULT_RECEIVER_NAME)
        values = await session.form(
            [
                CONF_ENTRY_WARN_PREVIEW,
                _player_selector(session.mass, prefill.get(CONF_MASS_PLAYER_ID)),
                ConfigEntry(
                    key=CONF_RECEIVER_NAME,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=receiver_name,
                    value=receiver_name,
                ),
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        setup_data.update(values)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


def _player_selector(mass: MusicAssistant, selected_value: Any) -> ConfigEntry:
    """
    Return the target player selector, with automatic selection as the first option.

    Built here instead of with the shared helper, whose signature differs between releases.

    :param mass: The Music Assistant instance providing the current players.
    :param selected_value: Previously selected player id to prefill when still available.
    """
    options = [
        ConfigValueOption(PLAYER_ID_AUTO),
        *(
            ConfigValueOption(player.player_id, title=player.display_name)
            for player in sorted(
                mass.players.all_players(False, False),
                key=lambda player: player.display_name.lower(),
            )
        ),
    ]
    selected = (
        selected_value
        if any(option.value == selected_value for option in options)
        else PLAYER_ID_AUTO
    )
    return ConfigEntry(
        key=CONF_MASS_PLAYER_ID,
        type=ConfigEntryType.STRING,
        required=True,
        default_value=selected,
        value=selected,
        options=options,
    )
