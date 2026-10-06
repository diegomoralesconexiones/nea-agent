"""Wline (026, US5): la conversación que ve el agente sale de la bandeja.

El contexto trae `history` (hasta 40 mensajes, con lo que escribió el equipo).
Nea lo usa en vez de su copia: el modelo ve los últimos 20 y lo del equipo llega
marcado como tal. Sin `history` todo sigue como hasta hoy (contrato aditivo).
"""
from __future__ import annotations

from app import wline
from tests.wline_fixtures import contexto


def _historial(n: int = 3) -> list[dict[str, str]]:
    roles = ["cliente", "ia", "equipo"]
    return [
        {"role": roles[i % 3], "text": f"mensaje {i}", "at": f"2026-10-06T14:{i % 60:02d}:00Z"}
        for i in range(n)
    ]


def test_sin_history_no_hay_lista_y_se_usa_la_copia() -> None:
    assert wline.historial(contexto()) is None
    assert wline.historial(None) is None
    assert wline.historial({**contexto(), "history": "no es una lista"}) is None


def test_los_roles_se_traducen_y_lo_del_equipo_llega_marcado() -> None:
    lista = wline.historial({**contexto(), "history": _historial(3)})
    assert lista is not None
    assert [m.role for m in lista] == ["user", "assistant", "assistant"]
    assert lista[0].content == "mensaje 0"
    assert lista[1].content == "mensaje 1"
    assert lista[2].content.startswith(wline.MARCA_EQUIPO)
    assert lista[2].content.endswith("mensaje 2")


def test_el_modelo_ve_los_ultimos_20() -> None:
    lista = wline.historial({**contexto(), "history": _historial(40)})
    assert lista is not None
    assert len(lista) == wline.HISTORIAL_MODELO == 20
    assert lista[-1].content.endswith("mensaje 39") or "mensaje 39" in lista[-1].content
    assert "mensaje 20" in lista[0].content


def test_un_historial_vacio_es_una_lista_vacia_no_la_copia() -> None:
    # El Laboratorio recién creado: nada que recordar, y no se lee la copia de Nea.
    assert wline.historial({**contexto(), "history": []}) == []


def test_entradas_mal_formadas_se_saltan_sin_tumbar_el_turno() -> None:
    sucio = [{"role": "cliente", "text": "hola"}, "basura", {"role": "otro", "text": "x"}, {"role": "ia"}, None]
    lista = wline.historial({**contexto(), "history": sucio})
    assert lista is not None
    assert [m.content for m in lista] == ["hola"]
