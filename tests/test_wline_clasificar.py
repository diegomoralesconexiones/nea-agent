"""Wline (N3): `clasificar_lead {linea, etapa}` → POST /api/brains/pipeline/move.

Se ofrece solo con `capabilities.mover_pipeline` (leído del contexto de cada
turno) y al menos un criterio escrito. `handoff:true` → el turno se despide
y no vuelve a pedir un handoff; `ok:false` no tumba el turno.
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


def _nombres(ctx: dict) -> list[str]:
    return [t["function"]["name"] for t in catalogo(TOOL_SCHEMAS, ctx)]


def test_se_ofrece_con_la_capacidad_y_un_criterio() -> None:
    assert "clasificar_lead" in _nombres(contexto())


def test_solo_ofrece_las_lineas_con_criterio() -> None:
    tool = next(t for t in catalogo(TOOL_SCHEMAS, contexto()) if t["function"]["name"] == "clasificar_lead")
    assert tool["function"]["parameters"]["properties"]["linea"]["enum"] == ["mayoristas"]


def test_sin_la_capacidad_no_se_ofrece() -> None:
    ctx = contexto(capabilities={"mover_pipeline": False})
    assert "clasificar_lead" not in _nombres(ctx)


def test_sin_ningun_criterio_no_se_ofrece() -> None:
    sin = contexto()
    for linea in sin["pipeline"]["lines"]:
        linea["criterio"] = "   "
    assert "clasificar_lead" not in _nombres(sin)


def test_las_capacidades_se_leen_en_cada_turno() -> None:
    # Dos turnos seguidos con contextos distintos → catálogos distintos, sin
    # reiniciar nada (no depende de N6).
    assert "clasificar_lead" in _nombres(contexto())
    assert "clasificar_lead" not in _nombres(contexto(capabilities={"mover_pipeline": False}))


async def test_mover_con_umbral_despide_y_no_pide_otro_handoff() -> None:
    crm = CrmWline(
        move={"ok": True, "moved": True, "line": "mayoristas", "stage": "Clasificado", "handoff": True}
    )
    runtime = await runtime_wline(crm, contexto())

    resultado = await runtime.execute("clasificar_lead", {"linea": "mayoristas", "etapa": "Clasificado"})

    assert resultado["ok"] is True and resultado["handoff"] is True
    assert "despídete" in resultado["instrucciones"]
    assert runtime.umbral is True
    assert crm.llamadas == [("move_lead", ("cv_test1", "mayoristas", "Clasificado"))]
    # El CRM ya lo pausó con motivo `umbral`: otro handoff lo pisaría.
    await runtime.execute("handoff", {"reason": "pidió humano"})
    assert runtime.handoff_reason is None


async def test_un_rechazo_no_tumba_el_turno() -> None:
    crm = CrmWline(move={"ok": False, "rejection": "linea_no_coincide_ficha", "detail": "La ficha dice Moderno"})
    runtime = await runtime_wline(crm, contexto())

    resultado = await runtime.execute("clasificar_lead", {"linea": "mayoristas", "etapa": "Clasificado"})

    assert resultado == {
        "ok": False,
        "rechazo": "linea_no_coincide_ficha",
        "detalle": "La ficha dice Moderno",
        "instrucciones": "no se movió; sigue la conversación",
    }
    assert runtime.umbral is False


async def test_sin_capacidad_no_llama_al_crm_aunque_el_modelo_insista() -> None:
    crm = CrmWline()
    runtime = await runtime_wline(crm, contexto(capabilities={"mover_pipeline": False}))
    resultado = await runtime.execute("clasificar_lead", {"linea": "mayoristas", "etapa": "Clasificado"})
    assert resultado["ok"] is False
    assert crm.llamadas == []


@respx.mock
async def test_el_cliente_manda_el_despacho_y_acepta_el_rechazo_en_200() -> None:
    ruta = respx.post(f"{CRM_URL}/api/brains/pipeline/move").mock(
        return_value=httpx.Response(200, json={"ok": False, "rejection": "sin_criterios", "detail": "x"})
    )
    cliente = BrainsCrmClient(CRM_URL, "secreto", "principal")
    cliente.registrar_despacho("cv_1", "atr_1")

    respuesta = await cliente.move_lead("cv_1", "mayoristas", "Clasificado")

    assert respuesta["rejection"] == "sin_criterios"
    assert json.loads(ruta.calls.last.request.content) == {
        "conversationId": "cv_1",
        "line": "mayoristas",
        "stage": "Clasificado",
        "dispatchId": "atr_1",
    }
