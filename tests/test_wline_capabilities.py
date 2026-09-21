"""Wline (N6): la agenda por turno, no al arrancar.

En modo cloud Nea ya no sondea la agenda en el `lifespan`: con Wline, las
herramientas de agenda se ofrecen solo si el contexto del turno trae la
capacidad `agendar`. Dos despachos seguidos con capacidades distintas dan
catálogos distintos sin reiniciar. Sin Wline, lo de siempre.
"""
from __future__ import annotations

import httpx
import respx

from app import main
from app.tools import tool_schemas
from app.wline import HERRAMIENTAS_DE_AGENDA, agenda, catalogo
from tests.test_wline_proxy import CRM_URL, arranque_wline  # noqa: F401 (fixture)
from tests.wline_fixtures import contexto


def _nombres(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}


@respx.mock
async def test_en_cloud_no_hay_sondeo_de_agenda_al_arrancar(arranque_wline: None) -> None:
    ruta = respx.route(url__startswith=CRM_URL).mock(return_value=httpx.Response(200, json={}))
    app = main.create_app()
    async with app.router.lifespan_context(app):
        pass
    # Antes, un 200 del CRM aquí dejaba "hay agenda" para siempre.
    assert ruta.call_count == 0


def test_sin_la_capacidad_no_se_ofrece_la_agenda() -> None:
    base = tool_schemas(agenda_enabled=True, agenda_v2=True, coordination=True)
    assert _nombres(base) & HERRAMIENTAS_DE_AGENDA  # el catálogo base sí las trae
    ofrecidas = _nombres(catalogo(base, contexto(capabilities={"agendar": False})))
    assert not ofrecidas & HERRAMIENTAS_DE_AGENDA


def test_con_la_capacidad_si() -> None:
    base = tool_schemas(agenda_enabled=True)
    ofrecidas = _nombres(catalogo(base, contexto(capabilities={"agendar": True})))
    assert {"propose_slots", "book_session", "reschedule_session"} <= ofrecidas


def test_dos_despachos_seguidos_con_capacidades_distintas() -> None:
    base = tool_schemas(agenda_enabled=True)
    primero = _nombres(catalogo(base, contexto(capabilities={"agendar": True, "mover_pipeline": True})))
    segundo = _nombres(catalogo(base, contexto(capabilities={"agendar": False, "mover_pipeline": False})))
    assert "propose_slots" in primero and "clasificar_lead" in primero
    assert "propose_slots" not in segundo and "clasificar_lead" not in segundo


def test_el_prompt_sigue_la_capacidad() -> None:
    assert agenda(contexto(capabilities={"agendar": False}), por_defecto=True) is False
    assert agenda(contexto(capabilities={"agendar": True}), por_defecto=False) is True
    # Sin Wline, lo que ya sabía la Nea.
    assert agenda({"contact": {}}, por_defecto=True) is True
    assert agenda(None, por_defecto=False) is False
