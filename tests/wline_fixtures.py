"""Contexto v1 de Wline para las pruebas del fork (contracts/brain-contract.md §3.1)."""
from __future__ import annotations

import copy
from typing import Any

from app.state import AppContext, MemoryStore
from app.tools import ToolRuntime
from tests.conftest import CRM_CONV_ID, IDENTITY, FakeLLM, make_settings

CAMPOS = [
    {
        "key": "tipo_cliente",
        "label": "Tipo de cliente",
        "type": "lista",
        "required": True,
        "options": ["Consumidor", "Tradicional", "Moderno", "Mayorista"],
    },
    {"key": "volumen_mensual", "label": "Volumen mensual", "type": "numero", "required": True},
    {"key": "giro", "label": "Giro", "type": "texto", "required": False},
    {"key": "factura", "label": "¿Requiere factura?", "type": "si_no", "required": False},
]

_BASE: dict[str, Any] = {
    "contractVersion": 1,
    "capabilities": {"mover_pipeline": True, "redirigir": True, "agendar": False},
    "ficha": {"fields": CAMPOS, "pending": ["volumen_mensual"]},
    "pipeline": {
        "lines": [
            {
                "key": "consumidores",
                "name": "Consumidores",
                "tipoCliente": "Consumidor",
                "criterio": None,
                "stages": [],
            },
            {
                "key": "mayoristas",
                "name": "Mayoristas",
                "tipoCliente": "Mayorista",
                "criterio": "Compra por caja para reventa",
                "stages": [{"name": "Clasificado", "anchor": "clasificado"}, {"name": "Cotizado"}],
            },
        ],
        "threshold": "Cotizado",
        "outcomes": ["Cliente", "Perdido", "Redirigido"],
    },
    "lead": {"id": "ld_1", "stageName": "Nuevo", "stageAnchor": "nuevo", "line": None, "placedBy": None},
    "contact": {"name": "Lead de Prueba", "ficha": {}},
    "conversation": {"id": CRM_CONV_ID, "aiEnabled": True, "windowOpen": True, "isTest": False},
}


def contexto(**cambios: Any) -> dict[str, Any]:
    """El contexto de Wline con cambios de primer nivel (se mezclan un nivel)."""
    ctx = copy.deepcopy(_BASE)
    for k, v in cambios.items():
        if isinstance(v, dict) and isinstance(ctx.get(k), dict):
            ctx[k] = {**ctx[k], **v}
        else:
            ctx[k] = v
    return ctx


class CrmWline:
    """Lo que el ToolRuntime le pide al CRM de Wline, con respuestas en cola."""

    def __init__(self, **respuestas: Any) -> None:
        self.respuestas = respuestas
        self.llamadas: list[tuple[str, tuple[Any, ...]]] = []

    async def put_ficha(self, conversation_id: str, ficha: dict[str, Any]) -> dict[str, Any]:
        self.llamadas.append(("put_ficha", (conversation_id, ficha)))
        return self.respuestas.get("ficha", {"ficha": ficha, "accepted": list(ficha)})

    async def move_lead(self, conversation_id: str, linea: str, etapa: str) -> dict[str, Any]:
        self.llamadas.append(("move_lead", (conversation_id, linea, etapa)))
        return self.respuestas.get("move", {"ok": True, "moved": True, "line": linea, "stage": etapa, "handoff": False})

    async def redirect_lead(self, conversation_id: str, motivo: str) -> dict[str, Any]:
        self.llamadas.append(("redirect_lead", (conversation_id, motivo)))
        return self.respuestas.get("redirect", {"ok": True})


async def runtime_wline(crm: Any, context: dict[str, Any] | None) -> ToolRuntime:
    ctx = AppContext(settings=make_settings(), store=MemoryStore(), crm=crm, llm=FakeLLM())
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    return ToolRuntime(ctx, conv, CRM_CONV_ID, context=context)
