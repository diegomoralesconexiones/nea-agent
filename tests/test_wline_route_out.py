"""Wline (N4): `route_out` exige motivo → POST /api/brains/pipeline/redirect.

Deja de escribir `{calificado:false, resultado:"dio_diy"}` en la ficha y solo
se ofrece con `capabilities.redirigir`. Sin Wline, el `route_out` de siempre.
"""
from __future__ import annotations

import json

import httpx
import respx

from app.crm_brains import BrainsCrmClient
from app.tools import TOOL_SCHEMAS
from app.wline import catalogo
from tests.wline_fixtures import CrmWline, contexto, runtime_wline

CRM_URL = "http://crm.test"


def _route_out(ctx: dict) -> dict | None:
    return next((t for t in catalogo(TOOL_SCHEMAS, ctx) if t["function"]["name"] == "route_out"), None)


def test_con_la_capacidad_exige_motivo() -> None:
    tool = _route_out(contexto())
    assert tool is not None
    assert tool["function"]["parameters"]["required"] == ["motivo"]


def test_sin_la_capacidad_no_aparece() -> None:
    assert _route_out(contexto(capabilities={"redirigir": False})) is None


def test_sin_wline_es_el_de_siempre() -> None:
    assert _route_out({}) is next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "route_out")


async def test_redirige_con_el_motivo_y_no_toca_la_ficha() -> None:
    crm = CrmWline()
    runtime = await runtime_wline(crm, contexto())

    resultado = await runtime.execute("route_out", {"motivo": "  Solo curiosea  "})

    assert resultado["ok"] is True
    assert runtime.routed_out is True
    assert crm.llamadas == [("redirect_lead", ("cv_test1", "Solo curiosea"))]


async def test_sin_motivo_no_redirige() -> None:
    crm = CrmWline()
    runtime = await runtime_wline(crm, contexto())
    resultado = await runtime.execute("route_out", {})
    assert resultado["error"] == "motivo_requerido"
    assert crm.llamadas == []
    assert runtime.routed_out is False


async def test_un_rechazo_del_crm_no_cierra_la_conversacion() -> None:
    crm = CrmWline(redirect={"ok": False, "rejection": "decision_manual", "detail": "Una persona ubicó este lead"})
    runtime = await runtime_wline(crm, contexto())
    resultado = await runtime.execute("route_out", {"motivo": "no califica"})
    assert resultado["ok"] is False and resultado["rechazo"] == "decision_manual"
    assert runtime.routed_out is False


@respx.mock
async def test_el_cliente_manda_reason_y_despacho() -> None:
    ruta = respx.post(f"{CRM_URL}/api/brains/pipeline/redirect").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    cliente = BrainsCrmClient(CRM_URL, "secreto", "principal")
    cliente.registrar_despacho("cv_1", "atr_9")
    assert await cliente.redirect_lead("cv_1", "No compra por volumen") == {"ok": True}
    assert json.loads(ruta.calls.last.request.content) == {
        "conversationId": "cv_1",
        "reason": "No compra por volumen",
        "dispatchId": "atr_9",
    }
