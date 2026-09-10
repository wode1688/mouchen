#!/bin/sh
set -eu
set +x

umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${MOUCHEN_ENV_FILE:-"$DEPLOY_DIR/.env"}
BACKUP_DIR=${MOUCHEN_BACKUP_DIR:-"$DEPLOY_DIR/backups"}
COMPOSE_PROJECT=mouchen
GUARD="$SCRIPT_DIR/sqlite_release_guard.py"
VERIFY="$SCRIPT_DIR/verify.sh"
IMAGE_GUARD="$SCRIPT_DIR/runtime_image_guard.py"

[ "$#" -le 1 ] || {
  echo "usage: rollback.sh [latest|RELEASE_STATE_FILE]" >&2
  exit 2
}
[ "$(id -u)" -eq 0 ] || {
  echo "run rollback as root" >&2
  exit 1
}
[ -s "$ENV_FILE" ] || {
  echo "deploy/.env is required" >&2
  exit 1
}
for command_name in docker find flock grep install python3 sha256sum stat; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "$command_name is required" >&2
    exit 1
  }
done
docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is required" >&2
  exit 1
}
[ -f "$GUARD" ] && [ ! -L "$GUARD" ] || {
  echo "SQLite release guard is missing" >&2
  exit 1
}
[ -f "$IMAGE_GUARD" ] && [ ! -L "$IMAGE_GUARD" ] || {
  echo "runtime image guard is missing" >&2
  exit 1
}
[ -d "$BACKUP_DIR" ] && [ ! -L "$BACKUP_DIR" ] || {
  echo "backup directory is unavailable or unsafe" >&2
  exit 1
}
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

requested=${1:-latest}
if [ "$requested" = "latest" ]; then
  state_file="$BACKUP_DIR/last-release.state"
else
  state_file=$(python3 - "$requested" "$BACKUP_DIR" <<'PY'
from pathlib import Path
import sys

candidate = Path(sys.argv[1])
root = Path(sys.argv[2]).resolve()
if candidate.is_symlink():
    raise SystemExit("release state must not be a symbolic link")
candidate = candidate.resolve()
if candidate.parent != root or not candidate.name.startswith("release-") or not candidate.name.endswith(".state"):
    raise SystemExit("release state must be directly under the protected backup directory")
print(candidate)
PY
  )
fi
[ -f "$state_file" ] && [ ! -L "$state_file" ] || {
  echo "release state is unavailable" >&2
  exit 1
}
[ "$(stat -c '%u:%g:%a' "$state_file")" = "0:0:600" ] || {
  echo "release state owner or mode is unsafe" >&2
  exit 1
}

read_field() {
  key=$1
  sed -n "s/^$key=//p" "$state_file" | sed -n '1p'
}
release_id=$(read_field release_id)
backup_name=$(read_field backup_name)
previous_image=$(read_field previous_image)
previous_image_protection=$(read_field previous_image_protection)
candidate_image=$(read_field candidate_image)
previous_compose_name=$(read_field previous_compose_name)
previous_compose_sha256=$(read_field previous_compose_sha256)
previous_backend_hash=$(read_field previous_backend_config_hash)
previous_stt_hash=$(read_field previous_stt_config_hash)
state_format=$(read_field format)
case "$state_format" in
  2|3) ;;
  *) echo "unsupported release state format" >&2; exit 1 ;;
esac
case "$(read_field status)" in
  prepared|deployed) ;;
  *) echo "release state is not eligible for rollback" >&2; exit 1 ;;
esac
case "$release_id" in
  ""|*[!A-Za-z0-9._-]*) echo "invalid release id in state" >&2; exit 1 ;;
esac
case "$backup_name" in
  mouchen-*.db) ;;
  *) echo "invalid backup name in state" >&2; exit 1 ;;
esac
case "$backup_name" in
  *[!A-Za-z0-9._-]*) echo "unsafe backup name in state" >&2; exit 1 ;;
