"""A server-side switch that cannot be overridden by client approval headers."""
from __future__ import annotations

import os


def rules_only() -> bool:
    return os.getenv("MOUCHEN_MODEL_PROVIDER", "").strip().casefold() == "rules"
