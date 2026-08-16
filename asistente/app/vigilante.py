"""Vigilante de la casa.

Reglas de codigo, sin modelo. Detecta candidatos a problema y recien ahi
despierta al agente. Esto es a proposito: si el modelo mirara cada cambio de
estado, con 700 entidades la cuenta seria una locura y ademas hablaria todo
el tiempo. Aca se filtra barato, y el agente solo entra cuando hay algo real.

Tres cosas cuidadas para no llorar lobo:
  1. Espera. Un enchufe que parpadea 30 segundos no es una caida.
  2. Agrupa. Si se cae el WiFi y con el 12 entidades, es UN aviso, no 12.
  3. Silencio. Cada entidad tiene un tiempo minimo entre avisos.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from hogar import Hogar

log = logging.getLogger("vigilante")

SIN_RESPUESTA = ("unavailable", "unknown")

# Al arrancar HA todo aparece caido unos segundos. No avisamos nada
# hasta que pase esta gracia.
GRACIA_ARRANQUE = 180


@dataclass
class Incidente:
    """Un problema listo para contarle a Ariel."""

    tipo: str  # caida | recuperacion | bateria | energia
    critico: bool
    entidades: list[str]
    detalle: str = ""
    desde: float = field(default_factory=time.time)

    def clave(self) -> str:
        return f"{self.tipo}:{','.join(sorted(self.entidades))}"


class Vigilante:
    def __init__(self, hogar: Hogar, opciones: dict) -> None:
        self.hogar = hogar
        self.o = opciones
        self.arranque = time.time()

        # entidad -> momento en que se cayo
        self._caidas: dict[str, float] = {}
        # entidad -> momento del ultimo aviso
        self._avisadas: dict[str, float] = {}
        # entidades ya anunciadas como caidas, para poder avisar la vuelta
        self._anunciadas: set[str] = set()

        self._ignoradas = set(opciones.get("entidades_ignoradas") or [])
        self._criticas = set(opciones.get("entidades_criticas") or [])

    # ------------------------------------------------------------ ajustes

    @property
    def _espera(self) -> float:
        return float(self.o.get("minutos_para_avisar", 5)) * 60

    @property
    def _silencio(self) -> float:
        return float(self.o.get("minutos_entre_avisos", 120)) * 60

    def _interesa(self, entidad: str) -> bool:
        if entidad in self._ignoradas:
            return False
        # Los dominios que solo reflejan otra cosa no aportan nada como alerta.
        dominio = entidad.split(".", 1)[0]
        return dominio not in ("automation", "script", "scene", "person", "zone", "sun", "tts")

    # ------------------------------------------------------------ entrada

    async def al_cambiar(self, datos: dict) -> None:
        """Se llama con cada cambio de estado. Tiene que ser barato."""
        entidad = datos.get("entity_id", "")
        nuevo = datos.get("new_state") or {}
        if not self._interesa(entidad):
            return

        estado = nuevo.get("state")
        if estado in SIN_RESPUESTA:
            self._caidas.setdefault(entidad, time.time())
        elif entidad in self._caidas:
            del self._caidas[entidad]

    # -------------------------------------------------------------- ronda

    def revisar(self) -> list[Incidente]:
        """Se llama cada tanto. Devuelve lo que amerita contarle a Ariel."""
        if time.time() - self.arranque < GRACIA_ARRANQUE:
            return []

        incidentes: list[Incidente] = []
        incidentes += self._revisar_caidas()
        incidentes += self._revisar_recuperaciones()
        incidentes += self._revisar_bateria()
        incidentes += self._revisar_energia()
        return incidentes

    def _revisar_caidas(self) -> list[Incidente]:
        ahora = time.time()
        maduras: list[str] = []
        criticas: list[str] = []

        for entidad, desde in self._caidas.items():
            if entidad in self._anunciadas:
                continue
            es_critica = entidad in self._criticas
            espera = 30 if es_critica else self._espera
            if ahora - desde < espera:
                continue
            ultimo = self._avisadas.get(entidad, 0)
            if ahora - ultimo < self._silencio:
                continue
            (criticas if es_critica else maduras).append(entidad)

        salida: list[Incidente] = []
        # Las criticas van sueltas: cada una merece su propio aviso.
        for entidad in criticas:
            self._avisadas[entidad] = ahora
            self._anunciadas.add(entidad)
            salida.append(Incidente("caida", True, [entidad]))

        # El resto se agrupa en un solo aviso.
        if maduras:
            for entidad in maduras:
                self._avisadas[entidad] = ahora
                self._anunciadas.add(entidad)
            salida.append(Incidente("caida", False, sorted(maduras)))

        return salida

    def _revisar_recuperaciones(self) -> list[Incidente]:
        vueltas = [e for e in self._anunciadas if e not in self._caidas]
        if not vueltas:
            return []
        for e in vueltas:
            self._anunciadas.discard(e)
            self._avisadas.pop(e, None)
        return [Incidente("recuperacion", False, sorted(vueltas))]

    def _revisar_bateria(self) -> list[Incidente]:
        ahora = time.time()
        flacas: list[str] = []
        for entidad, st in self.hogar.todos().items():
            attrs = st.get("attributes") or {}
            if attrs.get("device_class") != "battery":
                continue
            if entidad in self._ignoradas:
                continue
            try:
                nivel = float(st.get("state"))
            except (TypeError, ValueError):
                continue
            if nivel > 15:
                continue
            if ahora - self._avisadas.get(entidad, 0) < 24 * 3600:
                continue
            self._avisadas[entidad] = ahora
            flacas.append(entidad)
        return [Incidente("bateria", False, sorted(flacas))] if flacas else []

    def _revisar_energia(self) -> list[Incidente]:
        """La UPS (add-on NUT). Que se corte la luz si es para avisar ya."""
        ahora = time.time()
        for entidad, st in self.hogar.todos().items():
            if "ups" not in entidad:
                continue
            estado = str(st.get("state") or "").lower()
            if estado not in ("on battery", "ob", "onbatt", "on_battery"):
                continue
            if ahora - self._avisadas.get(entidad, 0) < 900:
                continue
            self._avisadas[entidad] = ahora
            return [
                Incidente(
                    "energia",
                    True,
                    [entidad],
                    detalle="La UPS paso a bateria: se corto la luz.",
                )
            ]
        return []