esac
printf '%s\n' "$previous_image" | grep -Eq '^sha256:[0-9a-f]{64}$' || {
  echo "invalid previous image id in state" >&2
  exit 1
}
printf '%s\n' "$candidate_image" | grep -Eq '^sha256:[0-9a-f]{64}$' || {
  echo "invalid candidate image id in state" >&2
  exit 1
}
case "$previous_compose_name" in
  *[!A-Za-z0-9._-]*) echo "unsafe previous Compose contract name" >&2; exit 1 ;;
esac
printf '%s\n' "$previous_compose_sha256" | grep -Eq '^[0-9a-f]{64}$' || {
  echo "invalid previous Compose contract digest" >&2
  exit 1
}
[ "$previous_compose_name" = "compose-contract-$previous_compose_sha256.yaml" ] || {
  echo "previous Compose contract name does not match its digest" >&2
  exit 1
}
for config_hash in "$previous_backend_hash" "$previous_stt_hash"; do
  printf '%s\n' "$config_hash" | grep -Eq '^[0-9a-f]{64}$' || {
    echo "invalid previous Compose service hash" >&2
    exit 1
  }
done
if [ "$state_format" = "3" ]; then
  verified_protection=$(python3 "$IMAGE_GUARD" verify --probe \
    --image "$previous_image" --release-id "$release_id")
  [ "$previous_image_protection" = "$verified_protection" ] || {
    echo "previous image protection does not match release state; database was not changed" >&2
    exit 1
  }
fi
docker image inspect "$previous_image" >/dev/null || {
  echo "previous image is no longer present; database was not changed" >&2
  exit 1
}
backup="$BACKUP_DIR/$backup_name"
checksum="$backup.sha256"
[ -f "$backup" ] && [ ! -L "$backup" ] && [ -f "$checksum" ] && [ ! -L "$checksum" ] || {
  echo "rollback backup or checksum is missing" >&2
  exit 1
}
[ "$(stat -c '%u:%g:%a' "$backup")" = "0:0:600" ] && \
  [ "$(stat -c '%u:%g:%a' "$checksum")" = "0:0:600" ] || {
  echo "rollback backup and checksum must be root:root mode 0600" >&2
  exit 1
}
python3 "$GUARD" verify --database "$backup" --checksum "$checksum" >/dev/null

previous_contract="$BACKUP_DIR/$previous_compose_name"
[ -f "$previous_contract" ] && [ ! -L "$previous_contract" ] && \
  [ "$(stat -c '%u:%g:%a' "$previous_contract")" = "0:0:600" ] && \
  [ "$(sha256sum "$previous_contract" | awk '{print $1}')" = "$previous_compose_sha256" ] || {
  echo "previous Compose contract is missing, unsafe, or corrupt" >&2
  exit 1
}
contract_legacy_mode() {
  contract_path=$1
  docker compose -p "$COMPOSE_PROJECT" -f "$contract_path" config --format json |
    python3 -c '
import json, sys
document = json.load(sys.stdin)
environment = document["services"]["backend"].get("environment", {})
value = environment.get("MOUCHEN_LEGACY_AUTH_ENABLED")
if value is None:
    print("true")  # pre-switch private-alpha contracts used the static token
elif str(value).strip().casefold() in {"1", "true", "yes", "on"}:
    print("true")
elif str(value).strip().casefold() in {"0", "false", "no", "off"}:
    print("false")
else:
    raise SystemExit("invalid legacy-auth setting in Compose contract")
'
}

# Disabling the shared owner token is an irreversible security floor. During a
# failed false-mode candidate deployment, active-compose.state still points to
# the last verified true-mode release, so automatic recovery remains possible.
# Once false mode itself is verified and active, no ordinary rollback may
# silently resurrect a shared credential.
active_state="$BACKUP_DIR/active-compose.state"
[ -f "$active_state" ] && [ ! -L "$active_state" ] && \
  [ "$(stat -c '%u:%g:%a' "$active_state")" = "0:0:600" ] || {
  echo "verified active Compose state is unavailable" >&2
  exit 1
}
active_field() {
  key=$1
  [ "$(grep -c "^$key=" "$active_state")" -eq 1 ] || {
    echo "active Compose state is malformed" >&2
    exit 1
  }
  sed -n "s/^$key=//p" "$active_state"
}
[ "$(active_field format)" = "1" ] || {
  echo "unsupported active Compose state format" >&2
  exit 1
}
active_compose_name=$(active_field contract_name)
active_compose_sha256=$(active_field sha256)
case "$active_compose_name" in
  *[!A-Za-z0-9._-]*) echo "unsafe active Compose contract name" >&2; exit 1 ;;
