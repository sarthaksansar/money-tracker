"""Every test runs on fresh fake data: generate_sample.py writes a full set of statements
to a temporary folder, and a temporary config points the tracker at it. This has to
happen before analyze (and friends) are imported, because they read the config then."""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import generate_sample  # noqa: E402

END = date(2026, 10, 5)
WORK = Path(tempfile.mkdtemp(prefix="money-tracker-tests-"))
EVENTS = generate_sample.generate(WORK / "data", END)

example = (ROOT / "config.example.toml").read_text(encoding="utf-8")
config = (example.replace('data_dir = "sample_data"', f"data_dir = {(WORK / 'data').as_posix()!r}")
          .replace('rules_file = "rules.example.csv"', f"rules_file = {(ROOT / 'rules.example.csv').as_posix()!r}"))
(WORK / "config.toml").write_text(config, encoding="utf-8")
os.environ["MONEY_TRACKER_CONFIG"] = str(WORK / "config.toml")
