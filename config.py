"""Your personal settings, kept out of the code.

config.toml (never committed) holds your real names, recipients and folders. Without
it, config.example.toml is used, which points at the fake data from generate_sample.py.
Set the MONEY_TRACKER_CONFIG environment variable to use some other file.
"""

import os
import tomllib
from pathlib import Path

HERE = Path(__file__).parent


def config_file():
    if os.environ.get("MONEY_TRACKER_CONFIG"):
        return Path(os.environ["MONEY_TRACKER_CONFIG"])
    own = HERE / "config.toml"
    return own if own.exists() else HERE / "config.example.toml"


def load(file=None):
    """Read the settings; folder paths become absolute (relative ones are relative
    to the config file, and ~ means your home folder)."""
    file = Path(file or config_file())
    with open(file, "rb") as f:
        cfg = tomllib.load(f)
    resolve = lambda p: (file.parent / Path(p).expanduser()).resolve()
    cfg["data_dir"] = resolve(cfg["data_dir"])
    cfg["rules_file"] = resolve(cfg.get("rules_file", "rules.csv"))
    cfg["file"] = file
    return cfg


CONFIG = load()
