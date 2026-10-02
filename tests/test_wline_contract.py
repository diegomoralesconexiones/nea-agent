"""Wline (N9): la versión del contrato CRM ⇄ cerebro.

Un despacho con un `contractVersion` que esta Nea no entiende se rechaza con
409 `contrato_incompatible` ANTES de acusar recibo: el CRM no reintenta un 409
y lo distingue de una caída (la conversación pasa a un humano y el estado dice
"versión incompatible"). Sin `contractVersion` —el CRM de origen— se procesa
igual que siempre.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest

import app.dispatch as dispatch
from app.config import Settings
from app.dispatch_worker import drain_one
from app.main import create_app
from app.state import AppContext, MemoryStore

SECRETO = "secreto-del-cerebro-de-prueba"


def _firmar(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()


@pytest.fixture
def procesados(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    vistos: list[dict[str, Any]] = []

    async def _procesar(ctx: AppContext, payload: dict[str, Any], *, durable: bool = False) -> None:
        vistos.append(payload)

    monkeypatch.setattr(dispatch, "_procesar", _procesar)
    return vistos


@pytest.fixture
def ctx() -> AppContext:
    settings = Settings(
        _env_file=None, vocero_mode="cloud", crm_organization="principal", crm_brain_secret=SECRETO
    )
    return AppContext(settings=settings, store=MemoryStore(), crm=object(), llm=object())


@pytest.fixture
async def nea(ctx: AppContext):
    transport = httpx.ASGITransport(app=create_app(ctx=ctx))
    async with httpx.AsyncClient(transport=transport, base_url="http://nea.test") as c:
        yield c


async def _despachar(
    nea: httpx.AsyncClient, ctx: AppContext, extra: dict[str, Any]
) -> httpx.Response:
    cuerpo = json.dumps(
        {
            "dispatchId": "dsp_1",
            "conversation": {"id": "cv_1"},
            "contact": {"identity": "525550001111"},
            "messages": [{"id": "msg_1", "type": "text", "text": "hola"}],
            **extra,
        }
    ).encode()
    resp = await nea.post(
        "/vocero/dispatch",
        content=cuerpo,
        headers={"content-type": "application/json", "x-vocero-signature": _firmar(cuerpo)},
    )
    # El despacho queda en el inbox durable y lo procesa el worker.
    while await drain_one(ctx):
        pass
    return resp


@pytest.mark.parametrize("version", [3, 0, "1", "dos"])
async def test_una_version_desconocida_es_409_y_no_se_procesa(
    nea: httpx.AsyncClient, ctx: AppContext, procesados: list[dict[str, Any]], version: Any
) -> None:
    resp = await _despachar(nea, ctx, {"contractVersion": version})

    assert resp.status_code == 409
    assert resp.json() == {"error": {"code": "contrato_incompatible", "supported": [1, 2]}}
    assert procesados == []


@pytest.mark.parametrize("version", [1, 2])
async def test_las_versiones_1_y_2_se_procesan(
    nea: httpx.AsyncClient, ctx: AppContext, procesados: list[dict[str, Any]], version: int
) -> None:
    # 025: la v2 trae el contexto sin `agent`/`knowledge`/`llm` (viven en Nea).
    resp = await _despachar(nea, ctx, {"contractVersion": version})
    assert resp.status_code == 200
    assert [p["dispatchId"] for p in procesados] == ["dsp_1"]


async def test_sin_version_se_procesa_como_siempre(
    nea: httpx.AsyncClient, ctx: AppContext, procesados: list[dict[str, Any]]
) -> None:
    resp = await _despachar(nea, ctx, {})
    assert resp.status_code == 200
    assert len(procesados) == 1


async def test_la_firma_se_valida_antes_que_la_version(
    nea: httpx.AsyncClient, procesados: list[dict[str, Any]]
) -> None:
    # Quien no firma no aprende qué versiones entiende esta Nea.
    cuerpo = json.dumps({"contractVersion": 2}).encode()
    resp = await nea.post(
        "/vocero/dispatch", content=cuerpo, headers={"x-vocero-signature": "sha256=00"}
    )
    assert resp.status_code == 401
