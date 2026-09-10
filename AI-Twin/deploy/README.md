# My AI Twin VPS Docker baseline

This deployment runs one backend worker with SQLite on a named volume. The
container runs as UID/GID `10001`, has a read-only root filesystem, drops every
Linux capability, enables `no-new-privileges`, and limits memory, CPU, PIDs,
temporary storage, and retained logs.

The published port is deliberately `127.0.0.1` only. Do not change it to
`0.0.0.0`. Reach it through a private Tailscale path or an HTTPS reverse proxy
that is itself restricted to the tailnet. Commercial clients authenticate with
their per-device account session. The old shared Bearer token is disabled by
default and is never a substitute for the private transport.

Commercial mode creates or verifies its project bridge, then derives
`MOUCHEN_TRUSTED_PROXY_CIDRS` as the host proxy's exact directly connected
`/32` or `/128` address. Leave that `.env` value blank. Keep
`MOUCHEN_TRUSTED_PROXY_HEADER=x-forwarded-for` only when that proxy overwrites
the header. Never trust a client-supplied forwarding header or a broad subnet.

## Prepare the VPS

Install Docker Engine with the Compose v2 plugin from Docker's official
repository. Put this repository at `/opt/mouchen`, then:

```sh
cd /opt/mouchen/AI-Twin/deploy
cp env.example .env
chmod 600 .env
```

Edit only non-secret settings in `.env`. During the upgrade,
`MOUCHEN_SINGLE_USER_ID` keeps the old private-alpha token bound to the existing
owner, while `MOUCHEN_LEGACY_USERNAMES_JSON` assigns that unchanged tenant id a
login name. A successful one-time registration claim sets the password on that
same row; it never rewrites business data to a new user id. New users receive
independent server-derived user ids and per-device access tokens. Clients may
omit `X-User-Id`; if they send it during migration, it must equal the identity
derived from the token.

Create the three secret files by following [SECRETS.md](SECRETS.md). On a new
empty installation, deploy with:

```sh
sh scripts/init-secrets.sh
sh scripts/deploy.sh
```

The release gate pins the Compose project to `mouchen`; an inherited
`COMPOSE_PROJECT_NAME` cannot redirect backup or rollback to another volume.
It also retains an immutable, SHA-256-addressed rendering of the exact Compose
contract that created each verified release. A rollback restores the prior
database, image, and that prior contract before running verification.

When adopting this gate on an already running older installation, preserve the
deployed files before pulling or editing them:

```sh
cd /opt/mouchen/AI-Twin/deploy
cp compose.yaml rollback-bootstrap.compose.yaml
cp .env rollback-bootstrap.env
```

After the new scripts are present, but before the first guarded deployment or
any `.env` change, bind that old source to the running containers:

```sh
MOUCHEN_CONTRACT_SOURCE_FILE=/opt/mouchen/AI-Twin/deploy/rollback-bootstrap.compose.yaml \
MOUCHEN_ENV_FILE=/opt/mouchen/AI-Twin/deploy/rollback-bootstrap.env \
  sh scripts/capture-compose-contract.sh
```

Capture fails unless those files reproduce both running service hashes. Recover
the actual deployed Compose/env pair instead of bypassing this gate. Later
successful deployments update the active contract automatically. Keep the
bootstrap copies root-only until an accepted release passes an off-host restore
drill.

After a successful capture, run `sh scripts/deploy.sh` from the new checkout.

`deploy.sh` now fails closed around every non-empty data volume. Before it
builds the candidate, it pins the running image id under a release-unique,
content-addressed local protection tag. It builds without replacing the
running containers, then verifies that the protection tag still resolves to
the same id. It creates and independently verifies a transactionally
consistent host backup, then runs the candidate's
real Repository migrations against a disposable copy with no network, secrets,
or live-volume access. It replaces the service only after that preflight passes.
If live health or authenticated verification then fails, it automatically
restores the pre-deploy database and immutable previous image. A truly empty
new installation is the only case with no database to back up. A root-only host
lock rejects concurrent deploy/rollback processes instead of letting two
operators race the same SQLite volume.

