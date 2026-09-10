#!/bin/sh
set -eu
set +x

umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$DEPLOY_DIR/compose.yaml"
ENV_FILE=${MOUCHEN_ENV_FILE:-"$DEPLOY_DIR/.env"}
SECRETS_DIR="$DEPLOY_DIR/secrets"
RESPONSE_FILE=

cleanup() {
  if [ -n "$RESPONSE_FILE" ]; then
    rm -f -- "$RESPONSE_FILE"
  fi
  unset api_token
}
trap cleanup 0
trap 'exit 130' 1 2 15

[ "$(id -u)" -eq 0 ] || {
  echo "run the model smoke test as root" >&2
  exit 1
}
[ -s "$ENV_FILE" ] || {
  echo "deploy/.env is required" >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  echo "curl is required" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required" >&2
  exit 1
}
[ ! -L "$SECRETS_DIR" ] && [ -d "$SECRETS_DIR" ] || {
  echo "deploy/secrets must be a real directory" >&2
  exit 1
}
[ "$(stat -c '%u:%g:%a' "$SECRETS_DIR")" = "0:0:700" ] || {
  echo "deploy/secrets owner or mode is unsafe" >&2
  exit 1
}
compose() {
  docker compose --env-file "$ENV_FILE" -p mouchen -f "$COMPOSE_FILE" "$@"
}

compose config --quiet
container_id=$(compose ps -q backend)
[ -n "$container_id" ] || {
  echo "backend container is not running" >&2
  exit 1
}
health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")
[ "$health" = "healthy" ] || {
  echo "backend is not healthy" >&2
  exit 1
}
published=$(compose port backend 8788)
case "$published" in
  127.0.0.1:*) ;;
  *) echo "backend port is not bound to IPv4 loopback only" >&2; exit 1 ;;
esac
bind_port=${published##*:}
configured_user=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container_id" |
  sed -n 's/^MOUCHEN_SINGLE_USER_ID=//p')
legacy_setting=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container_id" |
  sed -n 's/^MOUCHEN_LEGACY_AUTH_ENABLED=//p')
case "$legacy_setting" in
  1|true|TRUE|yes|YES|on|ON)
    send_user_header=true
    [ -n "$configured_user" ] || {
      echo "configured single user is missing during legacy claim mode" >&2
      exit 1
    }
    api_token_path="$SECRETS_DIR/mouchen_api_token"
    [ ! -L "$api_token_path" ] && [ -s "$api_token_path" ] || {
      echo "backend token file is missing" >&2
      exit 1
    }
    [ "$(stat -c '%u:%g:%a' "$api_token_path")" = "10001:10001:400" ] || {
      echo "backend token owner or mode is unsafe" >&2
      exit 1
    }
    ;;
  0|false|FALSE|no|NO|off|OFF)
    send_user_header=false
    api_token_path=${MOUCHEN_ACCOUNT_ACCESS_TOKEN_FILE:-}
    [ -n "$api_token_path" ] && [ ! -L "$api_token_path" ] && [ -s "$api_token_path" ] || {
      echo "set MOUCHEN_ACCOUNT_ACCESS_TOKEN_FILE to a root-only account token file" >&2
      exit 1
    }
    [ "$(stat -c '%u:%g:%a' "$api_token_path")" = "0:0:400" ] || {
      echo "account token owner or mode is unsafe" >&2
      exit 1
    }
    ;;
  *) echo "legacy authentication switch is invalid" >&2; exit 1 ;;
esac

RESPONSE_FILE=$(mktemp "${TMPDIR:-/tmp}/mouchen-model-smoke.XXXXXX")
chmod 0600 "$RESPONSE_FILE"
request_payload='{"level":"L3","purpose":"deployment_model_smoke","prompt":"Reply with one short word confirming availability.","redacted_context":{"deployment_smoke":true},"outbound_approved":true,"force_private_7b":false}'

# The real token remains in shell memory and reaches curl only over stdin. The
# request payload is fixed and contains no user data. The response body goes to
# a mode-0600 temporary file and is never printed.
api_token=$(cat -- "$api_token_path")
http_code=$(
  {
    printf 'Authorization: Bearer %s\n' "$api_token"
    [ "$send_user_header" != "true" ] || printf 'X-User-Id: %s\n' "$configured_user"
  } | curl --disable --noproxy '*' --silent --show-error --output "$RESPONSE_FILE" \
    --write-out '%{http_code}' --max-time 180 --header @- \
    --header 'Content-Type: application/json' --request POST \
    --data-binary "$request_payload" \
    "http://127.0.0.1:$bind_port/v1/model/analyze"
)
unset api_token
[ "$http_code" = "200" ] || {
  echo "model smoke request failed" >&2
  exit 1
}

python3 - "$RESPONSE_FILE" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    provider = payload.get("provider")
    model = payload.get("model")
    content = payload.get("content")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError
    if provider.strip().casefold() == "template":
        raise ValueError
    if not isinstance(model, str) or not model.strip():
        raise ValueError
    if model.strip().casefold() in {"template", "deterministic-v1"}:
        raise ValueError
    if payload.get("degraded") is not False:
        raise ValueError
    if not isinstance(content, str) or not content.strip():
        raise ValueError
except Exception:
    raise SystemExit("model response did not satisfy the live-provider contract")
PY

echo "model smoke verified: live non-template provider returned a non-degraded response"
