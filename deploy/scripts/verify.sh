#!/bin/sh
set -eu
set +x

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE=${MOUCHEN_COMPOSE_FILE:-"$DEPLOY_DIR/compose.yaml"}
ENV_FILE=${MOUCHEN_ENV_FILE:-"$DEPLOY_DIR/.env"}
SECRETS_DIR="$DEPLOY_DIR/secrets"
COMPOSE_PROJECT=mouchen

[ -s "$ENV_FILE" ] || {
  echo "deploy/.env is required" >&2
  exit 1
}
[ "$(id -u)" -eq 0 ] || {
  echo "run verification as root to validate secret ownership" >&2
  exit 1
}
[ ! -L "$SECRETS_DIR" ] && [ -d "$SECRETS_DIR" ] || {
  echo "deploy/secrets must be a real directory" >&2
  exit 1
}
[ "$(stat -c '%u:%g:%a' "$SECRETS_DIR")" = "0:0:700" ] || {
  echo "deploy/secrets must be root:root mode 0700" >&2
  exit 1
}
for secret_name in mouchen_api_token openai_api_key mouchen_registration_code; do
  secret_path="$SECRETS_DIR/$secret_name"
  [ ! -L "$secret_path" ] && [ -f "$secret_path" ] && [ -s "$secret_path" ] || {
    echo "required secret file is missing" >&2
    exit 1
  }
  [ "$(stat -c '%u:%g:%a' "$secret_path")" = "10001:10001:400" ] || {
    echo "secret owner or mode is unsafe" >&2
    exit 1
  }
done

compose() {
  docker compose --env-file "$ENV_FILE" -p "$COMPOSE_PROJECT" \
    -f "$COMPOSE_FILE" "$@"
}

compose config --quiet
container_id=$(compose ps -q backend)
stt_container_id=$(compose ps -q stt)
[ -n "$container_id" ] && [ -n "$stt_container_id" ] || {
  echo "backend or resident STT container is not running" >&2
  exit 1
}

for runtime_id in "$container_id" "$stt_container_id"; do
  [ "$(docker inspect --format '{{.Config.User}}' "$runtime_id")" = "10001:10001" ] || {
    echo "a runtime is not using the expected non-root user" >&2
    exit 1
  }
  [ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$runtime_id")" = "true" ] || {
    echo "a runtime root filesystem is not read-only" >&2
    exit 1
  }
  case "$(docker inspect --format '{{join .HostConfig.CapDrop ","}}' "$runtime_id")" in
    *ALL*) ;;
    *) echo "a runtime did not drop all capabilities" >&2; exit 1 ;;
  esac
  case "$(docker inspect --format '{{join .HostConfig.SecurityOpt ","}}' "$runtime_id")" in
    *no-new-privileges:true*) ;;
    *) echo "no-new-privileges is missing" >&2; exit 1 ;;
  esac
  [ "$(docker inspect --format '{{.HostConfig.Memory}}' "$runtime_id")" -gt 0 ] || {
    echo "a runtime memory limit is missing" >&2
    exit 1
  }
  [ "$(docker inspect --format '{{.HostConfig.NanoCpus}}' "$runtime_id")" -gt 0 ] || {
    echo "a runtime CPU limit is missing" >&2
    exit 1
  }
  [ "$(docker inspect --format '{{.HostConfig.PidsLimit}}' "$runtime_id")" -gt 0 ] || {
    echo "a runtime PID limit is missing" >&2
    exit 1
  }
  [ "$(docker inspect --format '{{.HostConfig.LogConfig.Type}}' "$runtime_id")" = "json-file" ] || {
    echo "bounded json-file logging is missing" >&2
    exit 1
  }
done

published=$(compose port backend 8788)
case "$published" in
  127.0.0.1:*) ;;
  *) echo "backend port is not bound to IPv4 loopback only" >&2; exit 1 ;;
esac

health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")
[ "$health" = "healthy" ] || {
  echo "backend health is $health" >&2
  exit 1
}
stt_health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$stt_container_id")
[ "$stt_health" = "healthy" ] || {
  echo "resident STT health is $stt_health" >&2
  exit 1
}
[ -z "$(compose port stt 8080 2>/dev/null || true)" ] || {
  echo "resident STT port must not be published" >&2
  exit 1
}

stt_network_count=0
stt_network_id=
for network_id in $(docker inspect --format '{{range .NetworkSettings.Networks}}{{println .NetworkID}}{{end}}' "$stt_container_id"); do
  stt_network_count=$((stt_network_count + 1))
  stt_network_id=$network_id
