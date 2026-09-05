from __future__ import annotations

import os
from pathlib import Path


_MAX_SECRET_BYTES = 64 * 1024


class SecretConfigurationError(RuntimeError):
    """Raised for an unavailable secret without exposing its value or path."""


def secret_value(name: str) -> str | None:
    """Read a secret from NAME or, when unset, from Docker-style NAME_FILE.

    Direct environment values keep backward compatibility and take precedence.
    File-backed values have only trailing line endings removed so punctuation
    and intentional spaces remain intact.
    """

    direct = os.getenv(name)
    if direct is not None and direct.strip():
        return direct.strip()

    file_name = os.getenv(f"{name}_FILE")
    if not file_name:
        return None

    try:
        secret_path = Path(file_name)
        if not secret_path.is_file() or secret_path.stat().st_size > _MAX_SECRET_BYTES:
            raise SecretConfigurationError(f"{name}_FILE is unavailable")
        value = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
    except SecretConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SecretConfigurationError(f"{name}_FILE is unavailable") from exc

    if not value.strip() or "\x00" in value:
        raise SecretConfigurationError(f"{name}_FILE is invalid")
    return value
