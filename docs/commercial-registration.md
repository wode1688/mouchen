# Commercial account registration contract

Production registration defaults to closed. My AI Twin exposes no public or
authenticated HTTP administration route for invitation creation, listing, or
revocation. Those operations run locally on the server through
`deploy/scripts/invites.sh`.

Each generated invitation has an opaque public id, an administrator label, a
creation time, an expiry, and optional revocation/consumption timestamps. The
plaintext registration code is generated from 32 random bytes and emitted once
to the administrator. SQLite receives only its SHA-256 digest. The server
atomically consumes the invite in the same transaction that creates (or claims)
the account, so concurrent submissions cannot create two accounts.

Clients use the existing contract:

```json
POST /v1/auth/register
{
  "username": "chosen-name",
  "password": "a user-chosen password",
  "device_id": "stable-per-install-id",
  "device_name": "optional display name",
  "registration_code": "one plaintext invitation code"
}
```

Successful registration returns the normal bearer session. Unknown, expired,
revoked, reused, and racing-loser codes all fail without exposing which check
failed. The legacy file-backed bootstrap code remains supported only so the
original owner can claim an existing tenant without changing its `user_id` or
business data.
