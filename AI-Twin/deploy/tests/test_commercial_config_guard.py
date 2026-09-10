from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "validate-commercial-config.py"
SPEC = importlib.util.spec_from_file_location("commercial_config_guard", SCRIPT)
assert SPEC and SPEC.loader
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


def _document(**environment):
    defaults = {
        "MOUCHEN_COMMERCIAL_MULTI_USER": "true",
        "MOUCHEN_REGISTRATION_MODE": "closed",
        "MOUCHEN_TRUSTED_PROXY_HEADER": "x-forwarded-for",
        "MOUCHEN_TRUSTED_PROXY_CIDRS": "172.21.0.1/32",
    }
    defaults.update(environment)
    return {"services": {"backend": {"environment": defaults}}}


def test_commercial_config_requires_exact_trusted_proxy() -> None:
    GUARD.validate(_document())
    for value in ("", "172.21.0.0/24", "0.0.0.0/0", "not-an-address"):
        with pytest.raises(GUARD.ConfigurationError):
            GUARD.validate(_document(MOUCHEN_TRUSTED_PROXY_CIDRS=value))


def test_commercial_config_is_invite_only_and_selects_one_header() -> None:
    with pytest.raises(GUARD.ConfigurationError):
        GUARD.validate(_document(MOUCHEN_REGISTRATION_MODE="open"))
    with pytest.raises(GUARD.ConfigurationError):
        GUARD.validate(_document(MOUCHEN_TRUSTED_PROXY_HEADER="any-header"))


def test_noncommercial_private_install_does_not_require_proxy() -> None:
    GUARD.validate(
        _document(
            MOUCHEN_COMMERCIAL_MULTI_USER="false",
            MOUCHEN_TRUSTED_PROXY_CIDRS="",
            MOUCHEN_REGISTRATION_MODE="open",
        )
    )


def test_runtime_environment_list_is_parsed_without_trusting_other_headers() -> None:
    environment = GUARD._backend_environment(
        {
            "services": {
                "backend": {
                    "environment": [
                        "MOUCHEN_COMMERCIAL_MULTI_USER=true",
                        "MOUCHEN_REGISTRATION_MODE=closed",
                        "MOUCHEN_TRUSTED_PROXY_HEADER=x-forwarded-for",
                        "MOUCHEN_TRUSTED_PROXY_CIDRS=172.21.0.1/32",
                    ]
                }
            }
        }
    )
    assert environment["MOUCHEN_TRUSTED_PROXY_CIDRS"] == "172.21.0.1/32"
