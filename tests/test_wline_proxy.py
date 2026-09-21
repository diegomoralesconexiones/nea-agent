"""Wline (N1): una Nea de un solo negocio que piensa por el CRM.

Con `llm` en el contexto, el turno NO usa `LLM_API_KEY`: le pide al CRM por
`{CRM}{llm.path}` con `CRM_BRAIN_SECRET`, y la llave del proveedor se queda
allá. `llm: null` = el CRM no tiene con qué pensar → nadie contesta y la
conversación pasa a un humano. Sin `llm` en el contexto (un CRM que no lo
ofrece), el comportamiento de siempre.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from app.config import Settings
from app.dispatch import _contexto_una_organizacion
from app.llm import OpenAiLlm
from app.state import AppContext, MemoryStore

CRM_URL = "http://crm.test"
SECRETO = "secreto-del-cerebro-de-prueba"
CONV = "cv_wline1"


class CrmConContexto:
    """Lo mínimo del cliente del CRM que usa el despacho de un solo negocio."""

    def __init__(self, contexto: dict[str, Any] | None) -> None:
        self.contexto = contexto
        self.handoffs: list[tuple[str, str]] = []

    async def precargar(self, conversation_id: str) -> dict[str, Any] | None:
        return self.contexto

    async def post_handoff(self, conversation_id: str, reason: str) -> None:
        self.handoffs.append((conversation_id, reason))


def _ctx(crm: Any) -> AppContext:
    settings = Settings(
        _env_file=None,
        vocero_mode="cloud",
        crm_organization="principal",
        crm_brain_secret=SECRETO,
        crm_base_url=CRM_URL,
        llm_api_key="sk-del-entorno",
        llm_model="modelo/instancia",
    )
    return AppContext(settings=settings, store=MemoryStore(), crm=crm, llm=object())


async def test_con_llm_el_turno_piensa_por_el_crm() -> None:
    ctx = _ctx(CrmConContexto({"llm": {"path": "/api/brains/llm", "model": "a/b"}}))

    turno = await _contexto_una_organizacion(ctx, CONV)

    assert turno is not None
    assert isinstance(turno.llm, OpenAiLlm)
    assert str(turno.llm._client.base_url).startswith(f"{CRM_URL}/api/brains/llm")
    # El secreto del cerebro, no la llave del proveedor del entorno.
    assert turno.llm._client.api_key == SECRETO
    assert "sk-del-entorno" not in str(turno.llm._client.api_key)
    cabeceras = turno.llm._client.default_headers or {}
    assert cabeceras.get("X-Vocero-Organization") == "principal"
    assert turno.llm._model == "a/b"


async def test_sin_modelo_en_el_contexto_cae_al_de_la_instancia() -> None:
    ctx = _ctx(CrmConContexto({"llm": {"path": "/api/brains/llm"}}))
    turno = await _contexto_una_organizacion(ctx, CONV)
    assert turno is not None
    assert turno.llm._model == "modelo/instancia"


async def test_llm_null_no_piensa_y_pasa_a_un_humano() -> None:
    crm = CrmConContexto({"llm": None})
    ctx = _ctx(crm)

    turno = await _contexto_una_organizacion(ctx, CONV)

    assert turno is None
    assert crm.handoffs == [(CONV, "sin_credencial")]


async def test_sin_llm_en_el_contexto_todo_sigue_como_siempre() -> None:
    # Un CRM que no ofrece el proxy: la Nea usa su propia llave, igual que antes.
    ctx = _ctx(CrmConContexto({"agent": {}}))
    assert await _contexto_una_organizacion(ctx, CONV) is ctx


async def test_sin_contexto_todo_sigue_como_siempre() -> None:
    # La precarga falló: el turno lo reintenta en `get_context` como siempre.
    ctx = _ctx(CrmConContexto(None))
    assert await _contexto_una_organizacion(ctx, CONV) is ctx


async def test_un_crm_sin_precarga_no_cambia_nada() -> None:
    ctx = _ctx(object())
    assert await _contexto_una_organizacion(ctx, CONV) is ctx


@respx.mock
async def test_la_llamada_al_modelo_va_al_crm_con_el_secreto() -> None:
    """De punta a punta: lo que sale por el cable es lo que el CRM espera."""
    ruta = respx.post(f"{CRM_URL}/api/brains/llm/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "a/b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hola desde el CRM"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )
    )
    ctx = _ctx(CrmConContexto({"llm": {"path": "/api/brains/llm", "model": "a/b"}}))
    turno = await _contexto_una_organizacion(ctx, CONV)
    assert turno is not None

    respuesta = await turno.llm.complete([{"role": "user", "content": "hola"}])

    assert respuesta.content == "hola desde el CRM"
    pedido = ruta.calls.last.request
    assert pedido.headers["authorization"] == f"Bearer {SECRETO}"
    assert pedido.headers["x-vocero-organization"] == "principal"
    assert json.loads(pedido.content)["model"] == "a/b"
