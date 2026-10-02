"""Wline (025, US4): la cadena de respaldo del modelo y «Probar conexión».

`LlmCadena` cumple el mismo protocolo `Llm` que `OpenAiLlm` (`complete`,
`transcribe`), así que el turno no cambia: piensa con la cadena como pensaba
con un cliente. Cada eslabón es un `OpenAiLlm` sin reintentos propios, y la
cadena decide con el `tipo` de su `LlmExhausted` (R6, FR-044):

| tipo       | qué hace la cadena                                              |
|------------|-----------------------------------------------------------------|
| plataforma | salta TODOS los eslabones de esa plataforma en el turno         |
| modelo     | siguiente eslabón, sin reintento                                |
| pasajero   | un reintento corto; luego el siguiente                          |
| vacia      | siguiente eslabón                                               |

Tiempos (SC-006): 20 s por intento (el `timeout` del cliente) acotados a lo que
queda del turno, que tiene 90 s desde el despacho — menos que los 120 s que
Wline espera el cierre. Agotado todo, `LlmExhausted`: el turno calla y pasa a
humano con `error`, lo que Nea ya hacía.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from app.llm import LlmExhausted, LlmReply

logger = logging.getLogger("nea.cadena")

PRESUPUESTO_TURNO = 90.0
TOPE_INTENTO = 20.0
ESPERA_REINTENTO = 1.0

# El plazo y la traza del turno en curso. Variables de contexto, como la
# latencia (`llm._latencia_turno`): la misma Nea atiende turnos concurrentes y
# cada despacho corre en su propia tarea.
_fecha_limite: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "nea_fecha_limite", default=None
)
_traza: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "nea_traza_cadena", default=None
)


def iniciar_turno(segundos: float = PRESUPUESTO_TURNO) -> dict[str, Any]:
    """Arranca el plazo del turno; devuelve la traza que el cierre manda a Wline."""
    traza: dict[str, Any] = {}
    _fecha_limite.set(time.monotonic() + segundos)
    _traza.set(traza)
    return traza


def restante() -> float | None:
    """Segundos que le quedan al turno en curso; None fuera de un turno."""
    limite = _fecha_limite.get()
    return None if limite is None else limite - time.monotonic()


def sanear(texto: str, llave: str) -> str:
    """Un mensaje de error que se puede guardar o mostrar: sin la llave, ≤ 300."""
    if llave:
        texto = texto.replace(llave, "***")
    return re.sub(r"\bsk-[\w*.\-]+", "***", texto)[:300]


@dataclass(frozen=True)
class Plataforma:
    id: str
    nombre: str
    base_url: str
    llave: str = field(repr=False)


@dataclass
class Eslabon:
    plataforma: Plataforma
    modelo: str
    llm: Any  # OpenAiLlm, o un falso en las pruebas
    # El modelo que respondió al certificar (F15). None = no se vigila (la
    # cadena de prueba: ese eslabón es justo el que se está certificando).
    modelo_certificado: str | None = None
    requiere_atencion: bool = False


async def anotar_resultado(
    pool: Any, organization_id: str, plataforma_id: str, error: str | None
) -> None:
    """Último éxito o último error de la plataforma (FR-045). Nunca tumba nada."""
    if pool is None:
        return
    try:
        if error is None:
            await pool.execute(
                "UPDATE plataforma_modelo SET ultimo_exito_en = now() "
                "WHERE organization_id = $1 AND id = $2",
                organization_id, plataforma_id,
            )
        else:
            await pool.execute(
                "UPDATE plataforma_modelo SET ultimo_error_en = now(), ultimo_error = $3 "
                "WHERE organization_id = $1 AND id = $2",
                organization_id, plataforma_id, error[:300],
            )
    except Exception:
        logger.exception("no pude anotar el resultado de la plataforma %s", plataforma_id)


class LlmCadena:
    def __init__(
        self, eslabones: list[Eslabon], *, pool: Any = None, organization_id: str = ""
    ) -> None:
        self.eslabones = eslabones
        self._pool = pool
        self._org = organization_id
        # Plataformas caídas en ESTE turno: la cadena se arma por turno.
        self._caidas: set[str] = set()

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> LlmReply:
        traza = _traza.get()
        if traza is None:
            traza = {}  # fuera de un despacho: nadie la lee
        saltos: list[dict[str, str]] = traza.setdefault("saltos", [])
        for e in self.eslabones:
            if e.plataforma.id in self._caidas:
                continue
            for intento in (0, 1):
                try:
                    reply = await self._intento(e, messages, tools)
                except LlmExhausted as exc:
                    if exc.tipo == "presupuesto":
                        raise  # no es culpa del eslabón: se acabó el turno
                    if exc.tipo == "pasajero" and intento == 0:
                        await asyncio.sleep(ESPERA_REINTENTO)
                        continue
                    saltos.append({"plataforma": e.plataforma.nombre, "modelo": e.modelo, "tipo": exc.tipo})
                    if exc.tipo == "plataforma":
                        self._caidas.add(e.plataforma.id)
                    await anotar_resultado(
                        self._pool, self._org, e.plataforma.id,
                        f"{exc.tipo}: {sanear(str(exc), e.plataforma.llave)}",
                    )
                    break
                traza.update(plataforma=e.plataforma.nombre, modelo=e.modelo, modeloRespondio=reply.model or "")
                await anotar_resultado(self._pool, self._org, e.plataforma.id, None)
                await self._vigilar_modelo(e, reply.model)
                return reply
        raise LlmExhausted("ningún eslabón de la cadena respondió")

    async def _intento(
        self, e: Eslabon, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LlmReply:
        queda = restante()
        if queda is None:
            return await e.llm.complete(messages, tools)
        agotado = LlmExhausted("se agotaron los 90 s del turno", tipo="presupuesto")
        if queda <= 0:
            raise agotado
        try:
            return await asyncio.wait_for(e.llm.complete(messages, tools), timeout=queda)
        except TimeoutError:
            raise agotado from None

    async def _vigilar_modelo(self, e: Eslabon, respondio: str | None) -> None:
        """F15: la plataforma sirvió otro modelo bajo el nombre certificado."""
        if e.requiere_atencion or not e.modelo_certificado or not respondio:
            return
        if respondio == e.modelo_certificado:
            return
        e.requiere_atencion = True
        logger.warning(
            "%s respondió %s en vez de %s — el eslabón requiere atención",
            e.plataforma.nombre, respondio, e.modelo_certificado,
        )
        if self._pool is None:
            return
        try:
            await self._pool.execute(
                "UPDATE certificacion SET requiere_atencion = true "
                "WHERE organization_id = $1 AND plataforma_id = $2 AND modelo = $3",
                self._org, e.plataforma.id, e.modelo,
            )
        except Exception:
            logger.exception("no pude marcar el eslabón %s/%s", e.plataforma.id, e.modelo)

    async def transcribe(self, data: bytes, mime: str, filename: str = "audio.ogg") -> str:
        # Sin cambio frente a hoy: con Wline no se transcribe (R6).
        raise LlmExhausted("sin modelo de transcripción configurado — no se transcribe")


# ── «Probar conexión» (R9, FR-043) ───────────────────────────────────────

PING = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Confirma que la conexión funciona.",
        "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    },
}


async def probar_conexion(
    plataforma: Plataforma,
    modelo: str,
    extras: dict[str, Any],
    *,
    pool: Any = None,
    organization_id: str = "",
) -> dict[str, Any]:
    """¿La plataforma contesta, con ese modelo, y usa herramientas?

    Una llamada con la herramienta `ping` obligatoria, los extras del eslabón y
    20 s. Éxito = vuelve el `tool_call` de `ping`. Anota el último éxito o error
    de la plataforma. La llave nunca sale en la respuesta.
    """
    cliente = AsyncOpenAI(
        api_key=plataforma.llave, base_url=plataforma.base_url, timeout=TOPE_INTENTO, max_retries=0
    )
    resultado: dict[str, Any]
    try:
        resp = await cliente.chat.completions.create(
            model=modelo,
            messages=[{"role": "user", "content": "Prueba de conexión: llama a ping con ok=true."}],
            tools=[PING],  # type: ignore[list-item]
            tool_choice={"type": "function", "function": {"name": "ping"}},
            extra_body=extras or None,
        )
        llamadas = (resp.choices[0].message.tool_calls or []) if resp.choices else []
        if any(getattr(getattr(t, "function", None), "name", "") == "ping" for t in llamadas):
            resultado = {"ok": True, "modeloRespondio": resp.model}
        else:
            resultado = {"ok": False, "motivo": "no_usa_herramientas", "detalle": None}
    except APIStatusError as exc:
        motivo = {401: "credencial_invalida", 403: "credencial_invalida", 402: "sin_saldo", 404: "modelo_inexistente"}
        if exc.status_code in motivo:
            resultado = {"ok": False, "motivo": motivo[exc.status_code], "detalle": None}
        elif exc.status_code >= 500:
            resultado = {"ok": False, "motivo": "sin_respuesta", "detalle": None}
        else:
            resultado = {"ok": False, "motivo": "rechazo", "detalle": sanear(str(exc), plataforma.llave)}
    except APIConnectionError:
        resultado = {"ok": False, "motivo": "sin_respuesta", "detalle": None}
    finally:
        await cliente.close()
    error = None if resultado["ok"] else " ".join(filter(None, [resultado["motivo"], resultado["detalle"]]))
    await anotar_resultado(pool, organization_id, plataforma.id, error)
    return resultado
