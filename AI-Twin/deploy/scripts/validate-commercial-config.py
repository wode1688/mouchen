#!/usr/bin/env python3
"""Fail closed on unsafe commercial Compose authentication settings."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from typing import Any, Mapping


class ConfigurationError(ValueError):
    pass


def _flag(value: object, *, default: bool = False) -> bool:
    normalized = str(value if value is not None else "").strip().casefold()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError("commercial multi-user switch is invalid")


def _backend_environment(document: Mapping[str, Any]) -> dict[str, str]:
    services = document.get("services")
    if not isinstance(services, Mapping) or not isinstance(services.get("backend"), Mapping):
        raise ConfigurationError("rendered Compose config has no backend service")
    raw = services["backend"].get("environment", {})
    if isinstance(raw, Mapping):
        return {str(key): "" if value is None else str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for entry in raw:
            key, separator, value = str(entry).partition("=")
            result[key] = value if separator else ""
        return result
    raise ConfigurationError("rendered backend environment is invalid")


def validate(document: Mapping[str, Any]) -> None:
    environment = _backend_environment(document)
    if not _flag(environment.get("MOUCHEN_COMMERCIAL_MULTI_USER"), default=True):
        return
    if environment.get("MOUCHEN_REGISTRATION_MODE", "closed").strip().casefold() != "closed":
        raise ConfigurationError("commercial deployment requires invite-only registration")
    selected_header = environment.get(
        "MOUCHEN_TRUSTED_PROXY_HEADER", "x-forwarded-for"
    ).strip().casefold()
    if selected_header not in {"x-forwarded-for", "forwarded"}:
        raise ConfigurationError("trusted proxy header must be explicitly selected")
    configured = environment.get("MOUCHEN_TRUSTED_PROXY_CIDRS", "").strip()
    if not configured:
        raise ConfigurationError(
            "commercial deployment requires the exact reverse-proxy address"
        )
    for raw_network in configured.split(","):
        try:
            network = ipaddress.ip_network(raw_network.strip(), strict=True)
        except ValueError as exc:
            raise ConfigurationError("trusted proxy address is invalid") from exc
        if network.prefixlen != network.max_prefixlen:
            raise ConfigurationError("trusted proxy entries must be exact host prefixes")
        if network.network_address.is_unspecified or network.network_address.is_multicast:
            raise ConfigurationError("trusted proxy address is unsafe")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--environment-list",
        action="store_true",
        help="Read the runtime container Config.Env JSON list instead of Compose JSON.",
    )
    parser.add_argument(
        "--gateway",
        help="Runtime bridge gateway which must be an exact trusted peer.",
    )
    args = parser.parse_args()
    try:
        loaded = json.load(sys.stdin)
        if args.environment_list:
            if not isinstance(loaded, list):
                raise ConfigurationError("runtime environment must be a JSON list")
            document = {"services": {"backend": {"environment": loaded}}}
        else:
            document = loaded
        if not isinstance(document, Mapping):
            raise ConfigurationError("rendered configuration must be a JSON object")
        validate(document)
        if args.gateway:
            environment = _backend_environment(document)
            if _flag(environment.get("MOUCHEN_COMMERCIAL_MULTI_USER"), default=True):
                gateway = ipaddress.ip_address(args.gateway.strip())
                trusted = {
                    ipaddress.ip_network(item.strip(), strict=True).network_address
                    for item in environment.get("MOUCHEN_TRUSTED_PROXY_CIDRS", "").split(",")
                    if item.strip()
                }
                if gateway not in trusted:
                    raise ConfigurationError(
                        "runtime bridge gateway is not the configured trusted proxy"
                    )
    except (ConfigurationError, json.JSONDecodeError, ValueError) as exc:
        print(f"unsafe commercial configuration: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
