"""Cliente de Home Assistant.

Adentro de un complemento, HA se alcanza por el Supervisor con el token que
el propio Supervisor inyecta en el entorno. No hace falta ninguna credencial
de Ariel ni un token de larga duracion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

log = logging.getLogger("hogar")

TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
BASE_REST = "http://supervisor/core/api"
BASE_WS = "ws://supervisor/core/websocket"
BASE_SUPERVISOR = "http://supervisor"


class Hogar:
    """Habla con Home Assistant: lee estados, llama servicios y escucha cambios."""

    def __init__(self, sesion: aiohttp.ClientSession) -> None:
        self._sesion = sesion
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._id = 0
        self._pendientes: dict[int, asyncio.Future] = {}
        self._oyentes: list[Callable[[dict], Awaitable[None]]] = []
        self._estados: dict[str, dict] = {}
        self._listo = asyncio.Event()

    # ---------------------------------------------------------------- REST

    @property
    def _cabeceras(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

    async def llamar_servicio(
        self, dominio: str, servicio: str, datos: dict | None = None, *, respuesta: bool = False
    ) -> Any:
        """Ejecuta un servicio de HA. Con respuesta=True devuelve lo que el servicio conteste."""
        url = f"{BASE_REST}/services/{dominio}/{servicio}"
        if respuesta:
            url += "?return_response"
        async with self._sesion.post(url, headers=self._cabeceras, json=datos or {}) as r:
            cuerpo = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"{dominio}.{servicio} fallo ({r.status}): {cuerpo[:300]}")
            try:
                return json.loads(cuerpo) if cuerpo else None
            except json.JSONDecodeError:
                return cuerpo

    async def config_ha(self, camino: str, metodo: str = "GET", datos: dict | None = None):
        """Habla con la API de configuracion de HA (scripts, automatizaciones)."""
        url = f"{BASE_REST}/config/{camino}"
        async with self._sesion.request(
            metodo, url, headers=self._cabeceras, json=datos
        ) as r:
            cuerpo = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"{metodo} {camino} fallo ({r.status})")
            return json.loads(cuerpo) if cuerpo.strip() else {}

    async def registro(self) -> str:
        """Devuelve el log de errores de HA (util para diagnosticar)."""
        async with self._sesion.get(f"{BASE_REST}/error_log", headers=self._cabeceras) as r:
            return await r.text()

    async def supervisor(self, camino: str, metodo: str = "GET", datos: dict | None = None) -> dict:
        """Pega contra la API del Supervisor: complementos, red, sistema."""
        url = f"{BASE_SUPERVISOR}{camino}"
        async with self._sesion.request(
            metodo, url, headers={"Authorization": f"Bearer {TOKEN}"}, json=datos
        ) as r:
            return await r.json()

    # ------------------------------------------------------------ estados

    def estado(self, entidad: str) -> dict | None:
        return self._estados.get(entidad)

    def todos(self) -> dict[str, dict]:
        return dict(self._estados)

    def caidas(self, ignorar: set[str] | None = None) -> list[str]:
        """Entidades que ahora mismo no responden.

        Solo cuenta 'unavailable'. 'unknown' NO es una caida: para los botones,
        los emisores de infrarrojo, los motores de voz y varios tipos mas, es
        su estado normal de reposo y nunca cambia.
        """
        ignorar = ignorar or set()
        return [
            e
            for e, st in self._estados.items()
            if st.get("state") == "unavailable" and e not in ignorar
        ]


    async def referencias_rotas(self, ignorar: set[str] | None = None) -> dict:
        """Automatizaciones y scripts que apuntan a entidades que no existen o estan caidas.

        Orden permanente de Ariel (15/9/2026): cuando un aparato se cambia o se
        renombra, las automatizaciones tienen que seguirlo solas. Esto es lo que
        lo detecta; el agente lo corrige con reemplazar_entidad.
        """
        import re

        ignorar = ignorar or set()
        patron = re.compile(
            r"\b(sensor|binary_sensor|switch|light|camera|media_player|remote|climate|"
            r"humidifier|lock|cover|fan|vacuum|number|select|input_boolean|input_number|"
            r"input_text|button|automation|script|scene|device_tracker|alarm_control_panel|"
            r"siren|event|text|counter|timer|group|assist_satellite|image|todo|calendar)"
            r"\.([a-z0-9_]+)\b"
        )
        verbos = {
            "turn_on", "turn_off", "toggle", "press", "select_option", "set_value", "play_media",
            "volume_set", "volume_mute", "volume_up", "volume_down", "select_source",
            "send_command", "learn_command", "alarm_trigger", "alarm_arm_home", "alarm_arm_away",
            "alarm_arm_night", "alarm_disarm", "start", "stop", "pause", "reboot", "reload",
            "restart", "reload_config_entry", "set_mode", "set_humidity", "set_temperature",
            "set_hvac_mode", "snapshot", "record", "media_stop", "media_play", "media_pause",
            "media_next_track", "media_previous_track", "create", "dismiss", "speak", "announce",
            "start_conversation", "update_entity", "set_datetime", "increment", "decrement",
            "reset", "return_to_base", "locate", "clean_spot", "set_fan_speed", "oscillate",
            "set_percentage", "lock", "unlock", "open", "close", "open_cover", "close_cover",
            "stop_cover", "set_options", "trigger", "enable", "disable", "set", "finish",
            "cancel", "change", "add_item", "remove_item", "reload_all", "set_location",
            "set_direction", "set_preset_mode", "set_swing_mode", "set_aux_heat",
            "set_speed", "set_position", "set_tilt_position", "send_message", "notify",
            "clear_playlist", "shuffle_set", "repeat_set", "join", "unjoin", "browse_media",
            "search_media", "play_pause", "media_play_pause", "apply",
        }
        fuentes: dict[str, str] = {}
        for e, st in self._estados.items():
            if e.startswith("automation."):
                aid = (st.get("attributes") or {}).get("id")
                if aid:
                    fuentes[e] = f"automation/config/{aid}"
            elif e.startswith("script."):
                fuentes[e] = f"script/config/{e[7:]}"
        faltan: dict[str, list[str]] = {}
        caidas: dict[str, list[str]] = {}
        for origen, camino in fuentes.items():
            try:
                cfg = await self.config_ha(camino)
            except Exception:  # noqa: BLE001
                continue
            texto = json.dumps(cfg, ensure_ascii=False)
            refs = {m.group(0) for m in patron.finditer(texto) if m.group(2) not in verbos}
            f = sorted(r for r in refs if r not in self._estados)
            c = sorted(
                r for r in refs
                if r in self._estados and self._estados[r].get("state") == "unavailable" and r not in ignorar
            )
            if f:
                faltan[origen] = f
            if c:
                caidas[origen] = c
        return {"faltan": faltan, "caidas": caidas}

    async def reemplazar_entidad(self, vieja: str, nueva: str) -> list[str]:
        """Cambia una entidad por otra en todas las automatizaciones y scripts."""
        cambiadas: list[str] = []
        for e, st in list(self._estados.items()):
            if e.startswith("automation."):
                aid = (st.get("attributes") or {}).get("id")
                if not aid:
                    continue
                camino = f"automation/config/{aid}"
            elif e.startswith("script."):
                camino = f"script/config/{e[7:]}"
            else:
                continue
            try:
                cfg = await self.config_ha(camino)
            except Exception:  # noqa: BLE001
                continue
            texto = json.dumps(cfg, ensure_ascii=False)
            if vieja not in texto:
                continue
            await self.config_ha(camino, "POST", json.loads(texto.replace(vieja, nueva)))
            cambiadas.append(e)
        return cambiadas

    # ----------------------------------------------------------- websocket

    def al_cambiar(self, cb: Callable[[dict], Awaitable[None]]) -> None:
        self._oyentes.append(cb)

    async def esperar_listo(self) -> None:
        await self._listo.wait()

    async def conectar(self) -> None:
        """Mantiene la conexion viva. Si se corta, reconecta sola."""
        espera = 1
        while True:
            try:
                await self._sesion_ws()
                espera = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("websocket caido (%s); reintento en %ss", e, espera)
                self._listo.clear()
                await asyncio.sleep(espera)
                espera = min(espera * 2, 60)

    async def _sesion_ws(self) -> None:
        async with self._sesion.ws_connect(BASE_WS, heartbeat=30) as ws:
            self._ws = ws
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": TOKEN})
            rta = await ws.receive_json()
            if rta.get("type") != "auth_ok":
                raise RuntimeError(f"HA rechazo la autenticacion: {rta}")

            log.info("conectado a Home Assistant")
            await self._cargar_estados(ws)
            await self._enviar(ws, {"type": "subscribe_events", "event_type": "state_changed"})
            self._listo.set()

            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                await self._procesar(json.loads(msg.data))

        self._ws = None
        self._listo.clear()
        raise ConnectionError("la conexion con HA se cerro")

    async def _cargar_estados(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        ident = await self._enviar(ws, {"type": "get_states"})
        while True:
            m = json.loads((await ws.receive()).data)
            if m.get("id") == ident and m.get("type") == "result":
                for st in m.get("result", []):
                    self._estados[st["entity_id"]] = st
                log.info("%s entidades cargadas", len(self._estados))
                return
            await self._procesar(m)

    async def _enviar(self, ws: aiohttp.ClientWebSocketResponse, payload: dict) -> int:
        self._id += 1
        payload["id"] = self._id
        await ws.send_json(payload)
        return self._id

    async def _procesar(self, m: dict) -> None:
        if m.get("type") != "event":
            return
        ev = m.get("event", {})
        if ev.get("event_type") != "state_changed":
            return
        datos = ev.get("data", {})
        nuevo = datos.get("new_state")
        entidad = datos.get("entity_id")
        if not entidad:
            return
        if nuevo is None:
            self._estados.pop(entidad, None)
        else:
            self._estados[entidad] = nuevo
        for cb in self._oyentes:
            try:
                await cb(datos)
            except Exception:  # noqa: BLE001
                log.exception("un oyente de cambios fallo")
