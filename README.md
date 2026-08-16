# Asistente de Ariel

Complemento de Home Assistant. Un agente que vive adentro de la casa: vigila
los dispositivos, avisa por el parlante cuando algo se cae y ayuda a
resolverlo. Corre en el mini PC donde ya corre Home Assistant, así que está
prendido siempre y llega a la red de casa.

## Instalar

1. En Home Assistant: **Ajustes → Complementos → Tienda de complementos**.
2. Menú de los tres puntos (arriba a la derecha) → **Repositorios**.
3. Pegar `https://github.com/arielargain/asistente-casa` y **Añadir**.
4. Aparece *Asistente de Ariel* en la tienda. **Instalar**.
5. En la pestaña **Configuración** del complemento, pegar la clave de Anthropic
   y ajustar el parlante si hace falta. **Guardar** y **Iniciar**.

## Cómo está pensado

**Las reglas detectan, el modelo interpreta.** El vigilante es código común y
corriente que mira los cambios de estado. Solo cuando algo cruza un umbral se
despierta el agente. Si el modelo mirara cada cambio, con 700 entidades la
cuenta sería impagable y además hablaría todo el día.

**No llora lobo.** Tres frenos:

- *Espera*: un enchufe que parpadea 30 segundos no es una caída.
- *Agrupa*: si se cae el WiFi y con él 12 entidades, es un aviso, no 12.
- *Silencio*: cada entidad tiene un tiempo mínimo entre avisos.

Y encima de eso, el agente verifica antes de hablar: mira el estado real, el
registro de HA y las notas de la casa. Si concluye que fue un falso positivo,
se calla.

**Los encargos no bloquean.** "Revisá por qué se cayó el AP" contesta al toque
*"dale, me pongo con eso"*, trabaja en segundo plano y te habla por el parlante
cuando tiene la respuesta. El micrófono no queda colgado esperando.

## Permisos

Libre: leer estados, ver qué está caído, leer el registro, listar complementos,
buscar en las notas, buscar en la web, y órdenes de consola de solo mirar.

Pide permiso: todo lo que cambia algo — llamar servicios, editar archivos,
escribir, `git push`, reiniciar cosas. El agente te dice en una frase qué
quiere hacer; con un *"dale"* queda autorizado diez minutos.

Lo que se puede hacer sin preguntar se ajusta en `permitir_sin_preguntar`.

## Opciones

| Opción | Para qué |
|---|---|
| `clave_anthropic` | La clave de la API. |
| `modelo` | Modelo a usar. |
| `parlante` | Por dónde habla. |
| `voz` | Entidad de TTS. |
| `minutos_para_avisar` | Cuánto tiene que estar caído algo antes de avisar. |
| `minutos_entre_avisos` | Silencio mínimo por entidad. |
| `entidades_criticas` | Avisan en 30 segundos y con aviso propio. |
| `entidades_ignoradas` | Nunca avisan. |
| `permitir_sin_preguntar` | Acciones autorizadas de antemano. |

## Panel

El complemento agrega un panel en la barra lateral: qué está caído ahora, qué
está en observación y la bitácora de lo que fue diciendo.
