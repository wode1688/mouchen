"""Local-only registration invite administration.

This module intentionally has no HTTP surface. Run it inside the backend
container so SQLite remains the single source of truth. Newly generated invite
codes are printed once; only SHA-256 digests are persisted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from datetime import timedelta
from uuid import uuid4

from .auth import utc_now
from .storage import Repository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mouchen-invites")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create one or more one-time invites")
    create.add_argument("--label", required=True, help="non-secret administrative label")
    create.add_argument("--ttl-hours", type=int, default=168)
    create.add_argument("--count", type=int, default=1)

    listing = commands.add_parser("list", help="list invite metadata; never prints codes")
    listing.add_argument("--active-only", action="store_true")

    revoke = commands.add_parser("revoke", help="revoke active invites by public invite id")
    revoke.add_argument("invite_ids", nargs="+")
    return parser


def _emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = Repository()
    try:
        if args.command == "create":
            if not 1 <= args.ttl_hours <= 8760:
                raise ValueError("ttl-hours must be between 1 and 8760")
            if not 1 <= args.count <= 100:
                raise ValueError("count must be between 1 and 100")
            label = args.label.strip()
            if not label:
                raise ValueError("label is required")
            expires_at = utc_now() + timedelta(hours=args.ttl_hours)
            issued = []
            for index in range(args.count):
                code = secrets.token_urlsafe(32)
                invite_id = str(uuid4())
                item_label = label if args.count == 1 else f"{label} #{index + 1}"
                repo.create_registration_invite(
                    invite_id=invite_id,
                    code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
                    label=item_label,
                    expires_at=expires_at,
                )
                issued.append(
                    {
                        "invite_id": invite_id,
                        "label": item_label,
                        "expires_at": expires_at.isoformat(),
                        "registration_code": code,
                    }
                )
            _emit({"created": issued, "warning": "codes are shown once; store them safely"})
            return 0

        if args.command == "list":
            rows = repo.list_registration_invites()
            if args.active_only:
                rows = [row for row in rows if row["state"] == "active"]
            _emit({"invites": rows})
            return 0

        revoked = []
        unchanged = []
        for invite_id in args.invite_ids:
            (revoked if repo.revoke_registration_invite(invite_id) else unchanged).append(
                invite_id
            )
        _emit({"revoked": revoked, "unchanged": unchanged})
        return 0 if not unchanged else 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        repo.close()


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
