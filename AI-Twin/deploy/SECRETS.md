# Secret files

Never put a real key in Git, an image, `.env`, a command argument, shell
history, a screenshot, or a support log. Initialize the files from a real TTY.
When already logged in as `root`, run the hidden-input script directly:

```sh
cd /opt/mouchen/AI-Twin/deploy
sh scripts/init-secrets.sh
```

From a non-root administrator account that has `sudo`, use
`sudo sh scripts/init-secrets.sh` instead. Minimal root-only VPS images often do
not install `sudo`; a root shell must not prefix these commands with it.

The script rejects piped input, disables terminal echo while each value is
entered, writes through a mode-`0600` temporary file, and atomically installs:

- `secrets/mouchen_api_token`
- `secrets/openai_api_key`
- `secrets/mouchen_registration_code`

Use a random backend token of at least 32 characters only for the bounded
private-alpha owner-claim migration. Commercial Android, Windows, iOS, and web
clients use their own account/session tokens; they must not retain this shared
value. Set `MOUCHEN_LEGACY_AUTH_ENABLED=false` immediately after owner claim and
verify the old value receives `401`. The model-provider key is read from
`/run/secrets/openai_api_key`. The registration code is a one-time bootstrap
secret for claiming the pre-commercial owner's unchanged tenant id. After one
successful claim, the same code is rejected. Run the initializer without
`--rotate` to preserve existing files and add only a missing bootstrap code.
Routine customer invitations must use `scripts/invites.sh`; they are expiring,
revocable, database-backed, and do not require a restart. Rotate the bootstrap
code only for an intentional owner-recovery procedure without changing the API
or model keys:

```sh
sh scripts/init-secrets.sh --rotate-registration-code
sh scripts/deploy.sh
```

Local Docker Compose implements file-backed secrets as bind mounts. It cannot
apply Compose `uid`, `gid`, or `mode` remapping to a host file. For that reason,
the initializer makes `deploy/secrets` `root:root` mode `0700` and each secret
file numeric owner `10001:10001` mode `0400`. The Docker daemon can mount the
root-only host file, while the container's non-root UID `10001` can read only
the mounted file. Both deployment and live verification fail closed if these
owners or modes differ.

To intentionally replace all three files, use the same hidden-input path:

```sh
sh scripts/init-secrets.sh --rotate
sh scripts/deploy.sh
```

From a configured non-root administrator account, prefix both commands with
`sudo`.

The application also supports the `NAME_FILE` convention for
`ANTHROPIC_API_KEY`, `OLLAMA_AUTH_TOKEN`, and `MOUCHEN_SAFETY_SALT` when a later
deployment explicitly mounts those secrets.

A credential already pasted into chat or terminal history must be considered
exposed. Rotate it at the provider; do not reuse the exposed value in these
files.
