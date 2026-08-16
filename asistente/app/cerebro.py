"""Busqueda por significado en el segundo cerebro.

Le pega a la Edge Function de Supabase que ya indexa las notas del repo
arielargain/segundo-cerebro. El repo nunca se toca: el indice es una copia
derivada de solo lectura.
"""

from __future__ import annotations

import json
import logging

import aiohttp

log = logging.getLogger("cerebro")

URL = "https://yjdxtvgkkdcgihatmkak.supabase.co/functions/v1/cerebro"
LLAVE_PUBLICA = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlqZHh0dmdra2RjZ2loYXRta2FrIiwicm9sZSI6ImFub24i"
    "LCJpYXQiOjE3ODQxMzgxODksImV4cCI6MjA5OTcxNDE4OX0.Xt57v7dVYJPN1MH_rm43iWOfyAl7hqBUbslgKtft0xo"
)
FICHA = "cbr_7Kq2xW9mZ4tR1nH6vB3sL8dY"


async def buscar_en_el_cerebro(consulta: str, cantidad: int = 6) -> str:
    cabeceras = {
        "apikey": LLAVE_PUBLICA,
        "Authorization": f"Bearer {LLAVE_PUBLICA}",
        "x-cerebro-token": FICHA,
        "Content-Type": "application/json",
    }
    cuerpo = {"accion": "buscar", "texto": consulta, "cantidad": cantidad}
    try:
        tiempo = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=tiempo) as s:
            async with s.post(URL, headers=cabeceras, json=cuerpo) as r:
                datos = await r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("no pude buscar en el cerebro: %s", e)
        return "No pude consultar las notas en este momento."

    filas = datos.get("resultados") or []
    if not filas:
        return "No hay nada sobre eso en las notas."
    trozos = [f"[{f.get('ruta')}] {f.get('contenido')}" for f in filas]
    return json.dumps({"fragmentos": trozos}, ensure_ascii=False)[:8000]
