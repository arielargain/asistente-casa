"""Arranque del complemento.

Junta las piezas: escucha a Home Assistant, vigila, razona y habla.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from aiohttp import web

from agente import Agente
from hogar import Hogar
from vigilante import Vigilante
from voz import Voz, es_horario_de_silencio

# El SDK nuevo exige objetos PermissionResult en can_use_tool; el diccionario
# de antes lo toma como invalido y BLOQUEA el tool con "error de permisos"
# aunque el permiso diga que si (nos paso el 18/8). Con SDK viejo, caemos al
# formato de diccionario.
try:
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    def PERMITIR(entrada: dict):
        return PermissionResultAllow(updated_input=entrada)

    def NEGAR(mensaje: str):
        return PermissionResultDeny(message=mensaje)
except ImportError:
    def PERMITIR(entrada: dict):
        return {"behavior": "allow", "updatedInput": entrada}

    def NEGAR(mensaje: str):
        return {"behavior": "deny", "message": mensaje}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s"
)
log = logging.getLogger("asistente")

OPCIONES = Path("/data/options.json")
TRABAJO = Path("/share/asistente/trabajo")
# Encargos que todavia no terminaron. Sobrevive a los reinicios del
# complemento: al arrancar se retoman, en vez de morir en silencio.
PENDIENTES = Path("/share/asistente/encargos_pendientes.json")

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

# En el horario de no molestar (00-11) el agente tiene via libre para actuar
# sobre estas zonas sin pedir el dale: el indoor, las camaras y el dormitorio.
# Ariel lo pidio explicito: de noche resuelve solo, de dia pide confirmacion.
# La regla de silencio sigue por encima: via libre para ACTUAR, no para sonar.
ZONA_LIBRE_DE_NOCHE = (
    "indoor", "cultivo", "riego", "carpa", "humidificador", "humedad",
    "ventilador", "camara", "camera", "frigate", "dormitorio",
)

# Dominios que hacen ruido o luz fuerte: nunca se liberan solos de noche.
DOMINIOS_QUE_SUENAN = {"tts", "media_player", "notify", "siren"}


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
            return PERMITIR(entrada)

        if herramienta == "Bash":
            orden = str(entrada.get("command", "")).strip()
            if orden.startswith(BASH_MIRON):
                return PERMITIR(entrada)

        libres_extra = set(self.o.get("permitir_sin_preguntar") or [])
        if herramienta == "mcp__casa__reiniciar_complemento" and "reiniciar_complemento" in libres_extra:
            return PERMITIR(entrada)
        if herramienta == "mcp__casa__recargar_integracion" and "recargar_integracion" in libres_extra:
            return PERMITIR(entrada)
        if herramienta == "mcp__casa__reiniciar_por_poe" and "reiniciar_por_poe" in libres_extra:
            return PERMITIR(entrada)
        if herramienta == "mcp__casa__listar_integraciones":
            return PERMITIR(entrada)

        # Libertad nocturna: de 00 a 11 Ariel duerme y el agente resuelve solo
        # lo del indoor, las camaras y el dormitorio. De dia, esto pide el dale.
        if es_horario_de_silencio():
            zona = tuple(
                str(z).lower()
                for z in (self.o.get("zona_libre_de_noche") or ZONA_LIBRE_DE_NOCHE)
            )
            if herramienta == "mcp__casa__reiniciar_por_poe":
                log.info("libertad nocturna: reinicio por PoE %s", entrada)
                return PERMITIR(entrada)
            if herramienta == "mcp__casa__ejecutar_en_la_casa":
                dominio = str(entrada.get("dominio", "")).lower()
                datos = entrada.get("datos") or {}
                ids = datos.get("entity_id") or (datos.get("target") or {}).get("entity_id") or []
                if isinstance(ids, str):
                    ids = [ids]
                ids = [str(i).lower() for i in ids]
                if (
                    dominio not in DOMINIOS_QUE_SUENAN
                    and ids
                    and all(any(z in e for z in zona) for e in ids)
                ):
                    log.info("libertad nocturna sobre %s", ids)
                    return PERMITIR(entrada)

        if time.time() < self.permiso_hasta:
            log.info("permitido por autorizacion de Ariel: %s", herramienta)
            return PERMITIR(entrada)

        return NEGAR(
            "Esta accion cambia algo y todavia no esta autorizada. Contale a Ariel "
            "en una frase que queres hacer y pedile que te diga 'dale' para hacerlo."
        )

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

    # ---------------------------------------------------- parte diario 11:30

    async def parte_diario(self) -> None:
        """Una vez al dia, despues del silencio nocturno, cuenta lo que paso.

        Es la pieza "informar" del guardian: lo que se callo de noche, los
        incidentes del dia, el autochequeo del propio sistema, el consumo y
        la agenda. Todo en un solo mensaje hablado, corto.
        """
        assert self.hogar
        await self.hogar.esperar_listo()
        while True:
            ahora = datetime.now()
            hhmm = str(self.o.get("hora_parte", "11:30"))
            try:
                h, m = [int(x) for x in hhmm.split(":")]
            except ValueError:
                h, m = 11, 30
            # jamas antes de las 11: es la regla dura de silencio
            if h < 11:
                h, m = 11, 30
            objetivo = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
            if ahora >= objetivo:
                objetivo += timedelta(days=1)
            await asyncio.sleep((objetivo - ahora).total_seconds())
            try:
                await self._dar_parte()
            except Exception:  # noqa: BLE001
                log.exception("el parte diario fallo")

    async def _autochequeo(self) -> list[str]:
        """El guardian se toma el pulso. Devuelve solo lo que esta MAL."""
        assert self.hogar
        fallas: list[str] = []
        est = self.hogar.estado("assist_satellite.panel_de_voz_satelite_assist")
        if not est or est.get("state") in ("unavailable", "unknown"):
            fallas.append("el panel de voz no responde")
        parl = self.hogar.estado(self.o.get("parlante", "media_player.dormitorio"))
        if not parl or parl.get("state") == "unavailable":
            fallas.append("el parlante del dormitorio no responde")
        caidas = [
            e for e in self.hogar.caidas(set(self.o.get("entidades_ignoradas") or []))
            if e.startswith("camera.")
        ]
        if len(caidas) >= 3:
            fallas.append(f"hay {len(caidas)} camaras sin responder")
        try:
            r = await self.hogar.supervisor("/backups")
            lista = (r.get("data") or r).get("backups") or []
            fechas = sorted(b.get("date", "") for b in lista)
            if not fechas:
                fallas.append("no hay ningun backup de Home Assistant")
            else:
                ultimo = fechas[-1][:10]
                dias = (datetime.now() - datetime.fromisoformat(ultimo)).days
                if dias > 3:
                    fallas.append(f"el ultimo backup tiene {dias} dias")
        except Exception:  # noqa: BLE001
            fallas.append("no pude verificar los backups")
        return fallas

    async def _dar_parte(self) -> None:
        assert self.agente and self.voz
        fallas = await self._autochequeo()
        callado = self.voz.tomar_callado()
        hace24 = time.time() - 24 * 3600
        incidentes = [
            b for b in self.bitacora
            if b["cuando"] > hace24 and b["tipo"] != "encargo" and b.get("texto")
        ]
        partes: list[str] = []
        if fallas:
            partes.append("FALLAS DEL AUTOCHEQUEO (decilas primero): " + "; ".join(fallas))
        if callado:
            avisos = " | ".join(x[1][:120] for x in callado[-8:])
            partes.append(f"AVISOS SILENCIADOS DE ANOCHE ({len(callado)}): {avisos}")
        if incidentes:
            resumen = " | ".join(f"{b['tipo']}: {b['texto'][:100]}" for b in incidentes[-6:])
            partes.append(f"INCIDENTES DE LAS ULTIMAS 24H: {resumen}")
        contexto = " || ".join(partes) if partes else "Sin fallas, sin avisos silenciados y sin incidentes."
        pedido = (
            "Es el parte diario de la casa para Ariel. Datos crudos: " + contexto +
            " ||| Ademas consulta el consumo de hoy y si hay algo en la agenda "
            "(calendar.mi_agenda) o pendientes urgentes. "
            "Armalo en CUATRO A SEIS frases habladas, sin listas: primero las "
            "fallas si las hay, despues lo silenciado o incidentes (resumido, no "
            "uno por uno), despues consumo y agenda. Si esta todo bien, decilo "
            "en una frase y no rellenes. Empeza con 'Buen dia' o parecido."
        )
        frase = await self.agente.encargo(pedido)
        self.anotar("parte", [], frase)
        if frase:
            await self.voz.decir(frase, proactivo=True)

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

    def _leer_pendientes(self) -> list[str]:
        try:
            lista = json.loads(PENDIENTES.read_text(encoding="utf-8"))
            return [str(x) for x in lista] if isinstance(lista, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _guardar_pendientes(self, lista: list[str]) -> None:
        try:
            PENDIENTES.parent.mkdir(parents=True, exist_ok=True)
            PENDIENTES.write_text(json.dumps(lista, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.exception("no pude guardar los encargos pendientes")

    async def _trabajar(self, texto: str, *, proactivo: bool = False) -> None:
        """Un encargo SIEMPRE termina con una frase: la respuesta, el error o
        el aviso de que no hubo nada. El silencio ya nos costo tres encargos."""
        assert self.agente and self.voz
        # El encargo de Ariel ES la autorizacion: no tiene sentido que el
        # agente le pida el dale por algo que el mismo acaba de ordenar.
        # (Aprendido el 18/8: tres encargos frenados por permisos.)
        self.autorizar(15)
        pend = self._leer_pendientes()
        if texto not in pend:
            self._guardar_pendientes(pend + [texto])
        try:
            frase = await asyncio.wait_for(self.agente.encargo(texto), timeout=15 * 60)
        except asyncio.TimeoutError:
            log.error("encargo cortado a los 15 minutos: %s", texto[:120])
            frase = (
                "El encargo se paso de los quince minutos y lo corte. "
                "Pedimelo de nuevo, mas acotado."
            )
        except Exception as e:  # noqa: BLE001
            log.exception("el encargo fallo")
            frase = f"No pude terminar el encargo. {e}"
        if not frase.strip():
            log.warning("el encargo termino sin texto: %s", texto[:120])
            frase = (
                "Termine el encargo pero no me quedo ninguna respuesta para "
                "decirte. Si esperabas algo, pedimelo de nuevo."
            )
        self._guardar_pendientes([p for p in self._leer_pendientes() if p != texto])
        self.anotar("encargo", [], frase)
        # Rastro escrito SIEMPRE: si la bocina se traga el audio, el resultado
        # queda en la campanita de HA igual.
        try:
            assert self.hogar
            await self.hogar.llamar_servicio(
                "persistent_notification", "create",
                {"title": "Encargo terminado", "message": frase[:900]},
            )
        except Exception:  # noqa: BLE001
            log.exception("no pude dejar la notificacion del encargo")
        # proactivo=False: lo pidio Ariel y suena aunque sea de madrugada.
        # proactivo=True: es un encargo retomado tras un reinicio; si es de
        # noche va callado al parte de las 11:30, la regla de silencio manda.
        await self.voz.decir(frase, proactivo=proactivo)

    async def reanudar_encargos(self) -> None:
        """Al arrancar, retomar lo que un reinicio dejo a medias."""
        await asyncio.sleep(20)
        pend = self._leer_pendientes()
        if not pend:
            return
        log.info("retomando %d encargo(s) que quedaron a medias", len(pend))
        assert self.voz
        aviso = (
            f"Me reiniciaron con {len(pend)} encargo{'s' if len(pend) > 1 else ''} "
            "a medias. Lo retomo ahora." if len(pend) == 1 else
            f"Me reiniciaron con {len(pend)} encargos a medias. Los retomo ahora."
        )
        await self.voz.decir(aviso, proactivo=True)
        for texto in pend:
            await self._trabajar(texto, proactivo=True)

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
            self.parte_diario(),
            self.reanudar_encargos(),
            self.voz.correr(),
            self.rondas(),
            self.servidor(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(Asistente(cargar_opciones()).correr())
    except KeyboardInterrupt:
        pass