done
[ "$stt_network_count" -eq 1 ] && [ -n "$stt_network_id" ] || {
  echo "resident STT must be attached to exactly one Docker network" >&2
  exit 1
}
[ "$(docker network inspect --format '{{.Internal}}' "$stt_network_id")" = "true" ] || {
  echo "resident STT network is not internal" >&2
  exit 1
}
backend_shares_stt_network=false
for network_id in $(docker inspect --format '{{range .NetworkSettings.Networks}}{{println .NetworkID}}{{end}}' "$container_id"); do
  [ "$network_id" = "$stt_network_id" ] && backend_shares_stt_network=true
done
[ "$backend_shares_stt_network" = "true" ] || {
  echo "backend does not share the isolated STT network" >&2
  exit 1
}

compose exec -T stt python - <<'PY'
import hashlib
from pathlib import Path

from app.paraformer_server import ParaformerConfig

config = ParaformerConfig.from_environment()
expected_assets = (
    (
        config.model_path,
        Path("/opt/mouchen-stt/models/paraformer/model.int8.onnx"),
        243_371_218,
        "f36a0433bcf096bd6d6f11b80a3ac8bed110bdca632fe0d731df8d1a84475945",
    ),
    (
        config.tokens_path,
        Path("/opt/mouchen-stt/models/paraformer/tokens.txt"),
        75_756,
        "59aba8873a2ed1e122c25fee421e25f283b63290efbde85c1f01a853d83cb6e6",
    ),
)
for configured_path, expected_path, expected_size, expected_sha256 in expected_assets:
    if configured_path != expected_path or configured_path.is_symlink():
        raise SystemExit("container Paraformer asset path validation failed")
    try:
        metadata = configured_path.stat()
    except OSError as exc:
        raise SystemExit("container Paraformer asset is unavailable") from exc
    if not configured_path.is_file() or metadata.st_size != expected_size:
        raise SystemExit("container Paraformer asset size validation failed")
    with configured_path.open("rb") as asset_file:
        actual_sha256 = hashlib.file_digest(asset_file, "sha256").hexdigest()
    if actual_sha256 != expected_sha256:
        raise SystemExit("container Paraformer asset digest validation failed")
PY

compose exec -T backend python - <<'PY'
import asyncio
import hashlib
import http.client
import json
import os
import stat
from pathlib import Path

from app.config import secret_value

try:
    if os.geteuid() != 10001 or os.getegid() != 10001:
        raise RuntimeError
    legacy_value = os.environ.get("MOUCHEN_LEGACY_AUTH_ENABLED")
    # A captured pre-commercial contract has no switch and therefore used the
    # static token. New contracts always set the switch explicitly.
    legacy_enabled = (
        True
        if legacy_value is None
        else legacy_value.strip().casefold() in {"1", "true", "yes", "on"}
    )
    required_names = ["OPENAI_API_KEY"]
    if legacy_enabled:
        required_names.append("MOUCHEN_API_TOKEN")
    if os.environ.get("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE_FILE"):
        required_names.append("MOUCHEN_BOOTSTRAP_REGISTRATION_CODE")
    for name in required_names:
        path = Path(os.environ[f"{name}_FILE"])
        metadata = path.stat()
        if metadata.st_uid != 10001 or metadata.st_gid != 10001:
            raise RuntimeError
        if stat.S_IMODE(metadata.st_mode) != 0o400:
            raise RuntimeError
        if not secret_value(name):
            raise RuntimeError
except Exception:
    raise SystemExit("container secret validation failed")

try:
    health_connection = http.client.HTTPConnection("stt", 8080, timeout=3)
    try:
        health_connection.request("GET", "/health")
        health_response = health_connection.getresponse()
        health_body = health_response.read(256)
    finally:
        health_connection.close()
    if health_response.status != 200 or json.loads(health_body) != {"status": "ok"}:
        raise RuntimeError
except Exception as exc:
    raise SystemExit("container Paraformer health validation failed") from exc

if os.getenv("MOUCHEN_LOCAL_STT_ENABLED", "").strip().casefold() in {"1", "true", "yes", "on"}:
    from app.local_stt import LocalSttConfig, LocalSttService

    model = Path(os.environ["MOUCHEN_LOCAL_STT_MODEL"])
    expected_model_sha256 = os.environ.get("MOUCHEN_LOCAL_STT_MODEL_SHA256", "").strip().lower()
    try:
        expected_model_size = int(os.environ.get("MOUCHEN_LOCAL_STT_MODEL_SIZE", ""))
    except ValueError as exc:
        raise SystemExit("container STT model size configuration is invalid") from exc
    if (
        not model.is_file()
        or expected_model_size <= 0
        or model.stat().st_size != expected_model_size
    ):
        raise SystemExit("container STT model validation failed")
    if len(expected_model_sha256) != 64:
        raise SystemExit("container STT model digest configuration is invalid")
    with model.open("rb") as model_file:
        actual_model_sha256 = hashlib.file_digest(model_file, "sha256").hexdigest()
    if actual_model_sha256 != expected_model_sha256:
        raise SystemExit("container STT model digest validation failed")
    try:
        service = LocalSttService(LocalSttConfig.from_environment())
        service.validate_configuration()
        if (
            service.config.server_url != "http://stt:8080/inference"
            or service.config.server_engine != "local-paraformer-sherpa-onnx"
        ):
            raise RuntimeError
        resident_silence = service._run_server(
            b"\x00\x00" * 16_000,
            16_000,
            "zh",
        )
        if not isinstance(resident_silence, str):
            raise RuntimeError
        silence = asyncio.run(
            service.transcribe(
                b"\x00\x00" * 16_000,
                sample_rate=16_000,
                language="zh",
            )
        )
        if silence.transcript or silence.engine != "local-paraformer-sherpa-onnx":
            raise RuntimeError
    except Exception as exc:
        raise SystemExit("container STT inference validation failed") from exc
