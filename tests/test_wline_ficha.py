"""Wline (N2): la ficha que declaró el negocio.

`update_ficha` se arma en cada turno con los campos de `context.ficha.fields`
y los rechazos del CRM vuelven al modelo. Sin Wline, el catálogo de siempre.
"""
from __future__ import annotations

from app.tools import TOOL_SCHEMAS
from app.wline import catalogo, ficha_tool, seccion_prompt
from tests.wline_fixtures import CAMPOS, CrmWline, contexto, runtime_wline


def _tool(tools: list[dict], name: str) -> dict | None:
    return next((t for t in tools if t["function"]["name"] == name), None)


def test_el_esquema_sale_de_los_cuatro_tipos() -> None:
    props = ficha_tool(CAMPOS)["function"]["parameters"]["properties"]
    assert props["tipo_cliente"] == {
        "type": "string",
        "description": "Tipo de cliente",
        "enum": ["Consumidor", "Tradicional", "Moderno", "Mayorista"],
    }
    assert props["volumen_mensual"]["type"] == "number"
    assert props["giro"]["type"] == "string" and "enum" not in props["giro"]
    assert props["factura"]["type"] == "boolean"
    # Solo lo declarado: nada de los campos de la ficha de origen.
    assert set(props) == {"tipo_cliente", "volumen_mensual", "giro", "factura"}


def test_con_wline_update_ficha_es_la_del_negocio() -> None:
    tool = _tool(catalogo(TOOL_SCHEMAS, contexto()), "update_ficha")
    assert tool is not None
    assert set(tool["function"]["parameters"]["properties"]) == {
        "tipo_cliente",
        "volumen_mensual",
        "giro",
        "factura",
    }


def test_sin_wline_el_catalogo_no_cambia() -> None:
    # Un CRM que no manda `capabilities` recibe exactamente lo de siempre.
    assert catalogo(TOOL_SCHEMAS, {"contact": {}}) is TOOL_SCHEMAS
    assert catalogo(TOOL_SCHEMAS, None) is TOOL_SCHEMAS


def test_wline_sin_campos_conserva_la_ficha_de_origen() -> None:
    tool = _tool(catalogo(TOOL_SCHEMAS, contexto(ficha={"fields": None})), "update_ficha")
    assert tool is _tool(TOOL_SCHEMAS, "update_ficha")


async def test_los_rechazos_del_crm_vuelven_al_modelo() -> None:
    rechazos = [{"key": "tipo_cliente", "reason": "valor_invalido"}]
    crm = CrmWline(ficha={"ficha": {}, "accepted": [], "rejected": rechazos})
    runtime = await runtime_wline(crm, contexto())

    resultado = await runtime.execute("update_ficha", {"tipo_cliente": "Marciano"})

    assert resultado["ok"] is False
    assert resultado["rejected"] == rechazos
    assert crm.llamadas == [("put_ficha", ("cv_test1", {"tipo_cliente": "Marciano"}))]


async def test_sin_rechazos_es_ok() -> None:
    runtime = await runtime_wline(CrmWline(), contexto())
    assert await runtime.execute("update_ficha", {"volumen_mensual": 80}) == {"ok": True}


def test_el_prompt_lista_lo_pendiente() -> None:
    texto = "\n".join(seccion_prompt(contexto()))
    assert "Datos que faltan en la ficha" in texto
    assert "volumen_mensual" in texto
    assert "Datos que faltan" not in "\n".join(seccion_prompt(contexto(ficha={"pending": []})))