esac
printf '%s\n' "$active_compose_sha256" | grep -Eq '^[0-9a-f]{64}$' || {
  echo "invalid active Compose contract digest" >&2
  exit 1
}
[ "$active_compose_name" = "compose-contract-$active_compose_sha256.yaml" ] || {
  echo "active Compose contract name does not match its digest" >&2
  exit 1
}
active_contract="$BACKUP_DIR/$active_compose_name"
[ -f "$active_contract" ] && [ ! -L "$active_contract" ] && \
  [ "$(stat -c '%u:%g:%a' "$active_contract")" = "0:0:600" ] && \
  [ "$(sha256sum "$active_contract" | awk '{print $1}')" = "$active_compose_sha256" ] || {
  echo "active Compose contract is missing, unsafe, or corrupt" >&2
  exit 1
}
if [ "$(contract_legacy_mode "$active_contract")" = "false" ] && \
  [ "$(contract_legacy_mode "$previous_contract")" = "true" ]; then
  echo "rollback would re-enable the retired legacy owner token; database was not changed" >&2
  exit 1
fi
compose() {
  docker compose -p "$COMPOSE_PROJECT" -f "$previous_contract" "$@"
}
compose config --quiet
contract_hash() {
  service=$1
  compose config --hash "$service" | awk -v name="$service" \
    '$1 == name { print $NF } END { if (NF == 1) print $1 }' | sed -n '1p'
}
[ "$(contract_hash backend)" = "$previous_backend_hash" ] && \
  [ "$(contract_hash stt)" = "$previous_stt_hash" ] || {
  echo "previous Compose contract no longer reproduces its service hashes" >&2
  exit 1
}
container_ids=$(compose ps -a -q backend)
container_count=$(printf '%s\n' "$container_ids" | sed '/^$/d' | wc -l | tr -d ' ')
[ "$container_count" -eq 1 ] || {
  echo "rollback requires exactly one backend container; database was not changed" >&2
  exit 1
}
container_id=$(printf '%s\n' "$container_ids" | sed -n '1p')
[ -n "$container_id" ] || {
  echo "backend container is unavailable; database was not changed" >&2
  exit 1
}
volume_name=$(docker inspect --format \
  '{{range .Mounts}}{{if eq .Destination "/data"}}{{println .Name}}{{end}}{{end}}' \
  "$container_id" | sed -n '1p')
helper_image=$(docker inspect --format '{{.Image}}' "$container_id")
[ -n "$volume_name" ] && [ -n "$helper_image" ] || {
  echo "backend data volume or helper image is unavailable" >&2
  exit 1
}

restore_started=false
rollback_complete=false
restore_stage=
restore_input=
cleanup_restore_stage() {
  [ -n "$restore_stage" ] || return 0
  for candidate in "$restore_input-journal" "$restore_input-wal" "$restore_input-shm"; do
    if [ -e "$candidate" ] || [ -L "$candidate" ]; then
      if [ -f "$candidate" ] && [ ! -L "$candidate" ]; then
        rm -f -- "$candidate"
      else
        echo "unsafe restore staging sidecar retained for inspection" >&2
        return 1
      fi
    fi
  done
  if [ -n "$restore_input" ] && { [ -e "$restore_input" ] || [ -L "$restore_input" ]; }; then
    [ -f "$restore_input" ] && [ ! -L "$restore_input" ] || {
      echo "unsafe restore staging input retained for inspection" >&2
      return 1
    }
    rm -f -- "$restore_input"
  fi
  unknown=$(find "$restore_stage" -mindepth 1 -maxdepth 1 -print -quit)
  [ -z "$unknown" ] || {
    echo "unknown restore staging artifact retained for inspection" >&2
    return 1
  }
  rmdir -- "$restore_stage"
  restore_stage=
  restore_input=
}
cleanup_on_failure() {
  status=$?
  cleanup_restore_stage || true
  if [ "$status" -ne 0 ] && [ "$rollback_complete" != "true" ]; then
    if [ "$restore_started" = "false" ]; then
      compose start backend >/dev/null 2>&1 || true
      echo "rollback stopped before restore; the prior database remains in place" >&2
    else
      compose stop --timeout 30 backend >/dev/null 2>&1 || true
      echo "rollback restore started but service verification failed; leave the backend stopped and inspect the retained in-volume files" >&2
    fi
  fi
  exit "$status"
}
trap cleanup_on_failure 0
trap 'exit 130' 1 2 15

