#!/bin/sh
set -eu
set +x

umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$DEPLOY_DIR/compose.yaml"
ENV_FILE=${MOUCHEN_ENV_FILE:-"$DEPLOY_DIR/.env"}
BACKUP_DIR=${MOUCHEN_BACKUP_DIR:-"$DEPLOY_DIR/backups"}
COMPOSE_PROJECT=mouchen
GUARD="$SCRIPT_DIR/sqlite_release_guard.py"

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || {
  echo "usage: preflight-upgrade.sh VERIFIED_BACKUP [CANDIDATE_IMAGE_ID]" >&2
  exit 2
}
backup_input=$1
candidate_image=${2:-}

[ "$(id -u)" -eq 0 ] || {
  echo "run migration preflight as root" >&2
  exit 1
}
[ -s "$ENV_FILE" ] || {
  echo "deploy/.env is required" >&2
  exit 1
}
for command_name in docker grep python3 sha256sum stat; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "$command_name is required" >&2
    exit 1
  }
done
[ -f "$GUARD" ] && [ ! -L "$GUARD" ] || {
  echo "SQLite release guard is missing" >&2
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

backup=$(python3 - "$backup_input" "$BACKUP_DIR" <<'PY'
from pathlib import Path
import sys

candidate = Path(sys.argv[1])
root = Path(sys.argv[2]).resolve()
if candidate.is_symlink():
    raise SystemExit("backup must not be a symbolic link")
candidate = candidate.resolve()
if candidate.parent != root or not candidate.name.startswith("mouchen-") or not candidate.name.endswith(".db"):
    raise SystemExit("backup must be a My AI Twin backup directly under the protected backup directory")
print(candidate)
PY
)
checksum="$backup.sha256"
[ -f "$backup" ] && [ ! -L "$backup" ] && [ -f "$checksum" ] && [ ! -L "$checksum" ] || {
  echo "verified backup and checksum sidecar are required" >&2
  exit 1
}
[ "$(stat -c '%u:%g:%a' "$backup")" = "0:0:600" ] && \
  [ "$(stat -c '%u:%g:%a' "$checksum")" = "0:0:600" ] || {
  echo "backup and checksum must be root:root mode 0600" >&2
  exit 1
}
python3 "$GUARD" verify --database "$backup" --checksum "$checksum" >/dev/null

compose() {
  docker compose --env-file "$ENV_FILE" -p "$COMPOSE_PROJECT" \
    -f "$COMPOSE_FILE" "$@"
}
compose config --quiet
if [ -z "$candidate_image" ]; then
  configured_image=$(compose config --images | sed -n '1p')
  candidate_image=$(docker image inspect --format '{{.Id}}' "$configured_image")
fi
printf '%s\n' "$candidate_image" | grep -Eq '^sha256:[0-9a-f]{64}$' || {
  echo "candidate image must be an immutable local image id" >&2
  exit 1
}
docker image inspect "$candidate_image" >/dev/null

scratch="$BACKUP_DIR/.preflight-$$.db"
cleanup() {
  rm -f -- "$scratch"
}
trap cleanup 0
trap 'exit 130' 1 2 15
[ ! -e "$scratch" ] || {
  echo "preflight scratch path already exists" >&2
  exit 1
}
install -o 10001 -g 10001 -m 0600 "$backup" "$scratch"

# The candidate receives no network, secrets, live volume, or writable host
# path. Its startup migrations execute only against a disposable tmpfs copy.
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 128 --memory 2g --cpus 1 \
  --user 10001:10001 --env-file "$ENV_FILE" \
  --env MOUCHEN_DB_PATH=/work/mouchen.db \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=1700 \
  --tmpfs /work:rw,noexec,nosuid,nodev,size=1g,uid=10001,gid=10001,mode=0700 \
  --volume "$scratch:/input/mouchen.db:ro" \
  --volume "$GUARD:/guard/sqlite_release_guard.py:ro" \
  --entrypoint python "$candidate_image" /guard/sqlite_release_guard.py preflight \
  --database /input/mouchen.db --work-database /work/mouchen.db >/dev/null

echo "migration preflight passed on an isolated verified backup"
