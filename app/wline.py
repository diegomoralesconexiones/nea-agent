"""Nea como cerebro de Wline: lo que cambia cuando el CRM es Wline (N2–N5).

Todo lo específico de Wline vive aquí, en funciones puras sobre el contexto
que el CRM manda en cada turno, para que `tools.py`, `turn.py` y `prompt.py`
solo tengan un enganche de una línea (y fusionar `upstream/main` siga siendo
barato).

Cómo se sabe que el CRM es Wline: su contexto trae `capabilities` (contrato
v1). Un CRM que no las manda recibe exactamente el comportamiento de siempre.

- N2: `update_ficha` se arma con los campos que el negocio declaró
  (`context.ficha.fields`) y el prompt lista los pendientes.
- N3: `clasificar_lead {linea, etapa}` → `POST /api/brains/pipeline/move`,
  solo con `capabilities.mover_pipeline` y al menos un criterio escrito.
- N4: `route_out` exige `motivo` → `POST /api/brains/pipeline/redirect`,
  solo con `capabilities.redirigir`.
- N5: el prompt explica líneas, criterios, umbral y dónde está el lead, y
  (025) cómo atender a cada tipo, que llega mezclado desde el panel.
"""
from __future__ import annotations

import contextvars
import json
from typing import Any

# ── 026: el silencio deliberado del turno (contracts/fase-a §3.1) ─────────
#
# Wline pasa a una persona todo turno que termina sin mensaje con la IA
# encendida. Hay un silencio que es a propósito —el relleno tras la despedida
# del cierre «sin rumbo»— y el cierre del despacho lo dice, para que Wline no
# lo tome por una falla. Variable de contexto, como la latencia del turno:
# cada despacho corre en su propia tarea.
_silencio_turno: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "nea_silencio_turno", default=None
)


def medir_silencio() -> list[str]:
    """Empieza el turno sin silencio marcado; devuelve la lista que lo recibirá."""
    marca: list[str] = []
    _silencio_turno.set(marca)
    return marca


def marcar_silencio(motivo: str) -> None:
    marca = _silencio_turno.get()
    if marca is not None and not marca:
        marca.append(motivo)


# Tipo de campo de la ficha de Wline → tipo JSON Schema.
_TIPOS = {"texto": "string", "numero": "number", "si_no": "boolean", "lista": "string"}


def es_wline(context: dict[str, Any] | None) -> bool:
    return isinstance((context or {}).get("capabilities"), dict)


def capacidad(context: dict[str, Any] | None, clave: str) -> bool:
    return bool(((context or {}).get("capabilities") or {}).get(clave))