PY

command -v curl >/dev/null 2>&1 || {
  echo "curl is required for API verification" >&2
  exit 1
}
bind_port=${published##*:}
ready=$(curl --disable --noproxy '*' --fail --silent --show-error --max-time 3 \
  "http://127.0.0.1:$bind_port/ready")
case "$ready" in
  *'"status":"ready"'*'"database":"ready"'*'"authentication":"ready"'*) ;;
  *'"status":"ready"'*'"database":"ready"'*'"authentication":"bootstrap-required"'*) ;;
  *) echo "unexpected readiness response" >&2; exit 1 ;;
esac

legacy_setting=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container_id" |
  sed -n 's/^MOUCHEN_LEGACY_AUTH_ENABLED=//p')
case "$legacy_setting" in
  "") legacy_enabled=true ;;
  1|true|TRUE|yes|YES|on|ON) legacy_enabled=true ;;
  0|false|FALSE|no|NO|off|OFF) legacy_enabled=false ;;
  *) echo "legacy authentication switch is invalid" >&2; exit 1 ;;
esac
commercial_setting=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container_id" |
  sed -n 's/^MOUCHEN_COMMERCIAL_MULTI_USER=//p')
case "$commercial_setting" in
  ""|1|true|TRUE|yes|YES|on|ON) commercial_enabled=true ;;
  0|false|FALSE|no|NO|off|OFF) commercial_enabled=false ;;
  *) echo "commercial multi-user switch is invalid" >&2; exit 1 ;;
esac
if [ "$commercial_enabled" = "true" ]; then
  default_gateway=$(docker inspect --format \
    '{{with (index .NetworkSettings.Networks "mouchen_default")}}{{.Gateway}}{{end}}' \
    "$container_id")
  [ -n "$default_gateway" ] || {
    echo "commercial reverse-proxy gateway is unavailable" >&2
    exit 1
  }
  docker inspect --format '{{json .Config.Env}}' "$container_id" |
    python3 "$SCRIPT_DIR/validate-commercial-config.py" \
      --environment-list --gateway "$default_gateway"
fi
configured_user=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container_id" |
  sed -n 's/^MOUCHEN_SINGLE_USER_ID=//p')
if [ "$legacy_enabled" = "true" ] && [ -z "$configured_user" ]; then
  echo "configured single user is missing during legacy claim mode" >&2
  exit 1
fi

wrong_code=$(
  {
    printf '%s\n' 'Authorization: Bearer deliberately-invalid-token'
    [ -z "$configured_user" ] || printf 'X-User-Id: %s\n' "$configured_user"
  } | curl --disable --noproxy '*' --silent --show-error --output /dev/null \
    --write-out '%{http_code}' --max-time 3 --header @- \
    "http://127.0.0.1:$bind_port/v1/capabilities"
)
[ "$wrong_code" = "401" ] || {
  echo "invalid Bearer token was not rejected" >&2
  exit 1
}

# The token is read into shell memory and passed to curl over stdin. It never
# appears in argv, command output, the Compose environment, or a temporary file.
api_token=$(cat -- "$SECRETS_DIR/mouchen_api_token")
correct_code=$(
  {
    printf 'Authorization: Bearer %s\n' "$api_token"
    [ -z "$configured_user" ] || printf 'X-User-Id: %s\n' "$configured_user"
  } | curl --disable --noproxy '*' --silent --show-error --output /dev/null \
    --write-out '%{http_code}' --max-time 3 --header @- \
    "http://127.0.0.1:$bind_port/v1/capabilities"
)
unset api_token
if [ "$legacy_enabled" = "true" ]; then
  [ "$correct_code" = "200" ] || {
    echo "enabled legacy owner-token probe failed" >&2
    exit 1
  }
else
  [ "$correct_code" = "401" ] || {
    echo "disabled legacy owner token still grants API access" >&2
    exit 1
  }
fi

echo "verified: secrets, account/legacy-auth mode, database, Paraformer assets/health/isolation, non-root, read-only, localhost-only"
