"""
config.py

Loads configuration from config.toml at the project root.
"""

import tomllib
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent.parent / "config.toml"


def load_config() -> dict:
    with open(_CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def get_my_email() -> str:
    return load_config()["user"]["email"]
