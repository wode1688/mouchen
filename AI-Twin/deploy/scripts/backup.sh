#!/bin/sh
set -eu
set +x

umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE=${MOUCHEN_COMPOSE_FILE:-"$DEPLOY_DIR/compose.yaml"}
ENV_FILE=${MOUCHEN_ENV_FILE:-"$DEPLOY_DIR/.env"}
BACKUP_DIR=${MOUCHEN_BACKUP_DIR:-"$DEPLOY_DIR/backups"}
COMPOSE_PROJECT=mouchen
GUARD="$SCRIPT_DIR/sqlite_release_guard.py"
PRINT_PATH=false
REQUESTED_HELPER_IMAGE=${MOUCHEN_BACKUP_HELPER_IMAGE:-}

case "${1:-}" in
  "") ;;
  --print-path) PRINT_PATH=true ;;
  *) echo "usage: backup.sh [--print-path]" >&2; exit 2 ;;
esac

[ -s "$ENV_FILE" ] || {
  echo "deploy/.env is required" >&2
  exit 1
}
[ "$(id -u)" -eq 0 ] || {
  echo "run backup as root so host backup ownership can be verified" >&2
  exit 1
}
for command_name in docker find grep python3 sha256sum stat; do
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

compose() {
  docker compose --env-file "$ENV_FILE" -p "$COMPOSE_PROJECT" \
    -f "$COMPOSE_FILE" "$@"
}

