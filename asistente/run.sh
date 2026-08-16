#!/usr/bin/env bash
set -e

echo "[asistente] arrancando"
mkdir -p /share/asistente/trabajo

if [ -z "${SUPERVISOR_TOKEN}" ]; then
  echo "[asistente] no hay SUPERVISOR_TOKEN: el complemento no puede hablar con HA"
  exit 1
fi

exec python3 /app/main.py