The isolated migration preflight provides up to a 1 GiB disposable `/work` tmpfs
inside a 2 GiB container limit. This headroom is intentional: SQLite table and
index rebuilds can temporarily require several times the final database size.
The tmpfs is removed with the container and never contains the live volume.

For the first authentication-enabled deployment, keep
`MOUCHEN_REGISTRATION_MODE=closed` and explicitly set
`MOUCHEN_LEGACY_AUTH_ENABLED=true` only for the bounded owner-claim window.
After deployment, use the mapped legacy
username, a new password of at least ten characters, and the hidden one-time
bootstrap code in one client. Verify
`GET /v1/session` returns the unchanged legacy user id and that the existing
goals/advice are present. Then set `MOUCHEN_LEGACY_AUTH_ENABLED=false`, deploy
again, and require `scripts/verify.sh` to prove the old static token now receives
`401` before inviting anyone else. New empty installations use the same bounded
bootstrap window to create their first credentialed owner. The bootstrap code
exists only for that owner claim. Issue later customer invitations with the
database-backed administrator tool described below. Never switch to open
registration merely to work around a failed legacy mapping.

After a false-mode release becomes the verified active contract, ordinary and
automatic rollback refuse any target that would re-enable the retired shared
token. Recover application/database compatibility forward from false mode; do
not reopen the legacy credential as a rollback shortcut.

These examples assume the current shell is already `root`, as on many minimal
VPS images. If logged in as a non-root administrator and `sudo` is installed,
prefix the script commands with `sudo`. Do not use `sudo` from a root shell on
a minimal image where it is absent.

Deployment force-recreates the one backend container so an atomically rotated
secret file cannot leave the old bind-mounted inode active.

The script validates Compose, builds the image, enforces the backup/migration
release gate, waits for `/ready` to verify
SQLite and authentication readiness, and checks the live container's secret
ownership, user, read-only roots, dropped capabilities, security options, health,
resource bounds, an unpublished STT sidecar, and loopback-only API binding. It
also proves a bad token receives `401`. During the explicit legacy window it
proves the configured owner token receives `200`; in commercial mode it proves
that same token receives `401`. The value is fed to curl through stdin and is
never put in argv or output.

## Verify and operate

```sh
python3 scripts/check-release-guard.py
sh scripts/verify.sh
docker compose --env-file .env -p mouchen -f compose.yaml logs --tail 100 backend
docker compose --env-file .env -p mouchen -f compose.yaml logs --tail 100 stt
docker compose --env-file .env -p mouchen -f compose.yaml up -d --build stt backend
docker compose --env-file .env -p mouchen -f compose.yaml down
```

The first command is credential-free and does not contact Docker or the live
volume. It checks shell syntax, verifies that the mandatory backup/preflight/
rollback calls cannot be omitted, rejects broad volume-deletion commands, and
performs a temporary reversible SQLite restore self-check.

Do not remove or repoint `mouchen-release-guard:previous-*` tags. Automatic and
one-command rollback bind the exact immutable previous image id to one of these
release-unique tags before the candidate build, use that protected image for
the pre-deploy backup, and retain it after success. Rollback proves the tag
still resolves to the recorded id and successfully starts a locked-down probe
from it before changing database bytes. A deployment containing existing data
refuses to start unless both its verified database backup and protected
previous local image are available. Keep the protection tag until the release
has been accepted and an off-host restore drill has passed.

### Issue and revoke account invitations

Self-registration remains closed. There is deliberately no invitation-admin
HTTP endpoint. A server administrator creates expiring, single-use codes from
the VPS; the database stores only their SHA-256 digests:

```sh
sh scripts/invites.sh create --label "friends-2026-08" --ttl-hours 72 --count 3
sh scripts/invites.sh list --active-only
sh scripts/invites.sh revoke INVITE_ID [INVITE_ID ...]
```

