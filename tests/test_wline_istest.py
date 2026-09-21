"""Wline (N8): la conversación de prueba del CRM salta la allowlist.

El Laboratorio de Wline conversa con el cerebro en sandbox: el CRM jamás
manda esas respuestas a WhatsApp. Su identidad (`lab:<caso>`) nunca estará en
`ALLOWED_WA_IDS`, así que sin esta excepción el cerebro se callaría y el
Laboratorio no podría evaluarlo. Una conversación REAL fuera de la lista sigue
sin respuesta.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

import app.dispatch as dispatch
from app.config import Settings
from app.state import AppContext, InboundMessage, MemoryStore
from app.turn import run_turn
from tests.conftest import make_ctx, make_settings, mock_crm_basics

FUERA = "529999999999"


def _entrante(identity: str) -> list[InboundMessage]:
    return [
        InboundMessage(wa_message_id="wamid.lab", identity=identity, type="text", text="hola")
    ]


@pytest.fixture
def ctx_con_allowlist() -> AppContext:
    return make_ctx(make_settings(allowed_wa_ids="5215550001111"))


async def test_fuera_de_la_allowlist_una_conversacion_real_no_se_atiende(
    ctx_con_allowlist: AppContext, respx_mock: Any
) -> None:
    routes = mock_crm_basics(respx_mock)
    await run_turn(ctx_con_allowlist, FUERA, _entrante(FUERA))
    assert routes["messages"].call_count == 0
    assert ctx_con_allowlist.llm.calls == []


async def test_una_conversacion_de_prueba_se_atiende_aunque_no_este_en_la_lista(
    ctx_con_allowlist: AppContext, respx_mock: Any
) -> None:
    routes = mock_crm_basics(respx_mock)
    ctx = replace(ctx_con_allowlist, es_prueba=True)
    await run_turn(ctx, FUERA, _entrante(FUERA))
    assert len(ctx.llm.calls) == 1
    assert routes["messages"].call_count == 1


# ── El despacho marca la prueba ───────────────────────────────────────────


def _ctx() -> AppContext:
    settings = Settings(
        _env_file=None, vocero_mode="cloud", crm_organization="principal", crm_brain_secret="s"
    )
    return AppContext(settings=settings, store=MemoryStore(), crm=object(), llm=object())


@pytest.fixture
def turnos(monkeypatch: pytest.MonkeyPatch) -> list[AppContext]:
    vistos: list[AppContext] = []

    async def handle_flush(ctx: AppContext, identity: str, items: list[Any]) -> None:
        vistos.append(ctx)

    monkeypatch.setattr(dispatch, "handle_flush", handle_flush)
    return vistos


def _payload(dispatch_id: str, conversacion: dict[str, Any]) -> dict[str, Any]:
    return {
        "dispatchId": dispatch_id,
        "conversation": {"id": "cv_lab", **conversacion},
        "contact": {"identity": "lab:caso_1"},
        "messages": [{"id": "msg_1", "type": "text", "text": "hola"}],
    }


@pytest.mark.parametrize(
    ("conversacion", "es_prueba"),
    [
        ({"isTest": True}, True),
        ({"isTest": False}, False),
        ({}, False),
        # Solo el booleano cuenta: un texto no abre la puerta.
        ({"isTest": "true"}, False),
    ],
)
async def test_el_despacho_marca_la_prueba_solo_con_isTest_true(
    turnos: list[AppContext], conversacion: dict[str, Any], es_prueba: bool
) -> None:
    await dispatch._procesar(_ctx(), _payload("dsp_1", conversacion))
    assert len(turnos) == 1
    assert turnos[0].es_prueba is es_prueba
