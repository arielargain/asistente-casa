"""El agente: Claude con herramientas de verdad.

Es el que razona. Lo despierta el vigilante cuando hay un incidente, o Ariel
cuando le encarga algo por voz. Tiene las herramientas nativas de la SDK
(leer, escribir y editar archivos, bash, buscar) mas las de la casa que se
definen aca abajo.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)

from cerebro import buscar_en_el_cerebro
from hogar import Hogar

log = logging.getLogger("agente")

# Lo que puede hacer sin preguntar. Todo lo demas se le consulta a Ariel por voz.
# Ver PERMISOS en main.py.
SIEMPRE_PERMITIDO = {"Read", "Glob", "Grep", "WebSearch", "WebFetch"}

PERSONALIDAD = """\
Sos el asistente de la casa de Ariel Argain, en Villaguay, Entre Rios.

Sobre Ariel: 30 anios, lesion medular C5 por un accidente. Escribir le cuesta
fisicamente, por eso te habla. Curso Abogacia y le faltan 10 materias. Esta
sin empleo y su prioridad numero uno es generar ingresos. Tiene InnovateIA
(plataforma con IA), un proyecto de casino y trabajos de publicidad.

Como hablas:
- Espanol argentino con voseo. Directo, sin vueltas.
- Tus respuestas se leen en voz alta por un parlante. Nada de listas, markdown,
  links ni tablas. Frases cortas, como si estuvieras al lado.
- Si algo es largo, dale el titular primero y ofrecele el detalle.
- No le propongas parar, descansar ni dejarlo para maniana. El decide cuando corta.

Como trabajas:
- Sos un asistente que ejecuta. No le tires listas de opciones para que elija:
  resolve vos y contale lo que hiciste.
- Antes de afirmar que algo esta roto, verificalo. Mira el estado, el historial
  y el registro.
- Si no podes resolver algo, decile que falta y por que, sin adornarlo.
- Cuando avisas de un problema, en la misma frase deci que vas a hacer o que
  necesitas de el.
