"""Settings, read from the process environment (Docker's env_file supplies them from .env)."""
from dataclasses import dataclass
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class Config:
    token: str
    channel_id: str
    rehoming_dir: Path
    state_dir: Path
    poll_seconds: int
    post_existing: bool
    attach_picture: bool
    max_pet_bytes: int
    max_pets: int


def load_config(environ=None) -> Config:
    """The settings, or ValueError naming the first one that is missing or invalid. The token is never echoed."""
    values = os.environ if environ is None else environ

    def text(key, default=''):
        return values.get(key, default).strip()

    def boolean(key, default):
        value = text(key, default).lower()
        if value not in ('true', 'false'):
            raise ValueError(f'{key} must be true or false')
        return value == 'true'

    def integer(key, default, low, high):
        value = text(key, str(default))
        if not re.fullmatch(r'[0-9]{1,10}', value) or not low <= int(value) <= high:
            raise ValueError(f'{key} must be an integer from {low} to {high}')
        return int(value)

    token = text('DISCORD_BOT_TOKEN')
    if not token or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError('DISCORD_BOT_TOKEN must be set to the bot token of the Discord application')
    channel_id = text('DISCORD_CHANNEL_ID')
    if not re.fullmatch(r'[0-9]{15,22}', channel_id):
        raise ValueError('DISCORD_CHANNEL_ID must be the numeric ID of the channel to post in')
    return Config(
        token, channel_id,
        Path(text('REHOMING_DIR', '/rehoming')), Path(text('STATE_DIR', '/state')),
        integer('POLL_SECONDS', 5, 1, 3600),
        boolean('POST_EXISTING', 'false'),
        boolean('ATTACH_PICTURE', 'true'),
        # The defaults are the largest values the game server accepts for its own two limits
        # (PETZ_REHOMING_MAX_PET_BYTES, PETZ_REHOMING_MAX_PETS), so they fit every server.
        integer('MAX_PET_BYTES', 1048576, 65536, 1048576),
        integer('MAX_PETS', 10000, 1, 10000))