# Freeze writes, then preserve the failed/new release state as a second verified
# host backup. The original pre-deploy backup remains untouched.
compose stop --timeout 30 backend >/dev/null
[ "$(docker inspect --format '{{.State.Running}}' "$container_id")" = "false" ] || {
  echo "backend writer did not stop; database was not restored" >&2
  exit 1
}
set +e
rescue_backup=$(MOUCHEN_COMPOSE_FILE="$previous_contract" \
  sh "$SCRIPT_DIR/backup.sh" --print-path)
rescue_status=$?
set -e
if [ "$rescue_status" -eq 0 ]; then
  rescue_name=$(basename -- "$rescue_backup")
elif [ "$rescue_status" -eq 3 ]; then
  # If the failed candidate never created/opened a database there are no new
  # bytes to rescue. The verified pre-deploy backup is still restored.
  rescue_name=none
else
  echo "failed-release rescue backup failed; database was not restored" >&2
  exit 1
fi

restore_stage="$BACKUP_DIR/.restore-stage-$release_id-$$"
[ ! -e "$restore_stage" ] && [ ! -L "$restore_stage" ] || {
  echo "restore staging path already exists; database was not changed" >&2
  exit 1
}
install -d -o 10001 -g 10001 -m 0700 "$restore_stage"
restore_input="$restore_stage/mouchen.db"
install -o 10001 -g 10001 -m 0400 "$backup" "$restore_input"
[ "$(stat -c '%u:%g:%a' "$restore_stage")" = "10001:10001:700" ] && \
  [ "$(stat -c '%u:%g:%a' "$restore_input")" = "10001:10001:400" ] || {
  echo "restore staging ownership or mode is unsafe; database was not changed" >&2
  exit 1
}
python3 "$GUARD" verify --database "$restore_input" >/dev/null
python3 "$GUARD" finalize-artifacts --database "$restore_input" \
  --exclusive-directory >/dev/null
[ "$(sha256sum "$restore_input" | awk '{print $1}')" = \
  "$(sha256sum "$backup" | awk '{print $1}')" ] || {
  echo "restore staging digest mismatch; database was not changed" >&2
  exit 1
}
for candidate in "$restore_input-journal" "$restore_input-wal" "$restore_input-shm"; do
  if [ -e "$candidate" ] || [ -L "$candidate" ]; then
    [ -f "$candidate" ] && [ ! -L "$candidate" ] || {
      echo "unsafe restore staging sidecar; database was not changed" >&2
      exit 1
    }
    rm -f -- "$candidate"
  fi
done
unknown_restore_entry=$(find "$restore_stage" -mindepth 1 -maxdepth 1 \
  ! -path "$restore_input" -print -quit)
[ -z "$unknown_restore_entry" ] || {
  echo "unknown restore staging artifact; database was not changed" >&2
  exit 1
}

restore_started=true
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 1 \
  --user 10001:10001 --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --volume "$volume_name:/data:rw" \
  --volume "$restore_input:/restore/mouchen.db:ro" \
  --volume "$GUARD:/guard/sqlite_release_guard.py:ro" \
  --entrypoint python "$helper_image" /guard/sqlite_release_guard.py restore \
  --backup /restore/mouchen.db --target /data/mouchen.db \
  --expected-parent /data --release-id "$release_id" >/dev/null
cleanup_restore_stage

