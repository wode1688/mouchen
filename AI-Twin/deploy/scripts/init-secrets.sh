#!/bin/sh
set -eu
set +x

umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
SECRETS_DIR="$DEPLOY_DIR/secrets"
ROTATE=false
ROTATE_REGISTRATION=false
CURRENT_TMP=
TERMINAL_HIDDEN=false

cleanup() {
  if [ "$TERMINAL_HIDDEN" = "true" ]; then
    stty echo 2>/dev/null || true
    printf '\n' >&2
  fi
  if [ -n "$CURRENT_TMP" ]; then
    rm -f -- "$CURRENT_TMP"
  fi
  unset SECRET_INPUT
}
trap cleanup 0
trap 'exit 130' 1 2 15

case "${1:-}" in
  "") ;;
  --rotate) ROTATE=true ;;
  --rotate-registration-code) ROTATE_REGISTRATION=true ;;
  *) echo "usage: sh scripts/init-secrets.sh [--rotate|--rotate-registration-code]" >&2; exit 2 ;;
esac

[ "$(id -u)" -eq 0 ] || {
  echo "run this script as root so secret ownership can be fixed to UID 10001" >&2
  exit 1
}
[ -t 0 ] && [ -t 1 ] || {
  echo "an interactive terminal is required; piped secrets are rejected" >&2
  exit 1
}
[ ! -L "$SECRETS_DIR" ] || {
  echo "deploy/secrets must not be a symbolic link" >&2
  exit 1
}

install -d -o 0 -g 0 -m 0700 "$SECRETS_DIR"

install_secret() {
  SECRET_NAME=$1
  SECRET_LABEL=$2
  SECRET_MINIMUM=$3
  SECRET_TARGET="$SECRETS_DIR/$SECRET_NAME"

  SHOULD_ROTATE=$ROTATE
  if [ "$ROTATE_REGISTRATION" = "true" ] && [ "$SECRET_NAME" = "mouchen_registration_code" ]; then
    SHOULD_ROTATE=true
  fi

  if [ "$SHOULD_ROTATE" != "true" ] && [ -e "$SECRET_TARGET" ]; then
    [ -f "$SECRET_TARGET" ] && [ ! -L "$SECRET_TARGET" ] || {
      echo "existing $SECRET_NAME is not a safe regular file" >&2
      exit 1
    }
    [ -s "$SECRET_TARGET" ] && [ "$(stat -c '%u:%g:%a' "$SECRET_TARGET")" = "10001:10001:400" ] || {
      echo "existing $SECRET_NAME has unsafe ownership, mode, or content" >&2
      exit 1
    }
    echo "preserved existing $SECRET_NAME" >&2
    return
  fi

  printf '%s' "$SECRET_LABEL (hidden input): " >&2
  TERMINAL_HIDDEN=true
  stty -echo
  IFS= read -r SECRET_INPUT || exit 1
  stty echo
  TERMINAL_HIDDEN=false
  printf '\n' >&2

  [ "${#SECRET_INPUT}" -ge "$SECRET_MINIMUM" ] || {
    echo "$SECRET_LABEL is too short" >&2
    exit 1
  }
  CURRENT_TMP=$(mktemp "$SECRETS_DIR/.$SECRET_NAME.XXXXXX")
  # printf is executed inside this script, so the value never appears in shell
  # history or a child process argument.
  printf '%s' "$SECRET_INPUT" > "$CURRENT_TMP"
  unset SECRET_INPUT
  chown 10001:10001 "$CURRENT_TMP"
  chmod 0400 "$CURRENT_TMP"
  mv -f -- "$CURRENT_TMP" "$SECRET_TARGET"
  CURRENT_TMP=
}

install_secret mouchen_api_token "Backend Bearer token" 32
install_secret openai_api_key "Model provider API key" 16
install_secret mouchen_registration_code "One-time account registration code" 16

echo "secret files are present with owner 10001:10001 and mode 0400"
