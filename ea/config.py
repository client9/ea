"""
config.py

Loads configuration from config.toml at the project root.
"""

import tomllib
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent.parent / "config.toml"


def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise SystemExit(
            f"config.toml not found at {_CONFIG_PATH}\n"
            "Copy the example from docs/install-macos.md and edit it before running EA."
        )
    with open(_CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def get_my_email() -> str:
    return load_config()["user"]["email"]
