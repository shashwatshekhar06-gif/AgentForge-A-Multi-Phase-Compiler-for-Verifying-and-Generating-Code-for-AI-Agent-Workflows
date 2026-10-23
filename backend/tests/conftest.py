"""Shared test setup."""

import os

# The compiler and the generated files read API keys from a .env file. The tests must never pick up a real key
# from the developer's machine (a "no key" test would then call a paid API), so reading .env is switched off.
# tests/test_envfile.py turns it back on, in a temporary folder, to test the loader itself.
os.environ["AGENTFORGE_NO_DOTENV"] = "1"
