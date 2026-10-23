"""Read API keys from a `.env` file (KEY=VALUE lines) so they never have to be typed or pasted anywhere.

Variables that are already set in the environment win over the file. Nothing is printed except the
names of the variables that were loaded, never their values. Set AGENTFORGE_NO_DOTENV=1 to skip it
(the tests do, so a real key can never leak into them).
"""

import os
from pathlib import Path

# the folder that contains the `compiler` package (backend/), where the README tells you to put .env
BACKEND_DIR = Path(__file__).resolve().parent.parent


def parse_env(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:
            values[key] = value
    return values


def load_env(paths=None) -> list:
    """Set variables from the first `.env` files found (the current folder, then backend/). Returns the names set."""
    if os.environ.get("AGENTFORGE_NO_DOTENV"):
        return []
    loaded = []
    for path in paths or [Path.cwd() / ".env", BACKEND_DIR / ".env"]:
        try:
            values = parse_env(Path(path).read_text(encoding="utf-8"))
        except OSError:
            continue
        for key, value in values.items():
            if key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded
