#!/bin/sh
set -eu

if [ -z "${KUBEDOOR_INTERNAL_TOKEN:-}" ]; then
  mkdir -p /run/kubedoor
  chmod 700 /run/kubedoor
  (umask 077 && python3 -c 'import secrets; print(secrets.token_hex(32))' > /run/kubedoor/internal.token)
  chmod 600 /run/kubedoor/internal.token
fi

exec "$@"
