"""Wline (N5): el prompt explica líneas, criterios, umbral y ubicación.

Snapshot de la sección con y sin criterios, y con el candado humano. Sin
Wline, el prompt no cambia.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import MemoryStore
from app.wline import seccion_prompt
from tests.wline_fixtures import contexto

CON_CRITERIOS = [
    "- Datos que faltan en la ficha (pregúntalos con naturalidad, uno a la vez): volumen_mensual.",
    "- CLASIFICACIÓN DEL LEAD (líneas del negocio y sus criterios):",
    "  - Consumidores (linea=consumidores, tipo_cliente=Consumidor): sin criterio escrito",
    '  - Mayoristas (linea=mayoristas, tipo_cliente=Mayorista): "Compra por caja para reventa"',
    "- Ubicación actual del lead: Nuevo.",
    "- Al llegar a Cotizado el lead pasa a una persona del equipo y tú te despides.",
    "- Reglas de clasificación: registra tipo_cliente con update_ficha ANTES de clasificar; "
    "solo clasifica en una línea con criterio escrito y cuando el cliente cumpla ese criterio.",
]


def test_snapshot_con_criterios() -> None:
    assert seccion_prompt(contexto()) == CON_CRITERIOS


def test_sin_criterios_no_clasifica() -> None:
    ctx = contexto()
    for linea in ctx["pipeline"]["lines"]:
        linea["criterio"] = None
    reglas = seccion_prompt(ctx)[-1]
    assert reglas.endswith("en este turno no puedes clasificar; solo conversa y llena la ficha.")


def test_con_candado_humano_no_propone_mover() -> None:
    ctx = contexto(lead={"stageName": "Clasificado", "line": "mayoristas", "placedBy": "persona"})
    seccion = seccion_prompt(ctx)
    assert "- Ubicación actual del lead: Clasificado (línea mayoristas)." in seccion
    assert "una persona del equipo ubicó a este lead: NO propongas moverlo" in seccion[-1]


def test_como_atenderlo_va_con_su_linea() -> None:
    """025 (US2): la «atención» de cada tipo viene del panel (mezclada en el
    contexto por `configuracion.mezclar_atencion`); va aunque no haya criterio."""
    ctx = contexto()
    lineas = ctx["pipeline"]["lines"]
    lineas[0]["atencion"] = "Recomiéndale el paquete chico."
    lineas[1]["atencion"] = "Ofrece la lista de precios por caja."
    seccion = seccion_prompt(ctx)

    i = seccion.index("  - Consumidores (linea=consumidores, tipo_cliente=Consumidor): sin criterio escrito")
    assert seccion[i + 1] == '    Cómo atenderlo: "Recomiéndale el paquete chico."'
    j = seccion.index('  - Mayoristas (linea=mayoristas, tipo_cliente=Mayorista): "Compra por caja para reventa"')
    assert seccion[j + 1] == '    Cómo atenderlo: "Ofrece la lista de precios por caja."'
    # La regla de clasificación no cambia: sigue contando solo los criterios.
    assert seccion[-1] == CON_CRITERIOS[-1]


def test_sin_wline_no_agrega_nada() -> None:
    assert seccion_prompt({"contact": {}, "lead": {"stageName": "Nuevo"}}) == []
    assert seccion_prompt(None) == []


async def test_el_prompt_completo_la_incluye() -> None:
    conv = await MemoryStore().get_or_create_conversation("525550001111")
    prompt = build_system_prompt(
        profile=BusinessProfile(),
        context=contexto(),
        conv=conv,
        now=datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc),
    )
    assert "CLASIFICACIÓN DEL LEAD" in prompt
    assert '"Compra por caja para reventa"' in prompt
