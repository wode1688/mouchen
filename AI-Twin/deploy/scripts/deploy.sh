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
IMAGE_GUARD="$SCRIPT_DIR/runtime_image_guard.py"
COMMERCIAL_CONFIG_GUARD="$SCRIPT_DIR/validate-commercial-config.py"

for command_name in docker flock grep python3 sha256sum stat; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "$command_name is required" >&2
    exit 1
  }
done
docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is required" >&2
  exit 1
}
[ "$(id -u)" -eq 0 ] || {
  echo "run deployment as root to validate protected release state" >&2
  exit 1
}
[ -s "$ENV_FILE" ] || {
  echo "missing deploy/.env; copy env.example and set the user id" >&2
  exit 1
}
[ -f "$IMAGE_GUARD" ] && [ ! -L "$IMAGE_GUARD" ] || {
  echo "runtime image guard is missing" >&2
  exit 1
}
[ -f "$COMMERCIAL_CONFIG_GUARD" ] && [ ! -L "$COMMERCIAL_CONFIG_GUARD" ] || {
  echo "commercial configuration guard is missing" >&2
  exit 1
}

SECRETS_DIR="$DEPLOY_DIR/secrets"
[ ! -L "$SECRETS_DIR" ] && [ -d "$SECRETS_DIR" ] || {
  echo "deploy/secrets must be a real directory" >&2
  exit 1
}
[ "$(stat -c '%u:%g:%a' "$SECRETS_DIR")" = "0:0:700" ] || {
  echo "deploy/secrets must be root:root mode 0700; rerun init-secrets.sh" >&2
  exit 1
}
for secret_name in mouchen_api_token openai_api_key mouchen_registration_code; do
  secret_path="$DEPLOY_DIR/secrets/$secret_name"
  [ ! -L "$secret_path" ] && [ -f "$secret_path" ] && [ -s "$secret_path" ] || {
    echo "missing required secret file: deploy/secrets/$secret_name" >&2
    exit 1
  }
  [ "$(stat -c '%u:%g:%a' "$secret_path")" = "10001:10001:400" ] || {
    echo "deploy/secrets/$secret_name must be owned by 10001:10001 with mode 0400" >&2
    exit 1
  }
done

compose() {
  docker compose --env-file "$ENV_FILE" -p "$COMPOSE_PROJECT" \
    -f "$COMPOSE_FILE" "$@"
}

compose config --quiet
network_name="${COMPOSE_PROJECT}_default"
if docker network inspect "$network_name" >/dev/null 2>&1; then
  network_project=$(docker network inspect --format \
    '{{index .Labels "com.docker.compose.project"}}' "$network_name")
  network_role=$(docker network inspect --format \
    '{{index .Labels "com.docker.compose.network"}}' "$network_name")
  [ "$network_project" = "$COMPOSE_PROJECT" ] && [ "$network_role" = "default" ] || {
    echo "existing project network is not owned by this My AI Twin deployment" >&2
    exit 1
  }
else
  docker network create --driver bridge \
    --label "com.docker.compose.project=$COMPOSE_PROJECT" \
    --label "com.docker.compose.network=default" \
    "$network_name" >/dev/null
fi
proxy_gateway=$(docker network inspect --format \
  '{{(index .IPAM.Config 0).Gateway}}' "$network_name")
[ -n "$proxy_gateway" ] || {
  echo "project network has no directly connected proxy gateway" >&2
  exit 1
}
MOUCHEN_TRUSTED_PROXY_CIDRS=$(python3 -c \
  'import ipaddress,sys; value=ipaddress.ip_address(sys.argv[1]); print(f"{value}/{value.max_prefixlen}")' \
  "$proxy_gateway")
export MOUCHEN_TRUSTED_PROXY_CIDRS
compose config --format json | python3 "$COMMERCIAL_CONFIG_GUARD"
configured_runtime_name=$(compose config --images | sed -n '1p')
case "$configured_runtime_name" in
  ""|sha256:*|*@sha256:*)
    echo "MOUCHEN_IMAGE must be a mutable local tag so verified rollback can repoint it" >&2
    exit 1
    ;;
