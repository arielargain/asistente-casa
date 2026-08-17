"""Arranque del complemento.

Junta las piezas: escucha a Home Assistant, vigila, razona y habla.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from agente import Agente
from hogar import Hogar
from vigilante import Vigilante
from voz import Voz

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s"
)
log = logging.getLogger("asistente")

OPCIONES = Path("/data/options.json")
TRABAJO = Path("/share/asistente/trabajo")

# Herramientas que puede usar sin consultar. El resto se le niega y el agente
# tiene que pedirselo a Ariel en voz alta antes de volver a intentarlo.
LIBRES = {
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "mcp__casa__estado_de_la_casa",
    "mcp__casa__que_esta_caido",
    "mcp__casa__registro_de_home_assistant",
    "mcp__casa__complementos",
    "mcp__casa__buscar_en_mis_notas",
}

# Ordenes de consola que son de solo mirar.
BASH_MIRON = ("ls", "cat", "head", "tail", "grep", "find", "git status", "git log", "git diff", "ps", "df", "ping", "curl -s")


def cargar_opciones() -> dict:
    if OPCIONES.exists():
        return json.loads(OPCIONES.read_text(encoding="utf-8"))
    return {}


class Asistente:
    def __init__(self, opciones: dict) -> None:
        self.o = opciones
        self.sesion: aiohttp.ClientSession | None = None
        self.hogar: Hogar | None = None
        self.voz: Voz | None = None
        self.vigilante: Vigilante | None = None
        self.agente: Agente | None = None
        self.bitacora: list[dict] = []
        # Cuando Ariel dice "dale", se abre la puerta un rato.
        self.permiso_hasta: float = 0

    # -------------------------------------------------------- permisos

    async def permiso(self, herramienta: str, entrada: dict, _ctx) -> dict:
        if herramienta in LIBRES:
            return {"behavior": "allow", "updatedInput": entrada}

        if herramienta == "Bash":
            orden = str(entrada.get("command", "")).strip()
            if orden.startswith(BASH_MIRON):
                return {"behavior": "allow", "updatedInput": entrada}

        libres_extra = set(self.o.get("permitir_sin_preguntar") or [])
        if herramienta == "mcp__casa__reiniciar_complemento" and "reiniciar_complemento" in libres_extra:
            return {"behavior": "allow", "updatedInput": entrada}

        if time.time() < self.permiso_hasta:
            log.info("permitido por autorizacion de Ariel: %s", herramienta)
            return {"behavior": "allow", "updatedInput": entrada}

        return {
            "behavior": "deny",
            "message": (
                "Esta accion cambia algo y todavia no esta autorizada. Contale a Ariel "
                "en una frase que queres hacer y pedile que te diga 'dale' para hacerlo."
            ),
        }

    def autorizar(self, minutos: int = 10) -> None:
        self.permiso_hasta = time.time() + minutos * 60

    # ------------------------------------------------------- vigilancia

    async def rondas(self) -> None:
        assert self.vigilante and self.agente and self.voz and self.hogar
        await self.hogar.esperar_listo()
        self.vigilante.tomar_linea_base()
        while True:
            try:
                for incidente in self.vigilante.revisar():
                    log.info("incidente %s en %s", incidente.tipo, incidente.entidades)
                    frase = await self.agente.analizar(incidente)
                    self.anotar(incidente.tipo, incidente.entidades, frase)
                    if frase and "SILENCIO" not in frase.upper():
                        await self.voz.decir(frase)
                    else:
                        log.info("el agente decidio no hablar")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("la ronda de vigilancia fallo")
            await asyncio.sleep(60)

    # ------------------------------------------------------- encargos por voz

    # Ariel le habla al asistente rapido, ese escribe el pedido en este
    # ayudante de Home Assistant, y el agente de fondo lo levanta desde aca.
    # Se hace asi y no por HTTP porque el complemento solo se alcanza por
    # ingress, y montar una llamada REST pedia editar configuration.yaml.
    BUZON = "input_text.encargo_al_agente"

    async def al_cambiar_encargo(self, datos: dict) -> None:
        if datos.get("entity_id") != self.BUZON:
            return
        nuevo = (datos.get("new_state") or {}).get("state", "")
        texto = str(nuevo).strip()
        if not texto or texto in ("unknown", "unavailable"):
            return
        log.info("encargo por voz: %s", texto[:120])
        # Se vacia enseguida para que el proximo pedido igual al anterior
        # tambien dispare un cambio de estado.
        assert self.hogar
        await self.hogar.llamar_servicio(
            "input_text", "set_value", {"entity_id": self.BUZON, "value": ""}
        )
        asyncio.create_task(self._trabajar(texto))

    # ------------------------------------------- destrabar lo que pide Ariel

    async def soltar_candados(self) -> None:
        """Le saca el candado horario a los scripts que Ariel dispara el mismo.

        El silencio de 00 a 11 es para lo que suena SOLO. Si Ariel pide un
        reporte a las tres de la maniana, tiene que sonar: lo pidio el. Las
        automatizaciones, que si se disparan solas, conservan el candado.
        """
        assert self.hogar
        await asyncio.sleep(15)
        try:
            estados = self.hogar.todos()
            scripts = [e for e in estados if e.startswith("script.")]
            sueltos = 0
            for ent in scripts:
                nombre = ent.split(".", 1)[1]
                try:
                    cfg = await self.hogar.config_ha(f"script/config/{nombre}")
                except Exception:  # noqa: BLE001
                    continue
                sec = cfg.get("sequence") or []
                if not sec:
                    continue
                primero = sec[0] if isinstance(sec[0], dict) else {}
                if (
                    primero.get("condition") == "time"
                    and primero.get("after") == "11:00:00"
                    and primero.get("before") == "00:00:00"
                ):
                    cfg["sequence"] = sec[1:]
                    await self.hogar.config_ha(f"script/config/{nombre}", "POST", cfg)
                    sueltos += 1
            if sueltos:
                log.info("candado horario sacado de %s scripts que dispara Ariel", sueltos)
                await self.hogar.llamar_servicio("script", "reload", {})
        except Exception:  # noqa: BLE001
            log.exception("no pude soltar los candados")

    def anotar(self, tipo: str, entidades: list[str], texto: str) -> None:
        self.bitacora.append(
            {"cuando": time.time(), "tipo": tipo, "entidades": entidades, "texto": texto}
        )
        del self.bitacora[:-200]

    # ------------------------------------------------------------ web

    async def servidor(self) -> None:
        app = web.Application()
        app.router.add_post("/encargo", self.h_encargo)
        app.router.add_post("/autorizar", self.h_autorizar)
        app.router.add_get("/estado", self.h_estado)
        app.router.add_get("/", self.h_panel)
        corredor = web.AppRunner(app)
        await corredor.setup()
        await web.TCPSite(corredor, "0.0.0.0", 8099).start()
        log.info("servidor escuchando en 8099")
        await asyncio.Event().wait()

    async def h_encargo(self, pedido: web.Request) -> web.Response:
        cuerpo = await pedido.json()
        texto = str(cuerpo.get("texto", "")).strip()
        if not texto:
            return web.json_response({"respuesta": "No entendi el encargo."})
        if cuerpo.get("autorizado"):
            self.autorizar()

        # Trabaja en segundo plano y avisa por el parlante: los encargos
        # grandes no pueden dejar el microfono colgado.
        asyncio.create_task(self._trabajar(texto))
        return web.json_response({"respuesta": "Dale, me pongo con eso y te aviso."})

    async def _trabajar(self, texto: str) -> None:
        assert self.agente and self.voz
        try:
            frase = await self.agente.encargo(texto)
        except Exception as e:  # noqa: BLE001
            log.exception("el encargo fallo")
            frase = f"No pude terminar el encargo. {e}"
        self.anotar("encargo", [], frase)
        if frase:
            # Lo pidio Ariel: suena aunque sea de madrugada.
            await self.voz.decir(frase, proactivo=False)

    async def h_autorizar(self, _pedido: web.Request) -> web.Response:
        self.autorizar()
        return web.json_response({"respuesta": "Listo, autorizado por diez minutos."})

    async def h_estado(self, _pedido: web.Request) -> web.Response:
        assert self.vigilante
        datos = self.vigilante.panorama()
        datos["bitacora"] = self.bitacora[-25:]
        datos["autorizado"] = time.time() < self.permiso_hasta
        return web.json_response(datos)

    async def h_panel(self, _pedido: web.Request) -> web.Response:
        assert self.vigilante
        p = self.vigilante.panorama()

        def chips(lista: list[str], tope: int = 30) -> str:
            if not lista:
                return "<p class=ok>Nada.</p>"
            resto = f" <span class=mas>y {len(lista) - tope} mas</span>" if len(lista) > tope else ""
            return "<p>" + " ".join(f"<code>{c}</code>" for c in lista[:tope]) + resto + "</p>"

        filas = "".join(
            f"<tr><td>{time.strftime('%H:%M', time.localtime(b['cuando']))}</td>"
            f"<td>{b['tipo']}</td><td>{b['texto'][:400]}</td></tr>"
            for b in reversed(self.bitacora[-40:])
        )
        html = f"""<!doctype html><meta charset=utf-8>
