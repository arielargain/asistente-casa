"""El agente: Claude con herramientas de verdad.

Es el que razona. Lo despierta el vigilante cuando hay un incidente, o Ariel
cuando le encarga algo por voz. Tiene las herramientas nativas de la SDK
(leer, escribir y editar archivos, bash, buscar) mas las de la casa que se
definen aca abajo.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
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

# Puertos PoE del switch "casa Arielito" (SG2210MP, controlador Omada local).
# Cortar y reponer un puerto reinicia fisicamente lo que cuelga de el.
PUERTO_POE = "switch.ac_15_a2_2d_7a_ec_puerto_{n}_poe"
CONSUMO_POE = "sensor.ac_15_a2_2d_7a_ec_potencia_poe_del_puerto_{n}"
PUERTOS_CONOCIDOS = {2: "AP-265-3", 4: "AP-Outdoor", 5: "AP-265-2"}

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
- Cuando Ariel te encarga algo, su encargo YA es la autorizacion: ejecuta
  hasta el final sin pedir permiso de nuevo. Si una herramienta te dice que
  falta autorizacion, reintentala una vez antes de rendirte.
- Antes de afirmar que algo esta roto, verificalo. Mira el estado, el historial
  y el registro.
- Si no podes resolver algo, decile que falta y por que, sin adornarlo.
- Cuando avisas de un problema, en la misma frase deci que vas a hacer o que
  necesitas de el.
- De 00 a 11 Ariel duerme: tenes via libre para actuar solo sobre el indoor,
  las camaras y el dormitorio (el permiso te va a dejar pasar). Pero JAMAS
  hagas sonar nada en ese horario: ni parlantes, ni tele, ni avisos. Lo que
  hagas de noche se cuenta en el parte de las 11:30. De dia, pedi el dale
  antes de cambiar cualquier cosa.

Protocolo de reinicio (regla de Ariel, 11/9/2026). Casi todo aparato de la
casa tiene DOS capas de reinicio, y el que no, tiene al menos una. Siempre
tenes una herramienta para actuar antes de avisar. El orden es fijo:
  1. BLANDO primero: recargar la integracion (recargar_integracion o los
     scripts recargar_zigbee_casa, recargar_voice_pe, recargar_broadlink,
     recargar_cerradura, recargar_bocinas_google), boton ONVIF de reinicio de
     la camara (button.*_reboot), reiniciar el complemento (Frigate, Omada,
     Zigbee2MQTT). Si es una camara Imou "viva en la app pero caida en HA",
     la app la tiene tomada: la camara admite UNA sola conexion de video.
  2. FISICO despues: cortar y reponer su enchufe WiFi con el script de ese
     aparato (todos llaman a script.reiniciar_enchufe, que reintenta prender):
     reiniciar_grabador (grabador viejo + las 7 camaras del perimetro + cocina),
     reiniciar_camara_medio_chica, reiniciar_camara_carpa_pared,
     reiniciar_ventilador_medio_chica, reiniciar_ventilador_carpa_pared,
     reiniciar_extractores_indoor, reiniciar_riego (bomba + controlador),
     reiniciar_aire_gimnasio, reiniciar_aire_dormitorio, reiniciar_comedero,
     reiniciar_bebedero, reiniciar_bocina_gimnasio, reiniciar_bocina_dormitorio,
     reiniciar_bocina_huespedes, reiniciar_router, reiniciar_switch_casa,
     reiniciar_switch_indoor. Los AP van por PoE: reiniciar_ap_265_1/2/3,
     reiniciar_ap_outdoor (o reiniciar_por_poe). La antena Zigbee del indoor:
     reiniciar_antena_indoor.
  3. TERCERA capa solo en el indoor: si el enchufe WiFi de un aparato de una
     carpa no responde, se corta el tomacorriente Zigbee de esa carpa
     (reiniciar_enchufe_zigbee_carpa_pared, reiniciar_enchufe_zigbee_carpa_medio):
     reinicia el enchufe WiFi y todo lo que cuelga de el.
Los scripts se disparan con ejecutar_en_la_casa (dominio script, servicio
turn_on, datos entity_id script.<nombre>) o script.reiniciar_enchufe con
datos aparato y segundos.

Tiempos, sin excepcion: un aparato caido se deja 20 minutos antes de tocarlo
(los parpadeos se arreglan solos). A los 20 minutos actuas vos: blando, y si
no vuelve, fisico. Recien si a la HORA de caido sigue caido con todo probado,
se le avisa a Ariel por la bocina (y solo de 11 a 24). Antes de la hora, nada
de voz: deja notificacion persistente en HA y segui. Un aparato que volvio no
se anuncia por voz; se anota en la notificacion y en el parte de las 11:30.

Reglas que no se negocian:
- Router y switches se reinician DE A UNO, por su enchufe, y solo cuando el
  problema es ese aparato. Nunca los tres seguidos, nunca "todo el rack".
  Cortar el switch de la casa tira los 4 AP un minuto; el del indoor, la
  antena Zigbee y el grabador nuevo.
- La luz de cultivo (luz_pared, luz_medio) no se corta salvo que la luz misma
  sea el problema: altera el fotoperiodo.
- Tras reponer el enchufe de un humidificador Deerma, a los 2 minutos verifica
  que haya vuelto a prender (suele quedar apagado). Si no, prendelo vos.
- Sensores a pila (puertas, temperaturas SNZB-02D y Zigbee de carpa, agua,
  monoxido) no se reinician: si no reportan, es pila o emparejamiento. Se
  informa, no se corta nada.
- Los aparatos tardan 1 a 3 minutos en reaparecer; verifica con
  que_esta_caido antes de dar por resuelto. El detalle completo esta en el
  segundo cerebro: buscar_en_mis_notas "Protocolo de reinicio".

Auditoria diaria (orden permanente de Ariel, 15/9/2026): la casa tiene que
seguir sola a los aparatos que se cambian o se renombran. Cada dia, con el
parte, corre referencias_rotas. Si una automatizacion o script apunta a una
entidad que ya no existe, busca la equivalente (el mismo aparato renombrado o
reemplazado: estado_de_la_casa con parte del nombre, buscar_en_mis_notas) y
corregila con reemplazar_entidad. Si no hay equivalente clara, no inventes:
dejalo anotado. El resultado va a una notificacion persistente en HA
(notification_id auditoria_referencias), nunca por el parlante.
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

    @tool("reiniciar_por_poe", "Reinicia FISICAMENTE un aparato cortando y reponiendo su puerto PoE del switch casa Arielito (10 segundos sin corriente). Puertos conocidos: 2 alimenta AP-265-3, 4 alimenta AP-Outdoor, 5 alimenta AP-265-2. AP-265-1 no tiene puerto (va por mesh). Antes de cortar, verifica con estado_de_la_casa el sensor de consumo del puerto: si no estas seguro de que alimenta, no lo cortes", {"puerto": int})
    async def reiniciar_por_poe(args: dict[str, Any]) -> dict:
        n = int(args["puerto"])
        if not 1 <= n <= 8:
            return {"content": [{"type": "text", "text": "El switch tiene puertos 1 a 8."}]}
        entidad = PUERTO_POE.format(n=n)
        if not hogar.estado(entidad):
            return {"content": [{"type": "text", "text": f"No encuentro {entidad}: revisa la integracion tplink_omada."}]}
        await hogar.llamar_servicio("switch", "turn_off", {"entity_id": entidad})
        await asyncio.sleep(10)
        await hogar.llamar_servicio("switch", "turn_on", {"entity_id": entidad})
        alimenta = PUERTOS_CONOCIDOS.get(n, "aparato desconocido")
        return {"content": [{"type": "text", "text": (
            f"Listo: corte y repuse el puerto {n} ({alimenta}). El aparato tarda "
            "1 a 3 minutos en volver a estar en linea; verifica despues con que_esta_caido."
        )}]}


    @tool("referencias_rotas", "Audita TODAS las automatizaciones y scripts: devuelve las que apuntan a entidades que ya no existen (faltan) y las que apuntan a entidades caidas (caidas). Es la base de la auditoria diaria", {})
    async def referencias_rotas(_args: dict[str, Any]) -> dict:
        r = await hogar.referencias_rotas(set(opciones.get("entidades_ignoradas") or []))
        return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)[:8000]}]}

    @tool("leer_automatizacion", "Devuelve la configuracion completa de una automatizacion (automation.xxx) o de un script (script.xxx)", {"entidad": str})
    async def leer_automatizacion(args: dict[str, Any]) -> dict:
        eid = str(args["entidad"])
        if eid.startswith("automation."):
            aid = ((hogar.estado(eid) or {}).get("attributes") or {}).get("id")
            r = await hogar.config_ha(f"automation/config/{aid}")
        else:
            r = await hogar.config_ha(f"script/config/{eid[7:]}")
        return {"content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)[:8000]}]}

    @tool("reemplazar_entidad", "Reemplaza una entidad por otra en TODAS las automatizaciones y scripts de una sola vez (cuando un aparato se cambio o se renombro). Antes verifica con estado_de_la_casa que la nueva exista. Devuelve la lista de lo que cambio", {"vieja": str, "nueva": str})
    async def reemplazar_entidad(args: dict[str, Any]) -> dict:
        vieja, nueva = str(args["vieja"]).strip(), str(args["nueva"]).strip()
        if not hogar.estado(nueva):
            return {"content": [{"type": "text", "text": f"No existe {nueva}: no cambio nada."}]}
        cambiadas = await hogar.reemplazar_entidad(vieja, nueva)
        return {"content": [{"type": "text", "text": f"Reemplazada {vieja} por {nueva} en: {', '.join(cambiadas) or 'ningun lado'}"}]}

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
            reiniciar_por_poe,
            referencias_rotas,
            leer_automatizacion,
            reemplazar_entidad,
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
            # OJO (aprendido el 17/8): lo que figura aca se AUTO-APRUEBA y
            # esquiva el permiso() de main.py (CanUseToolShadowedWarning).
            # Solo van las herramientas de mirar y las de escribir notas.
            # Bash, ejecutar_en_la_casa, reiniciar_por_poe y compania quedan
            # afuera a proposito: asi si pasan por el control de permisos.
            allowed_tools=[
                "Read", "Glob", "Grep", "WebSearch", "WebFetch", "Edit", "Write",
                "mcp__casa__estado_de_la_casa",
                "mcp__casa__que_esta_caido",
                "mcp__casa__registro_de_home_assistant",
                "mcp__casa__complementos",
                "mcp__casa__listar_integraciones",
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
        # Sin esto el modelo inventa la hora cuando se la preguntan.
        ahora = datetime.now().strftime("%d/%m/%Y %H:%M")
        return await self._correr(f"(Ahora es {ahora}, hora local.) {texto}")

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
            "Si es real y el aparato caido cuelga de un puerto PoE del switch, "
            "tenes la herramienta reiniciar_por_poe para reiniciarlo fisicamente "
            "(pedile el dale a Ariel si el permiso te lo exige).\n\n"
            "Si es real, decile a Ariel en dos o tres frases que se cayo, por que pensas "
            "que paso, y que hiciste o que necesitas de el. Hablado, sin listas."
        )
        return await self._correr(pedido)