esac
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
exec 9>"$BACKUP_DIR/.release.lock"
chmod 0600 "$BACKUP_DIR/.release.lock"
flock -n 9 || {
  echo "another My AI Twin deployment or rollback is already running" >&2
  exit 1
}
backend_containers=$(compose ps -a -q backend)
stt_containers=$(compose ps -a -q stt)
backend_count=$(printf '%s\n' "$backend_containers" | sed '/^$/d' | wc -l | tr -d ' ')
stt_count=$(printf '%s\n' "$stt_containers" | sed '/^$/d' | wc -l | tr -d ' ')
[ "$backend_count" -le 1 ] && [ "$stt_count" -le 1 ] || {
  echo "multiple service containers found; refusing an ambiguous release" >&2
  exit 1
}
existing_backend=$(printf '%s\n' "$backend_containers" | sed -n '1p')
existing_stt=$(printf '%s\n' "$stt_containers" | sed -n '1p')
release_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
previous_image=
if [ -n "$existing_backend" ]; then
  previous_image=$(docker inspect --format '{{.Image}}' "$existing_backend")
else
  previous_image=$(docker image inspect --format '{{.Id}}' "$configured_runtime_name" 2>/dev/null || true)
fi
if [ -n "$existing_stt" ]; then
  previous_stt_image=$(docker inspect --format '{{.Image}}' "$existing_stt")
  if [ -n "$previous_image" ] && [ "$previous_stt_image" != "$previous_image" ]; then
    echo "backend and STT use different previous images; refusing an ambiguous rollback" >&2
    exit 1
  fi
  previous_image=$previous_stt_image
fi

# A BuildKit build may replace the only mutable tag and make the running
# container's old .Image id unavailable to a new `docker run`. Pin the exact
# old id under a release-unique, content-addressed tag before build. The guard
# never repoints or deletes this tag; a verified deployment still needs it for
# one-command rollback later.
previous_image_protection=
if [ -n "$previous_image" ]; then
  previous_image_protection=$(python3 "$IMAGE_GUARD" protect \
    --image "$previous_image" --release-id "$release_id")
fi

# Building changes no database bytes and leaves current containers running.
# The immutable candidate id is captured before any service replacement.
compose build backend
candidate_image=$(docker image inspect --format '{{.Id}}' "$configured_runtime_name")
printf '%s\n' "$candidate_image" | grep -Eq '^sha256:[0-9a-f]{64}$' || {
  echo "candidate build did not produce an immutable image id" >&2
  exit 1
}
if [ -n "$previous_image_protection" ]; then
  verified_protection=$(python3 "$IMAGE_GUARD" verify \
    --image "$previous_image" --release-id "$release_id")
  [ "$verified_protection" = "$previous_image_protection" ] || {
    echo "previous image protection changed during candidate build" >&2
    exit 1
  }
fi

set +e
backup_path=$(MOUCHEN_BACKUP_HELPER_IMAGE="$previous_image_protection" \
  sh "$SCRIPT_DIR/backup.sh" --print-path)
backup_status=$?
set -e
is_new_install=false
if [ "$backup_status" -eq 3 ]; then
  [ -z "$existing_backend" ] || {
    echo "an existing backend has no database; refusing to treat it as a new install" >&2
    exit 1
  }
  is_new_install=true
elif [ "$backup_status" -ne 0 ]; then
  echo "mandatory pre-deploy backup failed; current services were not replaced" >&2
  exit 1
fi