The create command prints each plaintext code exactly once. Send each code to
one intended user through a separate secure channel; do not copy the JSON into
logs or tickets. `list` shows only non-secret ids, labels, timestamps, state,
and (after use) the owning user id. An invite becomes unusable when consumed,
expired, or revoked. Revocation is atomic and does not affect an account that
already completed registration.

The client submits the code in the existing `registration_code` field of
`POST /v1/auth/register`. The server returns the same generic
`registration unavailable` response for unknown, expired, revoked, and reused
codes, so the public API does not reveal invite state.

For a non-root administrator, prefix the script and Docker commands with
`sudo` when required by that host's Docker installation.

`/health` proves the process is alive. `/ready` additionally executes a SQLite
query and requires either at least one credentialed account or the explicitly
enabled legacy migration path; it returns `503` when authentication migration
is incomplete. Neither endpoint exposes user data or secret values.

After the first deployment, manually make exactly one live provider call:

```sh
sh scripts/smoke-model.sh
```

This smoke test is intentionally not part of every deployment because it
consumes a model request. It sends only a fixed synthetic prompt, requires HTTP
`200`, rejects template or degraded responses, and never prints the token or
model response body. A non-root administrator can run it with
`sudo sh scripts/smoke-model.sh`.

## Private speech-to-text

The image installs the pinned `sherpa-onnx` runtime and the Mandarin
`sherpa-onnx-paraformer-zh-2023-09-14` assets. A non-root, read-only `stt`
sidecar keeps the recognizer resident in memory on an internal Docker network;
its port is never published. The source archive is pinned to `234051698` bytes
and SHA-256
`9c49fd9c6fb63de8e18c1054cf3d100f804741b7e608e187923cd8ff09fa9f03`.
The extracted `model.int8.onnx` is pinned to `243371218` bytes and SHA-256
`f36a0433bcf096bd6d6f11b80a3ac8bed110bdca632fe0d731df8d1a84475945`;
`tokens.txt` is pinned to `75756` bytes and SHA-256
`59aba8873a2ed1e122c25fee421e25f283b63290efbde85c1f01a853d83cb6e6`.
The image build and deployment verification fail closed on any mismatch.
When `MOUCHEN_LOCAL_STT_ENABLED=true`, authenticated PCM16 segments sent to
`/v1/audio/transcribe` are processed serially. The backend wraps PCM in an
in-memory WAV request for the isolated Paraformer sidecar, and neither container
creates a plaintext audio file.

Before Paraformer runs, an energy gate and aggressive WebRTC voice-activity
detector require several consecutive speech frames. Digital silence and
isolated noise therefore return an empty transcript and never become an Android
`speech.transcript` event. The private Android client sends 16 kHz, 16-bit mono
PCM; the endpoint accepts the WebRTC VAD rates 8, 16, 32, and 48 kHz.

The portable profile limits the API container to 768 MiB and the resident STT
sidecar to 1536 MiB and uses four Paraformer inference threads. Other models or
thread counts require explicit host measurements and adjusted sidecar limits.
One segment must finish before the next is processed; a
concurrent request receives `429` and Android retries it. Keep the phone's STT
processing location set to `private_vps`, because the raw segment leaves the
phone even though it stays on this private Tailscale service. Do not label this
path on-device or local-only.

`scripts/verify.sh` proves the exact model and tokens paths, sizes, and SHA-256
digests; it also proves that the sidecar has no published port, is attached only
to an internal network shared with the backend, returns a healthy loaded
Paraformer runtime, and preserves the silence fast path. It
does not prove Mandarin accuracy. Final acceptance requires recordings from the
actual user and microphone; do not commit that corpus. Create a JSONL manifest
outside the repository with at least five samples in each category:

```json
{"id":"clean-01","path":"audio/clean-01.wav","kind":"speech","category":"clean","reference":"你好，今天下午三点开会"}
{"id":"noisy-01","path":"audio/noisy-01.wav","kind":"speech","category":"noisy","reference":"帮我提醒明天交报告"}
{"id":"entity-01","path":"audio/entity-01.wav","kind":"speech","category":"entity","reference":"给张伟转发二零二六年八月的方案"}
{"id":"silence-01","path":"audio/silence-01.wav","kind":"silence","category":"silence"}
```

