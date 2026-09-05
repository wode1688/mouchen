from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def explicit_legacy_private_alpha_compatibility(monkeypatch):
    """Existing API tests exercise the opt-in private-alpha compatibility mode."""

    monkeypatch.setenv("MOUCHEN_LEGACY_AUTH_ENABLED", "true")
    # Tests which explicitly select open registration still need the separate
    # development-only opt-in. Production examples leave this disabled.
    monkeypatch.setenv("MOUCHEN_ALLOW_OPEN_REGISTRATION", "true")
