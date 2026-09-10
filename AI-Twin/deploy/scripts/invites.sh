#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$DEPLOY_DIR/compose.yaml"
ENV_FILE=${MOUCHEN_ENV_FILE:-"$DEPLOY_DIR/.env"}
COMPOSE_PROJECT=mouchen

command -v docker >/dev/null 2>&1 || {
  echo "docker is required" >&2
  exit 1
}
[ -s "$ENV_FILE" ] || {
  echo "missing deploy/.env" >&2
  exit 1
}

exec docker compose --env-file "$ENV_FILE" -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" \
  exec -T backend python -m app.invite_admin "$@"
