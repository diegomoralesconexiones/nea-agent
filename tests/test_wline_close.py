"""Wline (N7): el cierre explícito del despacho.

El CRM deja la conversación esperando hasta que el cerebro avisa que el turno
terminó. El aviso sale SIEMPRE que el despacho se procesó —también si el turno
reventó— y lleva la latencia del modelo acumulada en ESE turno. Un despacho
repetido que el dedup descarta no cierra: el original sigue en curso.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx

import app.dispatch as dispatch
from app.config import Settings
from app.crm_brains import BrainsCrmClient
from app.llm import LlmReply, OpenAiLlm, medir_turno
from app.state import AppContext, MemoryStore

CRM_URL = "http://crm.test"
SECRETO = "secreto-del-cerebro-de-prueba"
CONV = "cv_wline1"
IDENTITY = "525550001111"


# ── El cliente ────────────────────────────────────────────────────────────


@pytest.fixture
def cliente() -> BrainsCrmClient:
    return BrainsCrmClient(CRM_URL, SECRETO, "principal")


@respx.mock
async def test_cierra_con_la_latencia_y_el_error(cliente: BrainsCrmClient) -> None:
    ruta = respx.post(f"{CRM_URL}/api/brains/dispatch/dsp_1/close").mock(
        return_value=httpx.Response(200, json={"outcome": "respuesta_entregada"})
    )
    await cliente.cerrar_despacho("dsp_1", 1234, None)

    pedido = ruta.calls.last.request
    assert json.loads(pedido.content) == {"providerMs": 1234, "error": None}
    assert pedido.headers["authorization"] == f"Bearer {SECRETO}"


@respx.mock
async def test_el_error_viaja_recortado(cliente: BrainsCrmClient) -> None:
    ruta = respx.post(f"{CRM_URL}/api/brains/dispatch/dsp_1/close").mock(
        return_value=httpx.Response(200, json={})
    )
    await cliente.cerrar_despacho("dsp_1", None, "x" * 900)
    assert len(json.loads(ruta.calls.last.request.content)["error"]) == 500


@respx.mock
async def test_un_fallo_del_crm_no_se_propaga(cliente: BrainsCrmClient) -> None:
    respx.post(f"{CRM_URL}/api/brains/dispatch/dsp_1/close").mock(
        return_value=httpx.Response(500)
    )
    await cliente.cerrar_despacho("dsp_1", 10, None)  # no revienta


@respx.mock
async def test_una_caida_de_red_no_se_propaga(cliente: BrainsCrmClient) -> None:
    respx.post(f"{CRM_URL}/api/brains/dispatch/dsp_1/close").mock(
        side_effect=httpx.ConnectError("caído")
    )
    await cliente.cerrar_despacho("dsp_1", 10, None)  # no revienta


@respx.mock
async def test_sin_despacho_no_hay_nada_que_cerrar(cliente: BrainsCrmClient) -> None:
    ruta = respx.post(url__startswith=f"{CRM_URL}/api/brains/dispatch/").mock(
        return_value=httpx.Response(200)
    )
    await cliente.cerrar_despacho("", 10, None)
    assert ruta.call_count == 0


# ── La latencia por turno ─────────────────────────────────────────────────


def _llm_lento(ms: float) -> OpenAiLlm:
    llm = OpenAiLlm("k", "m")

    async def _complete(messages: Any, tools: Any = None) -> LlmReply:
        await asyncio.sleep(ms / 1000)
        return LlmReply(content="ok")

    llm._complete = _complete  # type: ignore[method-assign]
    return llm


async def test_complete_anota_su_latencia_en_el_turno() -> None:
    async def turno() -> list[float]:
        latencias = medir_turno()
        llm = _llm_lento(20)
        await llm.complete([])
        await llm.complete([])
        return latencias

    latencias = await asyncio.create_task(turno())
    assert len(latencias) == 2
    assert all(ms >= 15 for ms in latencias)


async def test_fuera_de_un_turno_no_anota_nada() -> None:
    async def sin_medir() -> None:
        await _llm_lento(1).complete([])  # no revienta sin medición activa

    await asyncio.create_task(sin_medir())


# ── El despacho ───────────────────────────────────────────────────────────


class CrmQueAnota:
    def __init__(self, revienta: bool = False) -> None:
        self.revienta = revienta
        self.cierres: list[tuple[str, int | None, str | None]] = []

    def registrar(self, identity: str, conversation_id: str) -> None:
        pass

    async def precargar(self, conversation_id: str) -> dict[str, Any] | None:
        if self.revienta:
            raise RuntimeError("el CRM se cayó a media precarga")
        return None

    async def cerrar_despacho(
        self, dispatch_id: str, provider_ms: int | None, error: str | None
    ) -> None:
        self.cierres.append((dispatch_id, provider_ms, error))


def _ctx(crm: Any) -> AppContext:
    settings = Settings(
        _env_file=None,
        vocero_mode="cloud",
        crm_organization="principal",
        crm_brain_secret=SECRETO,
    )
    return AppContext(settings=settings, store=MemoryStore(), crm=crm, llm=object())


def _payload(dispatch_id: str = "dsp_1") -> dict[str, Any]:
    return {
        "contractVersion": 1,
        "dispatchId": dispatch_id,
        "conversation": {"id": CONV, "isTest": False},
        "contact": {"identity": IDENTITY},
        "messages": [{"id": "msg_1", "type": "text", "text": "hola"}],
    }


@pytest.fixture
def turno_con_modelo(monkeypatch: pytest.MonkeyPatch) -> None:
    """El turno llama dos veces al modelo, de 20 ms cada una."""
    llm = _llm_lento(20)

    async def handle_flush(ctx: AppContext, identity: str, items: list[Any], **_: Any) -> None:
        await llm.complete([])
        await llm.complete([])

    monkeypatch.setattr(dispatch, "handle_flush", handle_flush)


async def test_el_turno_cierra_con_la_latencia_acumulada(turno_con_modelo: None) -> None:
    crm = CrmQueAnota()
    await dispatch._procesar(_ctx(crm), _payload())

    assert len(crm.cierres) == 1
    dispatch_id, provider_ms, error = crm.cierres[0]
    assert dispatch_id == "dsp_1"
    assert provider_ms is not None and provider_ms >= 30
    assert error is None


async def test_un_turno_sin_modelo_cierra_sin_latencia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handle_flush(ctx: AppContext, identity: str, items: list[Any], **_: Any) -> None:
        pass

    monkeypatch.setattr(dispatch, "handle_flush", handle_flush)
    crm = CrmQueAnota()
    await dispatch._procesar(_ctx(crm), _payload())
    assert crm.cierres == [("dsp_1", None, None)]


async def test_un_turno_que_revienta_cierra_igual_con_el_error() -> None:
    crm = CrmQueAnota(revienta=True)
    await dispatch._procesar(_ctx(crm), _payload())  # no propaga

    assert len(crm.cierres) == 1
    _, _, error = crm.cierres[0]
    assert error is not None and "RuntimeError" in error


async def test_un_despacho_repetido_no_cierra_el_original(turno_con_modelo: None) -> None:
    crm = CrmQueAnota()
    ctx = _ctx(crm)
    await dispatch._procesar(ctx, _payload())
    await dispatch._procesar(ctx, _payload())  # el reintento del CRM
    assert [c[0] for c in crm.cierres] == ["dsp_1"]


async def test_turnos_concurrentes_no_se_mezclan_la_latencia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lentos = {"dsp_rapido": _llm_lento(5), "dsp_lento": _llm_lento(80)}

    async def handle_flush(ctx: AppContext, identity: str, items: list[Any], **_: Any) -> None:
        await lentos[ctx.crm.despacho].complete([])

    monkeypatch.setattr(dispatch, "handle_flush", handle_flush)

    class CrmDeUnDespacho(CrmQueAnota):
        despacho = ""

        def registrar_despacho(self, conversation_id: str, dispatch_id: str) -> None:
            self.despacho = dispatch_id

    rapido, lento = CrmDeUnDespacho(), CrmDeUnDespacho()
    await asyncio.gather(
        dispatch._procesar(_ctx(lento), _payload("dsp_lento")),
        dispatch._procesar(_ctx(rapido), _payload("dsp_rapido")),
    )
    assert rapido.cierres[0][1] is not None and rapido.cierres[0][1] < 60
    assert lento.cierres[0][1] is not None and lento.cierres[0][1] >= 70
