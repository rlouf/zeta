"""Compatibility alias for the Zeta agent loop."""

from __future__ import annotations

import sys

from . import loop as _loop
from .loop import *  # noqa: F403

sys.modules[__name__] = _loop