state_file=
previous_compose_name=
previous_compose_sha256=
previous_backend_hash=
previous_stt_hash=
write_state() {
  status_value=$1
  destination=$2
  temporary="$destination.tmp-$$"
  {
    printf 'format=3\n'
    printf 'release_id=%s\n' "$release_id"
    printf 'backup_name=%s\n' "$(basename -- "$backup_path")"
    printf 'previous_image=%s\n' "$previous_image"
    printf 'previous_image_protection=%s\n' "$previous_image_protection"
    printf 'candidate_image=%s\n' "$candidate_image"
    printf 'previous_compose_name=%s\n' "$previous_compose_name"
    printf 'previous_compose_sha256=%s\n' "$previous_compose_sha256"
    printf 'previous_backend_config_hash=%s\n' "$previous_backend_hash"
    printf 'previous_stt_config_hash=%s\n' "$previous_stt_hash"
    printf 'status=%s\n' "$status_value"
    printf 'rescue_backup_name=\n'
  } > "$temporary"
  chmod 0600 "$temporary"
  mv -- "$temporary" "$destination"
}

if [ "$is_new_install" = "false" ]; then
  printf '%s\n' "$previous_image" | grep -Eq '^sha256:[0-9a-f]{64}$' || {
    echo "existing data has no immutable previous image; refusing deployment" >&2
    exit 1
  }
  docker image inspect "$previous_image" >/dev/null
  [ -n "$previous_image_protection" ] || {
    echo "existing data has no release-unique previous image protection" >&2
    exit 1
  }

  # The previous database is recoverable only when its exact orchestrator
  # contract is recoverable too. The active state is atomically published by
  # capture-compose-contract.sh after it proves that the rendered contract
  # hashes match the running containers.
  active_state="$BACKUP_DIR/active-compose.state"
  [ -f "$active_state" ] && [ ! -L "$active_state" ] && \
    [ "$(stat -c '%u:%g:%a' "$active_state")" = "0:0:600" ] || {
    echo "verified active Compose contract is missing; capture it from the currently deployed checkout before upgrading" >&2
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
  previous_compose_name=$(active_field contract_name)
  previous_compose_sha256=$(active_field sha256)
  previous_backend_hash=$(active_field backend_config_hash)
  previous_stt_hash=$(active_field stt_config_hash)
  case "$previous_compose_name" in
    *[!A-Za-z0-9._-]*) echo "active Compose contract name is unsafe" >&2; exit 1 ;;
  esac
  for recorded_hash in "$previous_compose_sha256" "$previous_backend_hash" "$previous_stt_hash"; do
    printf '%s\n' "$recorded_hash" | grep -Eq '^[0-9a-f]{64}$' || {
      echo "active Compose state contains an invalid digest" >&2
      exit 1
    }
  done
  [ "$previous_compose_name" = "compose-contract-$previous_compose_sha256.yaml" ] || {
    echo "active Compose contract name does not match its digest" >&2
    exit 1
  }
  previous_contract="$BACKUP_DIR/$previous_compose_name"
  [ -f "$previous_contract" ] && [ ! -L "$previous_contract" ] && \
    [ "$(stat -c '%u:%g:%a' "$previous_contract")" = "0:0:600" ] && \
    [ "$(sha256sum "$previous_contract" | awk '{print $1}')" = "$previous_compose_sha256" ] || {
    echo "active Compose contract is missing, unsafe, or corrupt" >&2
    exit 1
  }
  previous_compose() {
    docker compose -p "$COMPOSE_PROJECT" -f "$previous_contract" "$@"
  }
  previous_compose config --quiet
  contract_hash() {
    service=$1
    value=$(previous_compose config --hash "$service" | awk -v name="$service" \
      '$1 == name { print $NF } END { if (NF == 1) print $1 }' | sed -n '1p')
    printf '%s\n' "$value"
  }
  [ "$(contract_hash backend)" = "$previous_backend_hash" ] && \
    [ "$(contract_hash stt)" = "$previous_stt_hash" ] || {
    echo "active Compose contract no longer reproduces its recorded service hashes" >&2
    exit 1
  }
  if [ -n "$existing_backend" ] || [ -n "$existing_stt" ]; then
    [ -n "$existing_backend" ] && [ -n "$existing_stt" ] || {
      echo "only one prior service container exists; refusing an ambiguous rollback contract" >&2
      exit 1
    }
    running_backend_hash=$(docker inspect --format \
      '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$existing_backend")
    running_stt_hash=$(docker inspect --format \
      '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$existing_stt")
    [ "$running_backend_hash" = "$previous_backend_hash" ] && \
      [ "$running_stt_hash" = "$previous_stt_hash" ] || {
      echo "saved Compose contract does not match the running prior release" >&2
      exit 1
    }
  fi
  sh "$SCRIPT_DIR/preflight-upgrade.sh" "$backup_path" "$candidate_image"
  install -d -o 0 -g 0 -m 0700 "$BACKUP_DIR"
  state_file="$BACKUP_DIR/release-$release_id.state"
  [ ! -e "$state_file" ] || {
    echo "release state already exists" >&2
    exit 1
  }
  write_state prepared "$state_file"
  latest_partial="$BACKUP_DIR/last-release.state.tmp-$$"
  install -o 0 -g 0 -m 0600 "$state_file" "$latest_partial"
  mv -- "$latest_partial" "$BACKUP_DIR/last-release.state"
fi

deployment_started=false
deployment_verified=false
live_verified=false
release_cleanup() {
  status=$?
  if [ "$status" -ne 0 ] && [ "$deployment_started" = "true" ] && [ "$deployment_verified" != "true" ]; then
    if [ -n "$state_file" ]; then
      echo "deployment verification failed; starting automatic rollback" >&2
      if MOUCHEN_RELEASE_LOCK_HELD=1 sh "$SCRIPT_DIR/rollback.sh" "$state_file"; then
        echo "automatic rollback completed" >&2
      else
        echo "automatic rollback failed; backend remains stopped for manual recovery" >&2
      fi
    else
      compose stop --timeout 30 backend >/dev/null 2>&1 || true
      echo "new installation failed verification; backend stopped and no pre-existing database was removed" >&2
    fi
  fi
  exit "$status"
}
trap release_cleanup 0
trap 'exit 130' 1 2 15

deployment_started=true
MOUCHEN_IMAGE="$candidate_image" docker compose --env-file "$ENV_FILE" \
  -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" \
  up -d --no-build --force-recreate stt backend

attempt=0
while [ "$attempt" -lt 45 ]; do
  stt_container_id=$(compose ps -q stt)
  container_id=$(compose ps -q backend)
  if [ -n "$stt_container_id" ] && [ -n "$container_id" ]; then
    stt_health=$(docker inspect --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$stt_container_id")
    health=$(docker inspect --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")
    if [ "$stt_health" = "healthy" ] && [ "$health" = "healthy" ]; then
      sh "$SCRIPT_DIR/verify.sh"
      live_verified=true
      break
    fi
    { [ "$stt_health" = "unhealthy" ] || [ "$health" = "unhealthy" ]; } && break
  fi
  attempt=$((attempt + 1))
  sleep 2
done

[ "$live_verified" = "true" ] || {
  compose ps stt backend >&2
  echo "backend or resident STT did not become healthy" >&2
  exit 1
}
# Capture the just-verified runtime with the immutable image override that was
# actually used to create it. A failure here is a release failure because the
# next deployment would otherwise have no exact orchestrator rollback target.
MOUCHEN_IMAGE="$candidate_image" MOUCHEN_CONTRACT_SOURCE_FILE="$COMPOSE_FILE" \
  MOUCHEN_RELEASE_LOCK_HELD=1 sh "$SCRIPT_DIR/capture-compose-contract.sh"
deployment_verified=true
if [ -n "$state_file" ]; then
  write_state deployed "$state_file"
  latest_partial="$BACKUP_DIR/last-release.state.tmp-$$"
  install -o 0 -g 0 -m 0600 "$state_file" "$latest_partial"
  mv -- "$latest_partial" "$BACKUP_DIR/last-release.state"
fi
trap - 0
echo "deployment completed with backup, migration preflight, and rollback state verified"
