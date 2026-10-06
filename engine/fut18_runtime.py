"""Small runtime-policy helpers shared by the local FUT backend and tests.

Keep this module free of import-time filesystem writes. The main server owns
directory/database creation; this module only interprets explicit settings.
"""

from __future__ import annotations

import os


_TRUE_VALUES = frozenset(("1", "true", "yes", "on"))
_FALSE_VALUES = frozenset(("0", "false", "no", "off"))


def env_flag(name: str, default: bool = False) -> bool:
    """Return a predictable boolean environment setting."""

    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    value = str(raw).strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return bool(default)
