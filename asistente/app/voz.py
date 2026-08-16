"""La voz del asistente.

Una sola cola: el agente puede tardar y terminar varias cosas juntas, y no
queremos que se pisen dos avisos en el parlante.
"""

from __future__ import annotations

import asyncio
import logging
import time

from hogar import Hogar

log = logging.getLogger("voz")


class Voz:
    def __init__(self, hogar: Hogar, opciones: dict) -> None:
        self.hogar = hogar
        self.o = opciones
        self._cola: asyncio.Queue[str] = asyncio.Queue()
        self._dicho: list[tuple[float, str]] = []

    async def decir(self, texto: str) -> None:
        texto = " ".join(texto.split())
        if texto:
            await self._cola.put(texto)

    def historial(self, cuantos: int = 30) -> list[tuple[float, str]]:
        return self._dicho[-cuantos:]

    async def correr(self) -> None:
        while True:
            texto = await self._cola.get()
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
