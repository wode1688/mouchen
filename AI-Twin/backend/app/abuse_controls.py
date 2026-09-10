from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
from ipaddress import ip_address, ip_network
import json
import os
import re
import threading
import time
from typing import Any, Iterable
import unicodedata

from fastapi.responses import JSONResponse


DEFAULT_REQUEST_BYTES = 256 * 1024
DEFAULT_EVENT_FACTS_BYTES = 64 * 1024
DEFAULT_AUDIO_BYTES = 48_000 * 2 * 45
DEFAULT_EVENT_RATE = 240
DEFAULT_EVENT_RATE_WINDOW_SECONDS = 60
DEFAULT_LOGIN_ACCOUNT_ATTEMPTS = 20
DEFAULT_LOGIN_SOURCE_ATTEMPTS = 120
DEFAULT_LOGIN_WINDOW_SECONDS = 15 * 60
DEFAULT_REGISTRATION_SOURCE_ATTEMPTS = 10
DEFAULT_REGISTRATION_WINDOW_SECONDS = 15 * 60
DEFAULT_ACCOUNT_STEP_UP_ATTEMPTS = 5
DEFAULT_ACCOUNT_STEP_UP_WINDOW_SECONDS = 15 * 60
DEFAULT_AUTHENTICATED_USER_REQUESTS = 600
DEFAULT_AUTHENTICATED_GLOBAL_REQUESTS = 5_000
DEFAULT_AUTHENTICATED_REQUEST_WINDOW_SECONDS = 60
DEFAULT_RATE_LIMITER_MAX_KEYS = 50_000
DEFAULT_RATE_LIMITER_MAX_HITS = 250_000
DEFAULT_RATE_LIMITER_SWEEP_SECONDS = 60


def bounded_environment_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def compact_json_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


class RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject oversized bodies while they stream, before FastAPI parses JSON."""

    def __init__(self, app) -> None:
        self.app = app

    @staticmethod
    def _limit(path: str) -> int:
        if path == "/v1/audio/transcribe":
            return bounded_environment_int(
                "MOUCHEN_MAX_AUDIO_BYTES",
                DEFAULT_AUDIO_BYTES,
                1,
                32 * 1024 * 1024,
            )
        return bounded_environment_int(
            "MOUCHEN_MAX_REQUEST_BYTES",
            DEFAULT_REQUEST_BYTES,
            1024,
            16 * 1024 * 1024,
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(str(scope.get("path") or ""))
        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        content_length = headers.get(b"content-length", b"").strip()
        if content_length.isdigit() and int(content_length) > limit:
            await self._reject(scope, receive, send)
            return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if not response_started:
                await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "request body is too large"},
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    retry_after: int = 0


class RollingWindowLimiter:
    def __init__(
        self,
        *,
        max_keys: int | None = None,
        max_hits: int | None = None,
        sweep_seconds: int | None = None,
    ) -> None:
        self._lock = threading.Lock()
        # Values are absolute monotonic expiry instants, not hit instants. This
        # lets a sweep remove untouched keys even when callers change windows.
        self._entries: dict[str, deque[float]] = {}
        self._last_sweep = 0.0
        self._total_hits = 0
        self._max_keys_override = max_keys
        self._max_hits_override = max_hits
        self._sweep_seconds_override = sweep_seconds

    def _max_keys(self) -> int:
        if self._max_keys_override is not None:
            return max(1, int(self._max_keys_override))
        return bounded_environment_int(
            "MOUCHEN_RATE_LIMITER_MAX_KEYS",
            DEFAULT_RATE_LIMITER_MAX_KEYS,
            1_000,
            1_000_000,
        )

    def _sweep_seconds(self) -> int:
        if self._sweep_seconds_override is not None:
            return max(1, int(self._sweep_seconds_override))
        return bounded_environment_int(
            "MOUCHEN_RATE_LIMITER_SWEEP_SECONDS",
            DEFAULT_RATE_LIMITER_SWEEP_SECONDS,
            1,
            3_600,
        )

    def _max_hits(self) -> int:
        if self._max_hits_override is not None:
            return max(1, int(self._max_hits_override))
        return bounded_environment_int(
            "MOUCHEN_RATE_LIMITER_MAX_HITS",
            DEFAULT_RATE_LIMITER_MAX_HITS,
            1_000,
            5_000_000,
        )

    @staticmethod
    def _retry_after_for_capacity(
        entries: dict[str, deque[float]],
        current: float,
    ) -> int:
        earliest = min(
            (expiries[0] for expiries in entries.values() if expiries),
            default=current + 1,
        )
        return max(1, int(earliest - current + 0.999))

    def _sweep_expired(self, current: float, *, force: bool = False) -> None:
        if not force and current - self._last_sweep < self._sweep_seconds():
            return
        for key, expiries in tuple(self._entries.items()):
            while expiries and expiries[0] <= current:
                expiries.popleft()
                self._total_hits -= 1
            if not expiries:
                self._entries.pop(key, None)
        self._last_sweep = current

    def reserve(
        self,
        limits: Iterable[tuple[str, int]],
        *,
        window_seconds: int,
        now: float | None = None,
    ) -> LimitDecision:
        current = time.monotonic() if now is None else now
        normalized: dict[str, int] = {}
        for key, limit in limits:
            clean_key = str(key)
            clean_limit = max(1, int(limit))
            normalized[clean_key] = min(normalized.get(clean_key, clean_limit), clean_limit)
        requested = tuple(normalized.items())
        with self._lock:
            self._sweep_expired(current)
            retry_after = 0
            for key, limit in requested:
                entries = self._entries.get(key)
                if entries is None:
                    continue
                while entries and entries[0] <= current:
                    entries.popleft()
                    self._total_hits -= 1
                if not entries:
                    self._entries.pop(key, None)
                    continue
                if len(entries) >= limit:
                    retry_after = max(
                        retry_after,
                        max(1, int(entries[0] - current + 0.999)),
                    )
            if retry_after:
                return LimitDecision(False, retry_after)
            new_keys = sum(1 for key, _ in requested if key not in self._entries)
            if len(self._entries) + new_keys > self._max_keys():
                self._sweep_expired(current, force=True)
                new_keys = sum(1 for key, _ in requested if key not in self._entries)
            if len(self._entries) + new_keys > self._max_keys():
                # Never evict a live bucket: that would let random-key churn
                # reset an account or source counter. Capacity fails closed
                # until the earliest live bucket expires.
                return LimitDecision(
                    False,
                    self._retry_after_for_capacity(self._entries, current),
                )
            requested_hits = len(requested)
            if self._total_hits + requested_hits > self._max_hits():
                self._sweep_expired(current, force=True)
            if self._total_hits + requested_hits > self._max_hits():
                return LimitDecision(
                    False,
                    self._retry_after_for_capacity(self._entries, current),
                )
            expiry = current + max(1, int(window_seconds))
            for key, _ in requested:
                self._entries.setdefault(key, deque()).append(expiry)
                self._total_hits += 1
        return LimitDecision(True)

    def clear(self, key: str) -> None:
        with self._lock:
            removed = self._entries.pop(key, None)
            if removed is not None:
                self._total_hits -= len(removed)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total_hits = 0
            self._last_sweep = 0.0


def _normalized_account_key(username: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(username or "")).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class LoginAttemptLimiter:
    def __init__(self) -> None:
        self._limiter = RollingWindowLimiter()

    def reserve(self, username: str, source_ip: str) -> LimitDecision:
        window = bounded_environment_int(
            "MOUCHEN_LOGIN_RATE_WINDOW_SECONDS",
            DEFAULT_LOGIN_WINDOW_SECONDS,
            60,
            86_400,
        )
        account_limit = bounded_environment_int(
            "MOUCHEN_LOGIN_ACCOUNT_ATTEMPTS_PER_WINDOW",
            DEFAULT_LOGIN_ACCOUNT_ATTEMPTS,
            1,
            10_000,
        )
        source_limit = bounded_environment_int(
            "MOUCHEN_LOGIN_SOURCE_ATTEMPTS_PER_WINDOW",
            DEFAULT_LOGIN_SOURCE_ATTEMPTS,
            1,
            100_000,
        )
        account_key = f"login:account:{_normalized_account_key(username)}"
        source_key = f"login:source:{source_ip}"
        return self._limiter.reserve(
            ((account_key, account_limit), (source_key, source_limit)),
            window_seconds=window,
        )

    def reset(self) -> None:
        self._limiter.reset()


class RegistrationAttemptLimiter:
    """Bound password-hash work and invitation probing by accountable source."""

    def __init__(self) -> None:
        self._limiter = RollingWindowLimiter()

    def reserve(self, source_ip: str) -> LimitDecision:
        window = bounded_environment_int(
            "MOUCHEN_REGISTRATION_RATE_WINDOW_SECONDS",
            DEFAULT_REGISTRATION_WINDOW_SECONDS,
            60,
            86_400,
        )
        source_limit = bounded_environment_int(
            "MOUCHEN_REGISTRATION_SOURCE_ATTEMPTS_PER_WINDOW",
            DEFAULT_REGISTRATION_SOURCE_ATTEMPTS,
            1,
            100_000,
        )
        return self._limiter.reserve(
            ((f"registration:source:{source_ip}", source_limit),),
            window_seconds=window,
        )

    def reset(self) -> None:
        self._limiter.reset()


class AccountStepUpLimiter:
    """One shared password step-up budget across export/change/delete.

    The composite account-and-source key prevents one stolen session from
    consuming the global Argon2 pool indefinitely without blocking a different
    tenant behind the same NAT address.
    """

    def __init__(self) -> None:
        self._limiter = RollingWindowLimiter()

    @staticmethod
    def _key(user_id: str, source_ip: str) -> str:
        identity = f"{user_id}\0{source_ip}".encode("utf-8")
        return f"account-step-up:{hashlib.sha256(identity).hexdigest()}"

    def reserve(self, user_id: str, source_ip: str) -> LimitDecision:
        attempts = bounded_environment_int(
            "MOUCHEN_ACCOUNT_STEP_UP_ATTEMPTS_PER_WINDOW",
            DEFAULT_ACCOUNT_STEP_UP_ATTEMPTS,
            1,
            100,
        )
        window = bounded_environment_int(
            "MOUCHEN_ACCOUNT_STEP_UP_WINDOW_SECONDS",
            DEFAULT_ACCOUNT_STEP_UP_WINDOW_SECONDS,
            60,
            86_400,
        )
        source_attempts = bounded_environment_int(
            "MOUCHEN_ACCOUNT_STEP_UP_SOURCE_ATTEMPTS_PER_WINDOW",
            25,
            1,
            10_000,
        )
        user_key = f"account-step-up:user:{_normalized_account_key(user_id)}"
        source_key = f"account-step-up:source:{source_ip}"
        return self._limiter.reserve(
            (
                (user_key, attempts),
                (self._key(user_id, source_ip), attempts),
                (source_key, source_attempts),
            ),
            window_seconds=window,
        )

    def succeeded(self, user_id: str, source_ip: str) -> None:
        self._limiter.clear(
            f"account-step-up:user:{_normalized_account_key(user_id)}"
        )
        self._limiter.clear(self._key(user_id, source_ip))

    def reset(self) -> None:
        self._limiter.reset()


def _trusted_proxy_networks() -> tuple[Any, ...]:
    configured = os.getenv("MOUCHEN_TRUSTED_PROXY_CIDRS", "")
    networks = []
    for item in configured.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ip_network(item, strict=False))
        except ValueError:
            # Fail closed: an invalid entry never grants proxy trust.
            continue
    return tuple(networks)


def commercial_proxy_configuration_ready() -> bool:
    """Require an exact accountable proxy peer for multi-user operation."""

    commercial = os.getenv("MOUCHEN_COMMERCIAL_MULTI_USER", "false").strip().casefold()
    if commercial not in {"1", "true", "yes", "on"}:
        return True
    if os.getenv(
        "MOUCHEN_TRUSTED_PROXY_HEADER", "x-forwarded-for"
    ).strip().casefold() not in {"x-forwarded-for", "forwarded"}:
        return False
    configured = [
        item.strip()
        for item in os.getenv("MOUCHEN_TRUSTED_PROXY_CIDRS", "").split(",")
        if item.strip()
    ]
    if not configured:
        return False
    try:
        networks = [ip_network(item, strict=True) for item in configured]
    except ValueError:
        return False
    return all(
        network.prefixlen == network.max_prefixlen
        and not network.network_address.is_unspecified
        and not network.network_address.is_multicast
        for network in networks
    )


def _clean_forwarded_ip(value: str) -> str | None:
    value = value.strip().strip('"')
    if not value or value.casefold() == "unknown" or value.startswith("_"):
        return None
    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]
    elif value.count(":") == 1 and value.rsplit(":", 1)[1].isdigit():
        value = value.rsplit(":", 1)[0]
    try:
        return str(ip_address(value))
    except ValueError:
        return None


def _forwarded_chain(headers: Any) -> list[str]:
    """Parse only the operator-selected header written by the trusted proxy.

    Accepting whichever of Forwarded/X-Forwarded-For happens to be present lets
    an end client inject the other header when a proxy rewrites only one. The
    deployment must select exactly one source of truth (XFF by default).
    """

    selected = os.getenv("MOUCHEN_TRUSTED_PROXY_HEADER", "x-forwarded-for").strip().casefold()
    result: list[str] = []
    if selected == "forwarded":
        forwarded = str(headers.get("forwarded", ""))
        for match in re.finditer(r"(?:^|[;,])\s*for=([^;,]+)", forwarded, re.I):
            candidate = _clean_forwarded_ip(match.group(1))
            if candidate:
                result.append(candidate)
        return result
    if selected != "x-forwarded-for":
        return []
    for value in str(headers.get("x-forwarded-for", "")).split(","):
        candidate = _clean_forwarded_ip(value)
        if candidate:
            result.append(candidate)
    return result


def client_source_ip(request: Any) -> str:
    """Use forwarding headers only when the immediate peer is explicitly trusted."""

    peer = str(getattr(getattr(request, "client", None), "host", "") or "unknown")
    try:
        peer_address = ip_address(peer)
    except ValueError:
        return peer
    networks = _trusted_proxy_networks()
    if not networks or not any(peer_address in network for network in networks):
        return str(peer_address)
    chain = _forwarded_chain(request.headers)
    if not chain:
        return str(peer_address)
    # Walk from the application toward the client, removing only explicitly
    # trusted proxy hops. The first remaining address is the accountable source.
    for candidate in reversed(chain):
        address = ip_address(candidate)
        if any(address in network for network in networks):
            continue
        return str(address)
    return chain[0]


class EventRateLimiter:
    def __init__(self) -> None:
        self._limiter = RollingWindowLimiter()

    def reserve(self, scope_key: str) -> LimitDecision:
        limit = bounded_environment_int(
            "MOUCHEN_EVENT_INGESTS_PER_WINDOW",
            DEFAULT_EVENT_RATE,
            1,
            100_000,
        )
        window = bounded_environment_int(
            "MOUCHEN_EVENT_RATE_WINDOW_SECONDS",
            DEFAULT_EVENT_RATE_WINDOW_SECONDS,
            1,
            86_400,
        )
        return self._limiter.reserve(
            ((f"event:{scope_key}", limit),),
            window_seconds=window,
        )

    def reset(self) -> None:
        self._limiter.reset()


class AuthenticatedRequestLimiter:
    """Bound cheap and expensive authenticated endpoints before route work."""

    def __init__(self) -> None:
        self._limiter = RollingWindowLimiter()

    def reserve(self, user_id: str) -> LimitDecision:
        window = bounded_environment_int(
            "MOUCHEN_AUTHENTICATED_RATE_WINDOW_SECONDS",
            DEFAULT_AUTHENTICATED_REQUEST_WINDOW_SECONDS,
            1,
            86_400,
        )
        user_limit = bounded_environment_int(
            "MOUCHEN_AUTHENTICATED_REQUESTS_PER_USER_WINDOW",
            DEFAULT_AUTHENTICATED_USER_REQUESTS,
            1,
            1_000_000,
        )
        global_limit = bounded_environment_int(
            "MOUCHEN_AUTHENTICATED_REQUESTS_GLOBAL_WINDOW",
            DEFAULT_AUTHENTICATED_GLOBAL_REQUESTS,
            1,
            10_000_000,
        )
        return self._limiter.reserve(
            (
                (f"authenticated:user:{user_id}", user_limit),
                ("authenticated:global", global_limit),
            ),
            window_seconds=window,
        )

    def reset(self) -> None:
        self._limiter.reset()


class AuthenticationHashAdmission:
    """Bound concurrent Argon2 work in the single production worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0

    def acquire(self) -> LimitDecision:
        limit = bounded_environment_int(
            "MOUCHEN_AUTH_HASH_CONCURRENCY",
            2,
            1,
            32,
        )
        with self._lock:
            if self._active >= limit:
                return LimitDecision(False, 1)
            self._active += 1
        return LimitDecision(True)

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("authentication work admission was not acquired")
            self._active -= 1

    def reset(self) -> None:
        with self._lock:
            self._active = 0


login_attempts = LoginAttemptLimiter()
registration_attempts = RegistrationAttemptLimiter()
account_step_up_attempts = AccountStepUpLimiter()
event_rate = EventRateLimiter()
authenticated_requests = AuthenticatedRequestLimiter()
authentication_hash_work = AuthenticationHashAdmission()