def _lineas(context: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list(((context or {}).get("pipeline") or {}).get("lines") or [])


def _con_criterio(context: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [l for l in _lineas(context) if str(l.get("criterio") or "").strip()]


# ── N2: la ficha que declaró el negocio ──────────────────────────────────


def ficha_tool(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """`update_ficha` con exactamente los campos declarados en el CRM."""
    props: dict[str, Any] = {}
    for f in fields:
        key = str(f.get("key") or "")
        if not key:
            continue
        prop: dict[str, Any] = {"type": _TIPOS.get(str(f.get("type")), "string")}
        if f.get("label"):
            prop["description"] = str(f["label"])
        if f.get("type") == "lista" and f.get("options"):
            prop["enum"] = [str(o) for o in f["options"]]
        props[key] = prop
    return {
        "type": "function",
        "function": {
            "name": "update_ficha",
            "description": (
                "Guarda en la ficha del lead lo que ya sabes (merge: solo los "
                "campos que mandes). Llámala en cuanto descubras un dato. Usa "
                "solo los valores permitidos; si el CRM rechaza alguno, te dice "
                "cuál y por qué."
            ),
            "parameters": {"type": "object", "properties": props},
        },
    }


def resultado_ficha(respuesta: dict[str, Any] | None) -> dict[str, Any]:
    """Lo que el modelo ve después de `update_ficha` (N2).

    Los rechazos del CRM vuelven al modelo: sin esto insistiría con el mismo
    valor inválido, o daría por guardado lo que no se guardó.
    """
    rechazados = list((respuesta or {}).get("rejected") or [])
    if not rechazados:
        return {"ok": True}
    return {
        "ok": False,
        "rejected": rechazados,
        "instrucciones": (
            "esos campos NO se guardaron; usa un valor permitido o pregúntale "
            "al cliente"
        ),
    }


# ── N3: clasificar el lead ───────────────────────────────────────────────


def clasificar_tool(context: dict[str, Any] | None) -> dict[str, Any] | None:
    lineas = _con_criterio(context)
    if not capacidad(context, "mover_pipeline") or not lineas:
        return None
    return {
        "type": "function",
        "function": {
            "name": "clasificar_lead",
            "description": (
                "Ubica al lead en la línea que le corresponde según los criterios "
                "del negocio, SOLO después de registrar tipo_cliente con "
                "update_ficha. El CRM valida la propuesta y puede rechazarla. "
                "Si responde handoff=true, despídete: una persona sigue desde "
                "aquí."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "linea": {
                        "type": "string",
                        "enum": [str(l.get("key")) for l in lineas],
                    },
                    "etapa": {
                        "type": "string",
                        "description": "Nombre de la etapa de esa línea (normalmente Clasificado)",
                    },
                },
                "required": ["linea", "etapa"],
            },
        },
    }


def resultado_clasificar(respuesta: dict[str, Any] | None) -> dict[str, Any]:
    r = respuesta or {}
    if not r.get("ok"):
        return {
            "ok": False,
            "rechazo": r.get("rejection"),
            "detalle": r.get("detail"),
            "instrucciones": "no se movió; sigue la conversación",
        }
    out: dict[str, Any] = {"ok": True, "linea": r.get("line"), "etapa": r.get("stage")}
    if r.get("handoff"):
        out["handoff"] = True
        out["instrucciones"] = (
            "el lead ya quedó con una persona del equipo: despídete en una "
            "línea cálida, sin más preguntas"
        )
    return out


# ── N4: route_out con motivo ─────────────────────────────────────────────


def route_out_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "route_out",
            "description": (
                "Marca al lead como que no califica (hoy), con el motivo. "
                "Después despídete con honestidad, puerta abierta."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {
                        "type": "string",
                        "description": "Por qué no califica, en una frase (máx. 300 caracteres)",
                    }
                },
                "required": ["motivo"],
            },
        },
    }


# ── N6: la agenda por turno ──────────────────────────────────────────────

# Todo lo que agenda: sin la capacidad `agendar`, nada de esto se ofrece.
HERRAMIENTAS_DE_AGENDA = frozenset(
    {
        "propose_slots",
        "book_session",
        "reschedule_session",
        "list_bookings",
        "cancel_session",
        "coordination_consent",
    }
)


def agenda(context: dict[str, Any] | None, por_defecto: bool) -> bool:
    """¿Se ofrece agendar en este turno? Con Wline, lo dice su capacidad."""
    return capacidad(context, "agendar") if es_wline(context) else por_defecto


# ── El catálogo del turno ────────────────────────────────────────────────


