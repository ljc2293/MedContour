#!/bin/sh
set -eu

CODEX_HOME="${CODEX_HOME:-/home/codex/.codex}"

has_content() {
    [ -f "$1" ] && [ -n "$(tr -d '[:space:]' < "$1")" ]
}

if has_content "$CODEX_HOME/auth.json"; then
    export CODEX_CREDENTIAL_SOURCE=project-auth-file
else
    export CODEX_CREDENTIAL_SOURCE=none
fi

exec "$@"