<title>Asistente</title>
<style>
 body{{font:15px/1.5 system-ui;margin:24px;max-width:900px}}
 h1{{font-size:21px;margin-bottom:4px}} h2{{font-size:16px;margin-top:28px}}
 td{{padding:6px 10px;border-bottom:1px solid #8883;vertical-align:top}}
 code{{background:#8882;padding:1px 6px;border-radius:4px;font-size:13px}}
 .ok{{opacity:.6}} .mas{{opacity:.6;font-size:13px}}
 .sub{{opacity:.7;margin-top:0}}
</style>
<h1>Asistente de Ariel</h1>
<p class=sub>{p['vigiladas']} entidades vigiladas.
Se cuenta solo <code>unavailable</code>: <code>unknown</code> es el estado normal
de los botones, los emisores y los motores de voz, no una caida.</p>

<h2>Caidas nuevas ({len(p['caidas_nuevas'])})</h2>
{chips(p['caidas_nuevas'])}

<h2>En observacion ({len(p['en_observacion'])})</h2>
<p class=sub>Se cayeron recien; todavia no cumplieron la espera para avisar.</p>
{chips(p['en_observacion'])}

<h2>Ya venian caidas ({len(p['caidas_de_antes'])})</h2>
<p class=sub>Estaban asi cuando arranque. No se avisan, pero aca estan.</p>
{chips(p['caidas_de_antes'])}

<h2>Bitacora</h2><table>{filas or "<tr><td>Sin novedades.</td></tr>"}</table>"""
        return web.Response(text=html, content_type="text/html")

    # ---------------------------------------------------------- arranque

    async def correr(self) -> None:
        TRABAJO.mkdir(parents=True, exist_ok=True)
        if self.o.get("clave_anthropic"):
            os.environ["ANTHROPIC_API_KEY"] = self.o["clave_anthropic"]
        else:
            log.error("falta la clave de Anthropic en la configuracion del complemento")

        self.sesion = aiohttp.ClientSession()
        self.hogar = Hogar(self.sesion)
        self.voz = Voz(self.hogar, self.o)
        self.vigilante = Vigilante(self.hogar, self.o)
        self.agente = Agente(self.hogar, self.o, self.permiso)
        self.hogar.al_cambiar(self.vigilante.al_cambiar)
        self.hogar.al_cambiar(self.al_cambiar_encargo)

        await asyncio.gather(
            self.hogar.conectar(),
            self.soltar_candados(),
            self.voz.correr(),
            self.rondas(),
            self.servidor(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(Asistente(cargar_opciones()).correr())
    except KeyboardInterrupt:
        pass
