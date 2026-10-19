"""Lets the compiler run as `python -m compiler file.af`."""

import sys

from .cli import main

sys.exit(main())
