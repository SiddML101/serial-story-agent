"""Settings from .env plus the model pricing table."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
ENV_FILE = ROOT / ".env"

TOTAL_EPISODES = 200
EPISODE_WORDS = (400, 700)

# Regexes (case-insensitive). Merged with the bible's own banned_phrases.
DEFAULT_BANNED_PHRASES = [
    "a testament to", "the air was thick with", "little did (he|she|they) know", "sent shivers down",
    "in that moment", "couldn't help but", "a sense of", "it was as if", "the weight of", "echoed through",
    "tapestry", "palpable",
]

# USD per 1M tokens. Fill these in from the provider's pricing page.
# We run on the free tier (actual spend is always $0); these give list_price_usd, which drives the
# per-episode budget cap and the 200-episode cost estimate.
PRICING: dict[str, dict[str, float]] = {
    "gemini-3.8-flash": {"input_per_1m": 0.50, "output_per_1m": 3.00},  # PLACEHOLDER: verify
    "gemini-3.5-flash-lite": {"input_per_1m": 0.10, "output_per_1m": 0.40},  # PLACEHOLDER: verify
    "gemini-3.1-flash-lite": {"input_per_1m": 0.10, "output_per_1m": 0.40},  # PLACEHOLDER: verify
    "gemini-3.7-flash": {"input_per_1m": 0.50, "output_per_1m": 3.00},  # PLACEHOLDER: verify
    "gemini-3.6-flash": {"input_per_1m": 0.50, "output_per_1m": 3.00},  # PLACEHOLDER: verify
    "gemini-3.5-flash": {"input_per_1m": 0.30, "output_per_1m": 2.50},  # PLACEHOLDER: verify
}
# Used for any model missing from PRICING. Deliberately pessimistic so the budget cap errs on the safe side.
DEFAULT_PRICE = {"input_per_1m": 1.00, "output_per_1m": 5.00}


def price_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    p = PRICING.get(model, DEFAULT_PRICE)
    return (tokens_in * p["input_per_1m"] + tokens_out * p["output_per_1m"]) / 1_000_000


@dataclass(frozen=True)
class Settings:
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    writer_model: str = ""
    fast_model: str = ""
    max_cost_per_episode_usd: float = 0.15
    max_revisions: int = 2
    min_seconds_between_calls: float = 4.0
    db_path: Path = ROOT / "story.db"
    runs_dir: Path = ROOT / "runs"


API_KEY_RE = re.compile(r"^[A-Za-z0-9._\-]{20,200}$")


def key_hint(key: str) -> str:
    """What the UI may show of a secret: never more than the last 4 characters."""
    return f"••••{key[-4:]}" if len(key) >= 12 else ("set" if key else "")


def set_env_value(name: str, value: str, env_file: Path | None = None) -> None:
    """Write NAME=value into .env (keeping every other line) and into this process's environment."""
    if "\n" in value or "\r" in value:
        raise ValueError("value must be a single line")
    path = env_file or ENV_FILE
    if not path.exists():
        example = ROOT / ".env.example"
        path.write_text(example.read_text(encoding="utf-8") if example.exists() else "", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == name and not line.lstrip().startswith("#"):
            out.append(f"{name}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{name}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[name] = value  # load_dotenv never overrides, so later load_settings() calls see the new value


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or ENV_FILE)
    db_path = Path(os.getenv("DB_PATH", "story.db"))
    runs_dir = Path(os.getenv("RUNS_DIR", "runs"))
    return Settings(
        base_url=os.getenv("LLM_BASE_URL", ""),
        api_key=os.getenv("LLM_API_KEY", ""),
        writer_model=os.getenv("WRITER_MODEL", ""),
        fast_model=os.getenv("FAST_MODEL", ""),
        max_cost_per_episode_usd=float(os.getenv("MAX_COST_PER_EPISODE_USD", "0.15")),
        max_revisions=int(os.getenv("MAX_REVISIONS", "2")),
        min_seconds_between_calls=float(os.getenv("MIN_SECONDS_BETWEEN_CALLS", "4")),
        db_path=db_path if db_path.is_absolute() else ROOT / db_path,
        runs_dir=runs_dir if runs_dir.is_absolute() else ROOT / runs_dir,
    )
