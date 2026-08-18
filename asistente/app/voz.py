"""La voz del asistente.

Una sola cola: el agente puede tardar y terminar varias cosas juntas, y no
queremos que se pisen dos avisos en el parlante.

REGLA INVIOLABLE: entre las 00:00 y las 11:00 no suena NADA. Ni alarmas, ni
urgencias, ni un aviso corto. Ariel fue explicito: si suena una bocina en ese
horario apaga todo el sistema. Lo que caiga en esa franja se guarda y se
cuenta despues de las 11, no se pierde, pero no se dice.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from hogar import Hogar

log = logging.getLogger("voz")

# Franja prohibida, hora local. De 00:00 (inclusive) a 11:00 (exclusive).
SILENCIO_DESDE = 0
SILENCIO_HASTA = 11


def es_horario_de_silencio(ahora: datetime | None = None) -> bool:
    h = (ahora or datetime.now()).hour
    return SILENCIO_DESDE <= h < SILENCIO_HASTA


class Voz:
    def __init__(self, hogar: Hogar, opciones: dict) -> None:
        self.hogar = hogar
        self.o = opciones
        self._cola: asyncio.Queue[tuple[str, bool]] = asyncio.Queue()
        self._dicho: list[tuple[float, str]] = []
        # Lo que no se pudo decir por el horario. Queda para contarlo despues.
        self.callado: list[tuple[float, str]] = []

    async def decir(self, texto: str, *, proactivo: bool = True) -> None:
        """proactivo=True es algo que el asistente dice por su cuenta: eso
        calla de 00 a 11. proactivo=False es la respuesta a algo que pidio
        Ariel, y eso suena siempre: si el lo pidio, quiere escucharlo."""
        texto = " ".join(texto.split())
        if not texto:
            return
        if proactivo and es_horario_de_silencio():
            self.callado.append((time.time(), texto))
            del self.callado[:-100]
            log.info("horario de silencio: NO se dice, queda guardado -> %s", texto[:90])
            return
        await self._cola.put((texto, proactivo))

    def historial(self, cuantos: int = 30) -> list[tuple[float, str]]:
        return self._dicho[-cuantos:]

    def tomar_callado(self) -> list[tuple[float, str]]:
        """Devuelve lo silenciado por horario y lo vacia: es para el parte."""
        pendiente = list(self.callado)
        self.callado.clear()
        return pendiente

    async def correr(self) -> None:
        while True:
            texto, proactivo = await self._cola.get()
            # Segunda barrera, solo para lo proactivo: un aviso puede entrar a
            # la cola a las 23:59 y salir a las 00:01.
            if proactivo and es_horario_de_silencio():
                self.callado.append((time.time(), texto))
                del self.callado[:-100]
                log.info("horario de silencio en la cola: NO se dice -> %s", texto[:90])
                self._cola.task_done()
                continue
            try:
                parlante = self.o.get("parlante", "media_player.dormitorio")
                # La bocina APAGADA descarta el audio SIN error (trampa
                # conocida): despertarla y darle aire para que arranque el
                # receptor. En "idle" ya esta despierta: ahi NO se la toca,
                # el turn_on mete su "bloop" y se come el mensaje (18/8).
                estado = (self.hogar.estado(parlante) or {}).get("state")
                if estado in (None, "off", "standby", "unknown", "unavailable"):
                    await self.hogar.llamar_servicio(
                        "media_player", "turn_on", {"entity_id": parlante}
                    )
                    # El receptor tarda en estar listo de verdad: con menos
                    # de ~5 s el primer mensaje se pierde (probado el 18/8).
                    await asyncio.sleep(6)
                await self.hogar.llamar_servicio(
                    "tts",
                    "speak",
                    {
                        "entity_id": self.o.get("voz", "tts.piper"),
                        "media_player_entity_id": parlante,
                        "message": texto,
                        "cache": True,
                    },
                )
                self._dicho.append((time.time(), texto))
                log.info("dicho: %s", texto[:120])
                # Le damos aire al parlante: ~14 caracteres por segundo, con techo.
                await asyncio.sleep(min(2 + len(texto) / 14, 25))
            except Exception:  # noqa: BLE001
                log.exception("no pude hablar por el parlante")
            finally:
                self._cola.task_done()
