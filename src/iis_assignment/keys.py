"""Where the API key comes from."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def api_key():
    """OPENROUTER_API_KEY from the environment or .env, else None."""
    if "OPENROUTER_API_KEY" in os.environ:
        return os.environ["OPENROUTER_API_KEY"] or None
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENROUTER_API_KEY":
                return value.strip() or None
    except OSError:
        pass
    return None