WAV files must be mono signed PCM16 at 8, 16, 32, or 48 kHz and no longer than
45 seconds. The evaluator refuses missing files, empty corpora, missing
references, or inadequate category counts. It never includes reference text,
transcripts, or the API token in its report.

Run the prior production STT image once and retain its report if a genuine
relative comparison is available. After deploying the pinned Paraformer image
into newly recreated containers, run:

```sh
container_id=$(docker compose --env-file .env -p mouchen -f compose.yaml ps -q stt)
python3 scripts/evaluate_stt.py \
  --manifest /root/mouchen-stt-corpus/manifest.jsonl \
  --token-file secrets/mouchen_api_token \
  --user-id "$(sed -n 's/^MOUCHEN_SINGLE_USER_ID=//p' .env)" \
  --docker-container "$container_id" \
  --require-memory-check \
  --baseline-report /root/stt-prior-production.json \
  --report /root/stt-paraformer-2023-09-14.json
```

Omit `--baseline-report` only when no genuine old-model run exists. Default
gates are clean Mandarin CER at most 10%, noisy CER at most 18%, exact
name/number phrases at least 85%, zero silence hallucinations, p95 latency at
most 8 seconds, p95 real-time factor at most 1.0, and (when a baseline is
provided) at least 20% relative CER improvement. With
`--require-memory-check`, the container must remain at 1536 MiB, stay below
1.25 GiB peak memory, and show no OOM or restart. A failed gate is a failed
release; the script does not manufacture or prefill an audio result.

After the evaluator passes, test phone microphone capture through
`speech.transcript` and a resulting advice notification. Configuration checks
alone are not an end-to-end transcription claim.

## Back up SQLite

Every non-empty `deploy.sh` run invokes this backup automatically. To create an
additional operator backup, run the same guarded path directly:

```sh
sh scripts/backup.sh
```

Use `sudo sh scripts/backup.sh` only from a non-root account with configured
`sudo`.

Backups are written under ignored `deploy/backups/`, mode `0600`. A locked-down
helper mounts the named data volume read-only and uses SQLite's online backup
API, so WAL writes remain transactionally consistent. The script validates the
volume-side snapshot in a new empty staging directory (the helper cannot see or
alter older backups), independently reopens and validates the host copy, and
publishes it only alongside a relative-filename SHA-256 sidecar. Traps remove
only new partial/failed artifacts.

Each successful preflight also writes root-only immutable release metadata plus
`deploy/backups/last-release.state`; it references a content-addressed previous
Compose contract plus the release-unique previous-image protection tag and
refuses rollback if the state, contract digest, or image binding is invalid. If
a release passed but must be reverted,
the supported one-command path is:

```sh
sh scripts/rollback.sh latest
```

Rollback revalidates the checksum and full SQLite integrity, stops only the
backend writer, creates a second verified rescue backup of the current/failed
database, restores the pre-release bytes, recreates the prior immutable image
with the saved prior Compose contract, reruns the complete live verifier against
that contract, and atomically makes it the active contract again. The replaced
`.db`, `-wal`, and `-shm` files are moved—not
deleted—under the named volume's
`/data/rollback-displaced/<release-id>/` directory. If restore begins but live
verification fails, the backend remains stopped for inspection rather than
silently serving an uncertain database.

Copy encrypted backups and their SHA-256 sidecars off the VPS. Test restoration
on a separate deployment before relying on them. Keep the pre-release backup,
release state, rescue backup, displaced files, and previous image until the
commercial release has been accepted and an off-host restore drill has passed.

## Network boundary

This Compose file does not terminate public TLS and never opens the public VPS
interface. For the first remote test, use an SSH local-forward or Tailscale.
For unattended Android use, terminate HTTPS on a tailnet-only proxy and keep
Docker bound to loopback. Do not publish port 8788 directly or rely on the
Bearer token as a substitute for TLS and network isolation.
