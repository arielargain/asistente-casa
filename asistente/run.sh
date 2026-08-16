#!/usr/bin/with-contenv bash
# with-contenv es obligatorio: los complementos corren bajo s6 y un script con
# el shebang comun arranca SIN el entorno del contenedor, o sea sin
# SUPERVISOR_TOKEN, que es justo lo que necesitamos para hablar con HA.
set -e

echo "[asistente] arrancando"
mkdir -p /share/asistente/trabajo

# Red de seguridad por si with-contenv no estuviera disponible en la imagen:
# el entorno tambien vive como archivos sueltos.
if [ -z "${SUPERVISOR_TOKEN}" ] && [ -d /run/s6/container_environment ]; then
  echo "[asistente] leyendo el entorno desde /run/s6/container_environment"
  for archivo in /run/s6/container_environment/*; do
    [ -f "$archivo" ] || continue
    export "$(basename "$archivo")=$(cat "$archivo")"
  done
fi

if [ -z "${SUPERVISOR_TOKEN}" ]; then
  echo "[asistente] no hay SUPERVISOR_TOKEN: el complemento no puede hablar con HA"
  exit 1
fi

echo "[asistente] token presente, levantando el agente"
exec python3 /app/main.py
