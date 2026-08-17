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
        self._cola: asyncio.Queue[str] = asyncio.Queue()
        self._dicho: list[tuple[float, str]] = []
        # Lo que no se pudo decir por el horario. Queda para contarlo despues.
        self.callado: list[tuple[float, str]] = []

    async def decir(self, texto: str) -> None:
        texto = " ".join(texto.split())
        if not texto:
            return
        if es_horario_de_silencio():
            self.callado.append((time.time(), texto))
            del self.callado[:-100]
            log.info("horario de silencio: NO se dice, queda guardado -> %s", texto[:90])
            return
        await self._cola.put(texto)

    def historial(self, cuantos: int = 30) -> list[tuple[float, str]]:
        return self._dicho[-cuantos:]

    async def correr(self) -> None:
        while True:
            texto = await self._cola.get()
            # Segunda barrera. Un aviso puede haber entrado a la cola a las
            # 23:59 y salir a las 00:01: aca se lo frena igual.
            if es_horario_de_silencio():
                self.callado.append((time.time(), texto))
                del self.callado[:-100]
                log.info("horario de silencio en la cola: NO se dice -> %s", texto[:90])
                self._cola.task_done()
                continue
            try:
                await self.hogar.llamar_servicio(
                    "tts",
                    "speak",
                    {
                        "entity_id": self.o.get("voz", "tts.piper"),
                        "media_player_entity_id": self.o.get("parlante", "media_player.dormitorio"),
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
