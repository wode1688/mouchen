#!/bin/sh
set -eu
set +x

# Capture the exact Compose contract of the currently running release.  This is
# intentionally separate from deploy.sh so the first guarded upgrade can be
# bootstrapped from the checkout that actually created the running containers.
umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE=${MOUCHEN_CONTRACT_SOURCE_FILE:-"$DEPLOY_DIR/compose.yaml"}
ENV_FILE=${MOUCHEN_ENV_FILE:-"$DEPLOY_DIR/.env"}
BACKUP_DIR=${MOUCHEN_BACKUP_DIR:-"$DEPLOY_DIR/backups"}
COMPOSE_PROJECT=mouchen

[ "$#" -eq 0 ] || {
  echo "usage: capture-compose-contract.sh" >&2
  exit 2
}
[ "$(id -u)" -eq 0 ] || {
  echo "run contract capture as root" >&2
  exit 1
}
[ -s "$ENV_FILE" ] && [ -f "$COMPOSE_FILE" ] && [ ! -L "$COMPOSE_FILE" ] || {
  echo "a real Compose source and deploy/.env are required" >&2
  exit 1
}
for command_name in docker flock grep install sha256sum stat; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "$command_name is required" >&2
    exit 1
  }
done
docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is required" >&2
  exit 1
}
[ ! -L "$BACKUP_DIR" ] || {
  echo "backup directory must not be a symbolic link" >&2
  exit 1
}
install -d -o 0 -g 0 -m 0700 "$BACKUP_DIR"
BACKUP_DIR=$(CDPATH= cd -- "$BACKUP_DIR" && pwd)
[ "$(stat -c '%u:%g:%a' "$BACKUP_DIR")" = "0:0:700" ] || {
  echo "backup directory must be root:root mode 0700" >&2
  exit 1
}
if [ "${MOUCHEN_RELEASE_LOCK_HELD:-}" != "1" ]; then
  exec 9>"$BACKUP_DIR/.release.lock"
  chmod 0600 "$BACKUP_DIR/.release.lock"
  flock -n 9 || {
    echo "another My AI Twin deployment or rollback is already running" >&2
    exit 1
  }
fi

source_compose() {
  docker compose --env-file "$ENV_FILE" -p "$COMPOSE_PROJECT" \
    -f "$COMPOSE_FILE" "$@"
}

source_compose config --quiet
backend_id=$(source_compose ps -q backend)
stt_id=$(source_compose ps -q stt)
[ -n "$backend_id" ] && [ -n "$stt_id" ] || {
  echo "both backend and STT must be running before their contract can be captured" >&2
  exit 1
}
[ "$(printf '%s\n' "$backend_id" | sed '/^$/d' | wc -l | tr -d ' ')" -eq 1 ] && \
  [ "$(printf '%s\n' "$stt_id" | sed '/^$/d' | wc -l | tr -d ' ')" -eq 1 ] || {
  echo "contract capture found an ambiguous service container set" >&2
  exit 1
}

rendered="$BACKUP_DIR/.compose-contract-$$.yaml"
active_partial="$BACKUP_DIR/.active-compose-$$.state"
cleanup() {
  rm -f -- "$rendered" "$active_partial"
}
trap cleanup 0
trap 'exit 130' 1 2 15
source_compose config > "$rendered"
chmod 0600 "$rendered"

rendered_compose() {
  docker compose -p "$COMPOSE_PROJECT" -f "$rendered" "$@"
}
rendered_compose config --quiet

config_hash() {
  service=$1
  value=$(rendered_compose config --hash "$service" | awk -v name="$service" \
    '$1 == name { print $NF } END { if (NF == 1) print $1 }' | sed -n '1p')
  printf '%s\n' "$value" | grep -Eq '^[0-9a-f]{64}$' || {
    echo "Compose did not return a stable $service config hash" >&2
    exit 1
  }
  printf '%s\n' "$value"
}
backend_hash=$(config_hash backend)
stt_hash=$(config_hash stt)
running_backend_hash=$(docker inspect --format \
  '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$backend_id")
running_stt_hash=$(docker inspect --format \
  '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$stt_id")
[ "$backend_hash" = "$running_backend_hash" ] && [ "$stt_hash" = "$running_stt_hash" ] || {
  echo "Compose source does not match the running release; use the checkout and env that created it" >&2
  exit 1
}

digest=$(sha256sum "$rendered" | awk '{print $1}')
printf '%s\n' "$digest" | grep -Eq '^[0-9a-f]{64}$' || {
  echo "could not digest rendered Compose contract" >&2
  exit 1
}
contract_name="compose-contract-$digest.yaml"
contract="$BACKUP_DIR/$contract_name"
if [ -e "$contract" ]; then
  [ -f "$contract" ] && [ ! -L "$contract" ] && \
    [ "$(stat -c '%u:%g:%a' "$contract")" = "0:0:600" ] && \
    [ "$(sha256sum "$contract" | awk '{print $1}')" = "$digest" ] || {
    echo "existing immutable Compose contract is unsafe or corrupt" >&2
    exit 1
  }
  rm -f -- "$rendered"
else
  mv -- "$rendered" "$contract"
fi

{
  printf 'format=1\n'
  printf 'contract_name=%s\n' "$contract_name"
  printf 'sha256=%s\n' "$digest"
  printf 'backend_config_hash=%s\n' "$backend_hash"
  printf 'stt_config_hash=%s\n' "$stt_hash"
} > "$active_partial"
chmod 0600 "$active_partial"
mv -- "$active_partial" "$BACKUP_DIR/active-compose.state"
trap - 0
echo "active Compose rollback contract captured and verified: $contract_name"
