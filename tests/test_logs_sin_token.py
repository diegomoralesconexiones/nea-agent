"""El token del webhook del CRM no llega a los logs.

httpx escribe a INFO la URL de cada petición, y la del relay lleva el token del
webhook del CRM en la ruta. Lo encontró la prueba de punta a punta contra
Vocero raíz (`scripts/e2e_contra_raiz.py`): cada entrante lo dejaba en el log.
"""
from __future__ import annotations

import logging

import httpx
import pytest
import respx

from app.main import SinTokenDelWebhook


def _registro(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args, None)


def test_tacha_el_token_de_la_ruta_del_webhook() -> None:
    registro = _registro(
        'HTTP Request: %s %s "%s %d %s"',
        "POST",
        httpx.URL("https://crm.test/api/webhooks/wa/s3cr3t-del-webhook"),
        "HTTP/1.1",
        200,
        "OK",
    )
    assert SinTokenDelWebhook().filter(registro) is True
    assert registro.getMessage() == (
        'HTTP Request: POST https://crm.test/api/webhooks/wa/*** "HTTP/1.1 200 OK"'
    )


def test_las_demas_lineas_quedan_igual() -> None:
    registro = _registro(
        "HTTP Request: %s %s", "GET", "http://crm.test/api/bot/context?waIdentity=525512345678"
    )
    SinTokenDelWebhook().filter(registro)
    assert registro.getMessage() == (
        "HTTP Request: GET http://crm.test/api/bot/context?waIdentity=525512345678"
    )


@respx.mock
def test_el_relay_real_no_deja_el_token_en_el_log(caplog: pytest.LogCaptureFixture) -> None:
    url = "https://crm.test/api/webhooks/wa/s3cr3t-del-webhook"
    respx.post(url).mock(return_value=httpx.Response(200))
    with caplog.at_level(logging.INFO, logger="httpx"):
        httpx.post(url, content=b"{}")
    assert "s3cr3t-del-webhook" not in caplog.text
    assert "/api/webhooks/wa/***" in caplog.text


# ── Wline (025): la llave de una plataforma tampoco (FR-041, SC-014) ──────

from tests.test_panel_publicar import _nea, pool  # noqa: E402,F401
from tests.test_pg_store import TEST_DATABASE_URL, base_de_prueba  # noqa: E402,F401


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="sin TEST_DATABASE_URL: el panel guarda en Postgres")
async def test_la_llave_de_una_plataforma_no_llega_a_logs_ni_respuestas(
    pool, respx_mock, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    llave = "sk-or-secreta-larga-1234"
    respx_mock.post("https://plataforma.test/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": f"la clave {llave} no vale aquí"}})
    )
    respuestas: list[str] = []
    with caplog.at_level(logging.DEBUG):
        async with _nea(pool) as nea:
            alta = await nea.pedir(
                "POST", "/panel/api/plataformas",
                {"nombre": "OpenRouter", "baseUrl": "https://plataforma.test/v1", "llave": llave},
            )
            pid = alta.json()["id"]
            prueba = await nea.pedir("POST", f"/panel/api/plataformas/{pid}/probar", {"modelo": "m1", "extras": {}})
            lista = await nea.pedir("GET", "/panel/api/plataformas")
            respuestas += [alta.text, prueba.text, lista.text]
    assert prueba.json()["motivo"] == "rechazo"
    assert lista.json()[0]["ultimoError"]  # el error quedó anotado, saneado
    assert lista.json()[0]["llave"] == "…1234"
    assert llave not in caplog.text
    assert all(llave not in texto for texto in respuestas)
