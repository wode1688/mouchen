from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


USERNAME_MIN_LENGTH: Final = 3
USERNAME_MAX_LENGTH: Final = 64
PASSWORD_MIN_LENGTH: Final = 10
PASSWORD_MAX_LENGTH: Final = 1024
DEVICE_ID_MAX_LENGTH: Final = 160
TOKEN_PREFIX: Final = "mch_at_"
_TOKEN_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Parameters are explicit so a dependency upgrade cannot silently weaken hashes.
# PasswordHasher.check_needs_rehash lets us strengthen them later at login.
PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

# Unknown usernames execute a real Argon2id verification too. This constant is
# deliberately a valid hash and contains no production secret.
_DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash(
    "mouchen-dummy-password-used-only-for-login-timing"
)


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    user_id: str
    username: str | None
    device_id: str
    scopes: tuple[str, ...]
    expires_at: datetime | None
    token_id: str | None
    auth_kind: str = "session"


class InvalidUsername(ValueError):
    pass


class InvalidPassword(ValueError):
    pass


def normalize_username(value: str) -> tuple[str, str]:
    display = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = display.casefold()
    if not (USERNAME_MIN_LENGTH <= len(normalized) <= USERNAME_MAX_LENGTH):
        raise InvalidUsername("username is invalid")
    if any(character.isspace() or unicodedata.category(character).startswith("C") for character in normalized):
        raise InvalidUsername("username is invalid")
    return display, normalized


def validate_password(value: str) -> str:
    password = str(value or "")
    if not (PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH):
        raise InvalidPassword("password is invalid")
    return password


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(validate_password(password))


def verify_password(password_hash: str | None, supplied: str) -> tuple[bool, str | None]:
    candidate_hash = password_hash or _DUMMY_PASSWORD_HASH
    try:
        valid = PASSWORD_HASHER.verify(candidate_hash, supplied)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, None
    if not password_hash:
        return False, None
    replacement = PASSWORD_HASHER.hash(supplied) if PASSWORD_HASHER.check_needs_rehash(password_hash) else None
    return bool(valid), replacement


def new_access_token(token_id: str) -> str:
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    return f"{TOKEN_PREFIX}{token_id}.{secret}"


def token_locator(token: str) -> str | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    locator, separator, secret = token[len(TOKEN_PREFIX) :].partition(".")
    if not separator or not _TOKEN_ID_RE.fullmatch(locator) or len(secret) < 40:
        return None
    return locator


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_token_match(expected_hash: str, supplied_token: str) -> bool:
    return hmac.compare_digest(expected_hash, token_hash(supplied_token))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