# A legacy captured contract may name a mutable image tag. Repoint every
# distinct configured runtime name before Compose starts; immutable sha256
# references already resolve to the verified previous image and need no tag.
for runtime_image in $(compose config --images | sort -u); do
  case "$runtime_image" in
    sha256:*)
      [ "$runtime_image" = "$previous_image" ] || {
        echo "previous contract refers to a different immutable image" >&2
        exit 1
      }
      ;;
    *@sha256:*)
      resolved_image=$(docker image inspect --format '{{.Id}}' "$runtime_image")
      [ "$resolved_image" = "$previous_image" ] || {
        echo "previous contract digest reference does not resolve to the prior image" >&2
        exit 1
      }
      ;;
    "") echo "previous contract has no runtime image" >&2; exit 1 ;;
    *) docker image tag "$previous_image" "$runtime_image" ;;
  esac
done
compose up -d --no-build --force-recreate stt backend >/dev/null

attempt=0
while [ "$attempt" -lt 45 ]; do
  backend_id=$(compose ps -q backend)
  stt_id=$(compose ps -q stt)
  if [ -n "$backend_id" ] && [ -n "$stt_id" ]; then
    backend_health=$(docker inspect --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$backend_id")
    stt_health=$(docker inspect --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$stt_id")
    if [ "$backend_health" = "healthy" ] && [ "$stt_health" = "healthy" ]; then
      MOUCHEN_COMPOSE_FILE="$previous_contract" sh "$VERIFY" >/dev/null
      break
    fi
    { [ "$backend_health" = "unhealthy" ] || [ "$stt_health" = "unhealthy" ]; } && exit 1
  fi
  attempt=$((attempt + 1))
  sleep 2
done
[ "$attempt" -lt 45 ] || {
  echo "rolled-back containers did not become healthy" >&2
  exit 1
}

write_state() {
  destination=$1
  temporary="$destination.tmp-$$"
  {
    printf 'format=%s\n' "$state_format"
    printf 'release_id=%s\n' "$release_id"
    printf 'backup_name=%s\n' "$backup_name"
    printf 'previous_image=%s\n' "$previous_image"
    if [ "$state_format" = "3" ]; then
      printf 'previous_image_protection=%s\n' "$previous_image_protection"
    fi
    printf 'candidate_image=%s\n' "$candidate_image"
    printf 'previous_compose_name=%s\n' "$previous_compose_name"
    printf 'previous_compose_sha256=%s\n' "$previous_compose_sha256"
    printf 'previous_backend_config_hash=%s\n' "$previous_backend_hash"
    printf 'previous_stt_config_hash=%s\n' "$previous_stt_hash"
    printf 'status=rolled_back\n'
    printf 'rescue_backup_name=%s\n' "$rescue_name"
  } > "$temporary"
  chmod 0600 "$temporary"
  mv -- "$temporary" "$destination"
}
latest_state="$BACKUP_DIR/last-release.state"
canonical_state="$BACKUP_DIR/release-$release_id.state"
[ -f "$canonical_state" ] && [ ! -L "$canonical_state" ] || {
  echo "canonical release state is missing after verified rollback" >&2
  exit 1
}
write_state "$canonical_state"
write_state "$latest_state"

# Rollback is complete only after the restored contract becomes the atomic
# active pointer used by the next guarded deployment.
active_partial="$BACKUP_DIR/.active-compose-rollback-$$.state"
{
  printf 'format=1\n'
  printf 'contract_name=%s\n' "$previous_compose_name"
  printf 'sha256=%s\n' "$previous_compose_sha256"
  printf 'backend_config_hash=%s\n' "$previous_backend_hash"
  printf 'stt_config_hash=%s\n' "$previous_stt_hash"
} > "$active_partial"
chmod 0600 "$active_partial"
mv -- "$active_partial" "$BACKUP_DIR/active-compose.state"

rollback_complete=true
trap - 0
if [ "$rescue_name" = "none" ]; then
  echo "rollback verified; failed release contained no database to retain"
else
  echo "rollback verified; failed release retained as $rescue_backup"
fi
