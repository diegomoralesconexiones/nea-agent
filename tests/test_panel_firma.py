"""Wline (025, US1): la firma con que Wline reenvía cada petición al panel.

contracts/panel.md §2. Nea no tiene sesión propia ni acceso público: confía en
la petición solo si trae la firma de Wline (HMAC-SHA256 con `CRM_BRAIN_SECRET`,
que es `BOT_API_KEY` en Wline) sobre método, ruta, resumen del cuerpo y la
cabecera de identidad, con marca de tiempo de ±60 s y rol Propietario o Admin.

El resumen del cuerpo se lee de `x-contenido-sha256` (research-fase2 F1): la
firma se verifica solo con cabeceras y cada ruta compara su cuerpo al leerlo.

El vector fijo es el MISMO que `tests/unit/panel-proxy.test.ts` de Wline: si
uno de los dos lados cambia la forma de firmar, los dos tests lo dicen.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest

from app import panel
from app.main import create_app
from tests.conftest import make_ctx, make_settings

# ── El vector compartido con Wline ────────────────────────────────────────

SECRETO = "clave-de-servicio-larga-0123456789abcdef"
METODO = "PUT"
RUTA = "/panel/api/configuracion?x=1"
CUERPO = b'{"base":1,"secciones":{"instrucciones":"Hola"}}'
CABECERA = "eyJ1IjoidXNyXzEiLCJlIjoiZHVlbmFAZXhhbXBsZS5jb20iLCJyIjoib3duZXIiLCJvIjoib3JnXzEiLCJ0IjoxNzAwMDAwMDAwfQ"
FIRMA = "32723b3ef4381ccc591cc9745283683c478f387c101cab0a4248cb6518a48010"
T = 1700000000

CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"


def test_el_vector_compartido_con_wline() -> None:
    resumen = hashlib.sha256(CUERPO).hexdigest()
    assert panel.firma_esperada(SECRETO, METODO, RUTA, resumen, CABECERA) == FIRMA
    relleno = "=" * (-len(CABECERA) % 4)
    assert json.loads(base64.urlsafe_b64decode(CABECERA + relleno)) == {
        "u": "usr_1", "e": "duena@example.com", "r": "owner", "o": "org_1", "t": T,
    }


# ── Por HTTP ──────────────────────────────────────────────────────────────


def _b64url(datos: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(datos).encode()).decode().rstrip("=")


def firmar(
    metodo: str,
    ruta: str,
    cuerpo: bytes = b"",
    *,
    r: str = "owner",
    t: int = T,
    secreto: str = SECRETO,
) -> dict[str, str]:
    cabecera = _b64url({"u": "usr_1", "e": "duena@example.com", "r": r, "o": "org_1", "t": t})
    resumen = hashlib.sha256(cuerpo).hexdigest()
    return {
        "x-wline-panel": cabecera,
        "x-wline-panel-firma": panel.firma_esperada(secreto, metodo, ruta, resumen, cabecera),
        "x-contenido-sha256": resumen,
    }


@pytest.fixture
def reloj(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    ahora = [float(T)]
    monkeypatch.setattr(panel, "_ahora", lambda: ahora[0])
    return ahora


@pytest.fixture
async def cliente(reloj: list[float]):
    ctx = make_ctx(make_settings(vocero_mode="cloud", crm_organization="principal", crm_brain_secret=SECRETO))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://nea.test"
    ) as c:
        yield c
    await ctx.crm.aclose()


async def test_firma_valida_sirve_la_pagina_en_panel_y_panel_barra(cliente: httpx.AsyncClient) -> None:
    for ruta in ("/panel", "/panel/"):
        r = await cliente.get(ruta, headers=firmar("GET", ruta))
        assert r.status_code == 200, ruta  # sin redirigir: Next quita la barra
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["content-security-policy"] == CSP
    js = await cliente.get("/panel/static/panel.js", headers=firmar("GET", "/panel/static/panel.js"))
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    css = await cliente.get("/panel/static/panel.css", headers=firmar("GET", "/panel/static/panel.css"))
    assert css.status_code == 200
    otro = await cliente.get("/panel/static/../main.py", headers=firmar("GET", "/panel/static/../main.py"))
    assert otro.status_code == 404


async def test_sin_firma_o_sin_secreto_no_pasa(cliente: httpx.AsyncClient, reloj: list[float]) -> None:
    r = await cliente.get("/panel")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "firma_invalida"
    assert r.headers["content-security-policy"] == CSP

    ctx = make_ctx(make_settings(vocero_mode="cloud", crm_organization="principal", crm_brain_secret=""))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://nea.test"
    ) as c:
        assert (await c.get("/panel", headers=firmar("GET", "/panel", secreto=""))).status_code == 401
    await ctx.crm.aclose()


@pytest.mark.parametrize(
    "que",
    ["metodo", "ruta", "consulta", "cabecera", "resumen"],
)
async def test_cualquier_parte_alterada_da_401(cliente: httpx.AsyncClient, que: str) -> None:
    # Rutas que existen con los dos métodos: un método que la ruta no tiene
    # daría 405 antes de mirar la firma.
    cabeceras = firmar("GET", "/panel/api/plataformas")
    metodo, ruta = "GET", "/panel/api/plataformas"
    if que == "metodo":
        metodo = "POST"
    elif que == "ruta":
        ruta = "/panel/api/versiones"
    elif que == "consulta":
        ruta = "/panel/api/plataformas?x=1"
    elif que == "cabecera":
        cabeceras["x-wline-panel"] = _b64url(
            {"u": "usr_1", "e": "duena@example.com", "r": "admin", "o": "org_1", "t": T}
        )
    elif que == "resumen":
        cabeceras["x-contenido-sha256"] = hashlib.sha256(b"otro").hexdigest()
    r = await cliente.request(metodo, ruta, headers=cabeceras)
    assert r.status_code == 401


async def test_cuerpo_alterado_da_401_al_leerlo(cliente: httpx.AsyncClient) -> None:
    """La firma pasa (solo mira cabeceras), pero la ruta compara su cuerpo."""
    cabeceras = firmar("PUT", "/panel/api/configuracion", CUERPO)
    r = await cliente.put(
        "/panel/api/configuracion",
        content=CUERPO.replace(b"Hola", b"Adios"),
        headers={**cabeceras, "content-type": "application/json"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "firma_invalida"


@pytest.mark.parametrize(("desfase", "pasa"), [(-61, False), (61, False), (-59, True), (59, True)])
async def test_ventana_de_60_s(cliente: httpx.AsyncClient, reloj: list[float], desfase: int, pasa: bool) -> None:
    reloj[0] = T - desfase  # la petición se firmó en T; el reloj de Nea va desfasado
    r = await cliente.get("/panel", headers=firmar("GET", "/panel"))
    assert (r.status_code == 200) is pasa


@pytest.mark.parametrize("rol", ["member", "supervisor", "sistema", ""])
async def test_solo_owner_y_admin_entran_a_la_pagina(cliente: httpx.AsyncClient, rol: str) -> None:
    r = await cliente.get("/panel", headers=firmar("GET", "/panel", r=rol))
    assert r.status_code == 401


async def test_admin_entra(cliente: httpx.AsyncClient) -> None:
    assert (await cliente.get("/panel", headers=firmar("GET", "/panel", r="admin"))).status_code == 200


def test_sistema_solo_vale_en_el_juez() -> None:
    assert panel.rol_permitido("sistema", "GET", "/panel/api/juez")
    assert not panel.rol_permitido("sistema", "POST", "/panel/api/juez")
    assert not panel.rol_permitido("sistema", "GET", "/panel/api/estado")
    assert not panel.rol_permitido("sistema", "GET", "/panel")
    for rol in ("owner", "admin"):
        assert panel.rol_permitido(rol, "GET", "/panel/api/estado")
        assert panel.rol_permitido(rol, "PUT", "/panel/api/configuracion")
    assert not panel.rol_permitido("member", "GET", "/panel/api/estado")


async def test_compara_en_tiempo_constante(cliente: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def espia(a: Any, b: Any) -> bool:
        llamadas.append((a, b))
        return original(a, b)

    monkeypatch.setattr(panel.hmac, "compare_digest", espia)
    await cliente.get("/panel", headers=firmar("GET", "/panel"))
    assert llamadas


async def test_la_api_no_se_guarda_en_cache(cliente: httpx.AsyncClient) -> None:
    r = await cliente.get("/panel/api/nada", headers=firmar("GET", "/panel/api/nada"))
    # Una ruta que no existe responde 404; las que existen llevan no-store
    # (lo comprueban las pruebas de publicar). Aquí: el 401 también lleva CSP.
    assert r.status_code == 404
    r = await cliente.get("/panel/api/versiones", headers=firmar("GET", "/panel/api/versiones", r="member"))
    assert r.status_code == 401
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-security-policy"] == CSP