def catalogo(base: list[dict[str, Any]], context: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Las herramientas de ESTE turno. Sin Wline, `base` tal cual."""
    if not es_wline(context):
        return base
    fields = ((context or {}).get("ficha") or {}).get("fields")
    con_agenda = agenda(context, False)
    out: list[dict[str, Any]] = []
    for tool in base:
        name = tool.get("function", {}).get("name")
        if name in HERRAMIENTAS_DE_AGENDA and not con_agenda:
            continue
        if name == "update_ficha" and isinstance(fields, list):
            out.append(ficha_tool(fields))
        elif name == "route_out":
            if capacidad(context, "redirigir"):
                out.append(route_out_tool())
        else:
            out.append(tool)
    clasificar = clasificar_tool(context)
    if clasificar:
        out.append(clasificar)
    return out


# ── N5: el prompt ────────────────────────────────────────────────────────


def seccion_prompt(context: dict[str, Any] | None) -> list[str]:
    """Líneas del bloque CONTEXTO ACTUAL con ficha pendiente y clasificación."""
    if not es_wline(context):
        return []
    out: list[str] = []
    pendientes = ((context or {}).get("ficha") or {}).get("pending") or []
    if pendientes:
        out.append(
            "- Datos que faltan en la ficha (pregúntalos con naturalidad, uno "
            "a la vez): " + ", ".join(str(p) for p in pendientes) + "."
        )

    lineas = _lineas(context)
    if not lineas:
        return out
    lead = (context or {}).get("lead") or {}
    umbral = ((context or {}).get("pipeline") or {}).get("threshold")
    out.append("- CLASIFICACIÓN DEL LEAD (líneas del negocio y sus criterios):")
    for l in lineas:
        criterio = str(l.get("criterio") or "").strip()
        out.append(
            f"  - {l.get('name')} (linea={l.get('key')}, tipo_cliente="
            f"{l.get('tipoCliente')}): "
            + (json.dumps(criterio, ensure_ascii=False) if criterio else "sin criterio escrito")
        )
        # 025 (US2): cómo atender a ese tipo, desde el panel de Nea.
        atencion = str(l.get("atencion") or "").strip()
        if atencion:
            out.append(f"    Cómo atenderlo: {json.dumps(atencion, ensure_ascii=False)}")
    ubicacion = lead.get("stageName") or "sin lead"
    if lead.get("line"):
        ubicacion += f" (línea {lead['line']})"
    out.append(f"- Ubicación actual del lead: {ubicacion}.")
    if umbral:
        out.append(
            f"- Al llegar a {umbral} el lead pasa a una persona del equipo y tú "
            "te despides."
        )

    reglas = [
        "registra tipo_cliente con update_ficha ANTES de clasificar",
        "solo clasifica en una línea con criterio escrito y cuando el cliente "
        "cumpla ese criterio",
    ]
    if lead.get("placedBy") == "persona":
        reglas.append(
            "una persona del equipo ubicó a este lead: NO propongas moverlo"
        )
    if not capacidad(context, "mover_pipeline") or not _con_criterio(context):
        reglas.append("en este turno no puedes clasificar; solo conversa y llena la ficha")
    out.append("- Reglas de clasificación: " + "; ".join(reglas) + ".")
    return out


# ── 026: una sola regla de pase (FR-110) ─────────────────────────────────
#
# Frases del prompt de upstream que en Wline le dicen al agente otra cosa que
# el negocio: darle a elegir «dilo con honestidad o haz handoff», mandarlo a
# pasar la conversación «cuando el lead quiera avanzar», hablarle de una agenda
# que el negocio no tiene o pedirle un «gancho» en el saludo (contradicciones
# 3 a 6 del inventario). En Wline cuándo pasar lo dicen las reglas de escalado
# del negocio, una sola vez, y el comportamiento base llega del panel.
#
# Son reemplazos de texto a propósito: el prompt de upstream no se toca. Si
# upstream cambia una frase, el reemplazo deja de aplicar y
# tests/test_wline_prompt.py lo dice en el siguiente merge.
_UNA_SOLA_REGLA: tuple[tuple[str, str], ...] = (
    ("NO lo inventes — dilo con honestidad o haz handoff). Son DATOS", "NO lo inventes). Son DATOS"),
    (
        "- ESTE NEGOCIO NO AGENDA POR AQUÍ: no ofrezcas horarios ni prometas una cita. "
        "Resuelve lo que puedas y, cuando el lead quiera avanzar, haz handoff para que "
        "lo coordine una persona.\n",
        "",
    ),
    (
        "- Es el PRIMER contacto: saluda transparente, gancho + UNA pregunta.",
        "- Es el PRIMER contacto: saluda y atiende lo que la persona pide.",
    ),
    (" Limítate a agendar y a escalar cualquier pregunta de fondo.", ""),
)


def una_sola_regla(prompt: str, context: dict[str, Any] | None) -> str:
    """El prompt de Wline sin las dobles salidas de upstream; fuera de Wline, intacto."""
    if not es_wline(context):
        return prompt
    for viejo, nuevo in _UNA_SOLA_REGLA:
        prompt = prompt.replace(viejo, nuevo)
    return prompt