compose config --quiet
container_ids=$(compose ps -a -q backend)
container_count=$(printf '%s\n' "$container_ids" | sed '/^$/d' | wc -l | tr -d ' ')
[ "$container_count" -le 1 ] || {
  echo "multiple backend containers found; refusing to guess a data volume" >&2
  exit 1
}
container_id=$(printf '%s\n' "$container_ids" | sed -n '1p')
volume_name=
helper_image=
container_runtime_image=
if [ -n "$container_id" ]; then
  volume_name=$(docker inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/data"}}{{println .Name}}{{end}}{{end}}' \
    "$container_id" | sed -n '1p')
  container_runtime_image=$(docker inspect --format '{{.Image}}' "$container_id")
  helper_image=$container_runtime_image
else
  volume_candidates=$(docker volume ls --quiet \
    --filter label=com.docker.compose.project="$COMPOSE_PROJECT" \
    --filter label=com.docker.compose.volume=mouchen_data)
  candidate_count=$(printf '%s\n' "$volume_candidates" | sed '/^$/d' | wc -l | tr -d ' ')
  [ "$candidate_count" -le 1 ] || {
    echo "multiple My AI Twin data volumes found; refusing to guess" >&2
    exit 1
  }
  volume_name=$(printf '%s\n' "$volume_candidates" | sed -n '1p')
  configured_image=$(compose config --images | sed -n '1p')
  helper_image=$(docker image inspect --format '{{.Id}}' "$configured_image" 2>/dev/null || true)
fi

# deploy.sh passes the release-unique protection tag created before Compose
# overwrites the mutable build tag. Resolve it back to an immutable id and, if
# a container exists, prove it is exactly that container's runtime image.
if [ -n "$REQUESTED_HELPER_IMAGE" ]; then
  requested_helper_id=$(docker image inspect --format '{{.Id}}' \
    "$REQUESTED_HELPER_IMAGE" 2>/dev/null || true)
  printf '%s\n' "$requested_helper_id" | grep -Eq '^sha256:[0-9a-f]{64}$' || {
    echo "requested protected backup image is unavailable" >&2
    exit 1
  }
  if [ -n "$container_runtime_image" ] && \
    [ "$requested_helper_id" != "$container_runtime_image" ]; then
    echo "requested protected backup image does not match the running backend" >&2
    exit 1
  fi
  helper_image=$requested_helper_id
fi

# Exit 3 means a genuinely new installation: no named volume or no database
# file exists. Any malformed or unreadable existing database remains a hard
# failure and can never be mistaken for a fresh install.
[ -n "$volume_name" ] || {
  echo "no existing My AI Twin data volume" >&2
  exit 3
}
[ -n "$helper_image" ] || {
  echo "no trusted local backend image is available to read the data volume" >&2
  exit 1
}

run_probe() {
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 1 \
    --user 10001:10001 --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
    --volume "$volume_name:/source:ro" \
    --entrypoint python "$helper_image" "$@"
}

set +e
run_probe -c \
  'from pathlib import Path; raise SystemExit(0 if Path("/source/mouchen.db").is_file() else 3)'
probe_status=$?
set -e
[ "$probe_status" -eq 0 ] || {
  if [ "$probe_status" -eq 3 ]; then
    echo "My AI Twin data volume contains no database" >&2
    exit 3
  fi
  echo "could not inspect the My AI Twin data volume" >&2
  exit 1
}

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_name="mouchen-$timestamp-$$.db"
host_partial="$BACKUP_DIR/$backup_name.partial"
host_final="$BACKUP_DIR/$backup_name"
sha_partial="$BACKUP_DIR/$backup_name.sha256.partial"
sha_final="$BACKUP_DIR/$backup_name.sha256"
stage_dir="$BACKUP_DIR/.backup-stage-$timestamp-$$"
stage_partial="$stage_dir/$backup_name.partial"
COMPLETED=false

reject_unknown_sqlite_siblings() {
  database_path=$1
  parent=$(dirname -- "$database_path")
  name=$(basename -- "$database_path")
  unknown=$(find "$parent" -mindepth 1 -maxdepth 1 -name "$name-*" \
    ! -name "$name-journal" ! -name "$name-wal" ! -name "$name-shm" \
    -print -quit)
  [ -z "$unknown" ] || {
    echo "unknown SQLite staging artifact; refusing to publish backup" >&2
    exit 1
  }
}

remove_known_sqlite_sidecars() {
  database_path=$1
  for suffix in -journal -wal -shm; do
    sidecar="$database_path$suffix"
    if [ -e "$sidecar" ] || [ -L "$sidecar" ]; then
      [ -f "$sidecar" ] && [ ! -L "$sidecar" ] || {
        echo "unsafe SQLite sidecar; refusing to publish backup" >&2
        exit 1
      }
      rm -f -- "$sidecar"
    fi
  done
}

cleanup() {
  rm -f -- "$host_partial" "$sha_partial"
  rm -f -- "$host_partial-journal" "$host_partial-wal" "$host_partial-shm"
  rm -f -- "$host_final-journal" "$host_final-wal" "$host_final-shm"
  rm -f -- "$stage_partial" "$stage_partial-journal" \
    "$stage_partial-wal" "$stage_partial-shm"
  rmdir -- "$stage_dir" 2>/dev/null || true
  if [ "$COMPLETED" != "true" ]; then
    rm -f -- "$host_final" "$sha_final"
  fi
}
trap cleanup 0
trap 'exit 130' 1 2 15

[ ! -e "$host_partial" ] && [ ! -e "$host_final" ] || {
  echo "backup target already exists" >&2
  exit 1
}
# The live data directory is deliberately owned by the non-root application
# account.  The helper drops every capability, so it must run as that same
# account to traverse the read-only source.  It receives only a fresh empty
# staging directory; root reclaims the completed snapshot before publishing it.
install -d -o 10001 -g 10001 -m 0700 "$stage_dir"

run_snapshot() {
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 1 \
    --user 10001:10001 --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
    --volume "$volume_name:/source:ro" \
    --volume "$stage_dir:/host-backups:rw" \
    --volume "$GUARD:/guard/sqlite_release_guard.py:ro" \
    --entrypoint python "$helper_image" "$@"
}

# The volume is mounted read-only. sqlite3.Connection.backup reads the live WAL
# transactionally and writes only to a new empty root-only staging directory;
# the helper never receives access to previously completed backups.
run_snapshot /guard/sqlite_release_guard.py snapshot \
  --source /source/mouchen.db \
  --destination "/host-backups/$backup_name.partial" >/dev/null
[ -f "$stage_partial" ] && [ ! -L "$stage_partial" ] || {
  echo "snapshot helper did not produce one regular database file" >&2
  exit 1
}
python3 "$GUARD" finalize-artifacts --database "$stage_partial" \
  --exclusive-directory >/dev/null
reject_unknown_sqlite_siblings "$stage_partial"
remove_known_sqlite_sidecars "$stage_partial"
unexpected_stage_entry=$(find "$stage_dir" -mindepth 1 -maxdepth 1 \
  ! -path "$stage_partial" -print -quit)
[ -z "$unexpected_stage_entry" ] || {
  echo "snapshot staging directory contains an unknown artifact" >&2
  exit 1
}
mv -- "$stage_partial" "$host_partial"
rmdir -- "$stage_dir"
chown 0:0 "$host_partial"
chmod 0600 "$host_partial"
[ "$(stat -c '%u:%g:%a' "$host_partial")" = "0:0:600" ] || {
  echo "host backup owner or mode is unsafe" >&2
  exit 1
}
python3 "$GUARD" verify --database "$host_partial" >/dev/null
python3 "$GUARD" finalize-artifacts --database "$host_partial" >/dev/null
reject_unknown_sqlite_siblings "$host_partial"
remove_known_sqlite_sidecars "$host_partial"

(
  cd "$BACKUP_DIR"
  digest=$(sha256sum "$backup_name.partial" | awk '{print $1}')
  printf '%s  %s\n' "$digest" "$backup_name" > "$backup_name.sha256.partial"
)
chmod 0600 "$sha_partial"
mv -- "$host_partial" "$host_final"
mv -- "$sha_partial" "$sha_final"
(
  cd "$BACKUP_DIR"
  sha256sum --check --status "$backup_name.sha256"
)
python3 "$GUARD" verify --database "$host_final" --checksum "$sha_final" >/dev/null
python3 "$GUARD" finalize-artifacts --database "$host_final" >/dev/null
reject_unknown_sqlite_siblings "$host_final"
remove_known_sqlite_sidecars "$host_final"
COMPLETED=true

if [ "$PRINT_PATH" = "true" ]; then
  printf '%s\n' "$host_final"
else
  echo "backup created and verified: $host_final"
fi