"""


def herramientas_de_la_casa(hogar: Hogar, opciones: dict):
    """Las herramientas propias que le damos al agente ademas de las nativas."""

    @tool("estado_de_la_casa", "Devuelve el estado actual de una entidad de Home Assistant, o de todas las que coincidan con un texto", {"buscar": str})
    async def estado_de_la_casa(args: dict[str, Any]) -> dict:
        patron = str(args.get("buscar", "")).lower().strip()
        todos = hogar.todos()
        if patron in todos:
            elegidas = {patron: todos[patron]}
        else:
            elegidas = {e: s for e, s in todos.items() if patron in e.lower()}
        resumen = {
            e: {
                "estado": s.get("state"),
                "nombre": (s.get("attributes") or {}).get("friendly_name"),
                "cambio": s.get("last_changed"),
            }
            for e, s in list(elegidas.items())[:60]
        }
        return {"content": [{"type": "text", "text": json.dumps(resumen, ensure_ascii=False)}]}

    @tool("que_esta_caido", "Lista todas las entidades que ahora mismo no responden", {})
    async def que_esta_caido(_args: dict[str, Any]) -> dict:
        caidas = hogar.caidas(set(opciones.get("entidades_ignoradas") or []))
        detalle = [
            {"entidad": e, "nombre": (hogar.estado(e) or {}).get("attributes", {}).get("friendly_name")}
            for e in caidas
        ]
        return {"content": [{"type": "text", "text": json.dumps(detalle, ensure_ascii=False)}]}

    @tool("ejecutar_en_la_casa", "Llama un servicio de Home Assistant. Ejemplo: dominio 'light', servicio 'turn_on', datos {'entity_id': 'light.cocina'}", {"dominio": str, "servicio": str, "datos": dict})
    async def ejecutar_en_la_casa(args: dict[str, Any]) -> dict:
        r = await hogar.llamar_servicio(
            str(args["dominio"]), str(args["servicio"]), args.get("datos") or {}
        )
        return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False, default=str)[:2000]}]}

    @tool("registro_de_home_assistant", "Devuelve las ultimas lineas del log de HA, opcionalmente filtradas por un texto", {"filtro": str})
    async def registro_de_home_assistant(args: dict[str, Any]) -> dict:
        texto = await hogar.registro()
        filtro = str(args.get("filtro", "")).lower()
        lineas = [l for l in texto.splitlines() if not filtro or filtro in l.lower()]
        return {"content": [{"type": "text", "text": "\n".join(lineas[-80:])[:6000]}]}

    @tool("complementos", "Lista los complementos de Home Assistant y su estado", {})
    async def complementos(_args: dict[str, Any]) -> dict:
        r = await hogar.supervisor("/addons")
        lista = [
            {"slug": a.get("slug"), "nombre": a.get("name"), "estado": a.get("state")}
            for a in (r.get("data", {}) or r).get("addons", [])
        ]
        return {"content": [{"type": "text", "text": json.dumps(lista, ensure_ascii=False)}]}

    @tool("reiniciar_complemento", "Reinicia un complemento de HA por su slug", {"slug": str})
    async def reiniciar_complemento(args: dict[str, Any]) -> dict:
        r = await hogar.supervisor(f"/addons/{args['slug']}/restart", "POST")
        return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)[:800]}]}

    @tool("listar_integraciones", "Lista las integraciones de Home Assistant con su estado y entry_id, para diagnosticar o recargar", {})
    async def listar_integraciones(_args: dict[str, Any]) -> dict:
        r = await hogar.config_ha("config_entries/entry")
        lista = [
            {"entry_id": e.get("entry_id"), "dominio": e.get("domain"), "titulo": e.get("title"), "estado": e.get("state")}
            for e in (r if isinstance(r, list) else [])
        ]
        return {"content": [{"type": "text", "text": json.dumps(lista, ensure_ascii=False)[:6000]}]}

    @tool("recargar_integracion", "Recarga una integracion de Home Assistant por su entry_id (auto-reparacion: util cuando una integracion quedo colgada)", {"entry_id": str})
    async def recargar_integracion(args: dict[str, Any]) -> dict:
        r = await hogar.config_ha(f"config_entries/entry/{args['entry_id']}/reload", "POST")
        return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False, default=str)[:500]}]}

    @tool("buscar_en_mis_notas", "Busca por significado en el segundo cerebro de Ariel: sus proyectos, decisiones y documentacion de la casa", {"consulta": str})
    async def buscar_en_mis_notas(args: dict[str, Any]) -> dict:
        texto = await buscar_en_el_cerebro(str(args["consulta"]))
        return {"content": [{"type": "text", "text": texto}]}

    return create_sdk_mcp_server(
        name="casa",
        version="0.1.0",
        tools=[
            estado_de_la_casa,
            que_esta_caido,
            ejecutar_en_la_casa,
            registro_de_home_assistant,
            complementos,
            reiniciar_complemento,
            listar_integraciones,
            recargar_integracion,
            buscar_en_mis_notas,
        ],
    )


class Agente:
    def __init__(self, hogar: Hogar, opciones: dict, pedir_permiso) -> None:
        self.hogar = hogar
        self.o = opciones
        self._servidor = herramientas_de_la_casa(hogar, opciones)
        self._pedir_permiso = pedir_permiso

    def _opciones(self, extra: str = "") -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self.o.get("modelo", "claude-sonnet-5"),
            system_prompt=PERSONALIDAD + extra,
            mcp_servers={"casa": self._servidor},
            allowed_tools=[
                "Read", "Glob", "Grep", "WebSearch", "WebFetch", "Edit", "Write", "Bash",
                "mcp__casa__estado_de_la_casa",
                "mcp__casa__que_esta_caido",
                "mcp__casa__ejecutar_en_la_casa",
                "mcp__casa__registro_de_home_assistant",
                "mcp__casa__complementos",
                "mcp__casa__reiniciar_complemento",
                "mcp__casa__buscar_en_mis_notas",
            ],
            can_use_tool=self._pedir_permiso,
            cwd="/share/asistente/trabajo",
        )

    async def _correr(self, pedido: str, extra: str = "") -> str:
        partes: list[str] = []
        async with ClaudeSDKClient(options=self._opciones(extra)) as cliente:
            await cliente.query(pedido)
            async for mensaje in cliente.receive_response():
                for bloque in getattr(mensaje, "content", []) or []:
                    if getattr(bloque, "type", None) == "text" or hasattr(bloque, "text"):
                        partes.append(getattr(bloque, "text", ""))
        return " ".join(p.strip() for p in partes if p).strip()

    async def encargo(self, texto: str) -> str:
        """Ariel le pide algo. Puede tardar; el resultado se dice por el parlante."""
        return await self._correr(texto)

    async def analizar(self, incidente) -> str:
        """El vigilante detecto algo. Que decida si es real y que hacer."""
        entidades = ", ".join(incidente.entidades)
        pedido = (
            f"El vigilante de la casa detecto un evento de tipo '{incidente.tipo}' "
            f"en: {entidades}. {incidente.detalle}\n\n"
            "Averigua que paso de verdad antes de hablar. Mira el estado actual, el "
            "registro de Home Assistant y lo que sepas de la casa en las notas de Ariel.\n\n"
            "Si es un falso positivo o algo que ya se resolvio solo, responde exactamente "
            "SILENCIO y nada mas: no le vamos a hablar al pepe.\n\n"
            "Si es real, decile a Ariel en dos o tres frases que se cayo, por que pensas "
            "que paso, y que hiciste o que necesitas de el. Hablado, sin listas."
        )
        return await self._correr(pedido)
