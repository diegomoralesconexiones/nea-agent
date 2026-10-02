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
import pytest
import respx

import app.dispatch as dispatch
from app import main
from app.cadena import LlmCadena
from app.config import Settings
from app.configuracion import Preparacion
from app.dispatch import _contexto_una_organizacion
from app.llm import OpenAiLlm
from app.multiorg import LlmSinLlave
from app.profile import BusinessProfile
from app.state import AppContext, MemoryStore
from tests.test_multiorg import _StoreDePrueba, _WorkerQuieto

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


# ── 025: la configuración del panel (T042) ───────────────────────────────

PERFIL_VIGENTE = BusinessProfile(cloud=True, agent_name="Sofi", instructions="Vende por caja.")
CADENA = LlmCadena([])
PAYLOAD = {"organization": {"id": "org_uno", "slug": "principal"}, "conversation": {"id": CONV}}


@pytest.fixture
def vigente(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """`preparar_contexto` falso: anota con qué se llamó y devuelve `respuesta`."""
    llamadas: list[dict[str, Any]] = [{"respuesta": Preparacion()}]

    async def preparar(pool: Any, organization_id: str, context: dict[str, Any], settings: Any, *, es_prueba: bool = False) -> Preparacion:
        llamadas.append({"org": organization_id, "context": context, "es_prueba": es_prueba})
        return llamadas[0]["respuesta"]

    monkeypatch.setattr(dispatch, "preparar_contexto", preparar)
    return llamadas


async def test_con_eslabones_certificados_piensa_con_la_cadena(vigente: list[dict[str, Any]]) -> None:
    vigente[0]["respuesta"] = Preparacion(perfil=PERFIL_VIGENTE, cadena=CADENA)
    # Aunque el contexto todavía sea v1 con `llm`: Nea habla directo (FR-046).
    ctx = _ctx(CrmConContexto({"contractVersion": 1, "llm": {"path": "/api/brains/llm"}}))

    turno = await _contexto_una_organizacion(ctx, CONV, PAYLOAD)

    assert turno is not None and turno.llm is CADENA
    assert await turno.profile.get() == PERFIL_VIGENTE
    assert vigente[1]["org"] == "org_uno" and vigente[1]["es_prueba"] is False


async def test_v2_sin_eslabones_certificados_pasa_a_humano(vigente: list[dict[str, Any]]) -> None:
    vigente[0]["respuesta"] = Preparacion(perfil=PERFIL_VIGENTE, cadena=None)
    crm = CrmConContexto({"contractVersion": 2})

    assert await _contexto_una_organizacion(_ctx(crm), CONV, PAYLOAD) is None
    assert crm.handoffs == [(CONV, "sin_credencial")]


async def test_v2_sin_version_vigente_pasa_a_humano(vigente: list[dict[str, Any]]) -> None:
    crm = CrmConContexto({"contractVersion": 2})
    assert await _contexto_una_organizacion(_ctx(crm), CONV, PAYLOAD) is None
    assert crm.handoffs == [(CONV, "sin_credencial")]


async def test_transicion_v1_sin_cadena_piensa_por_el_crm_con_el_perfil_vigente(
    vigente: list[dict[str, Any]],
) -> None:
    vigente[0]["respuesta"] = Preparacion(perfil=PERFIL_VIGENTE, cadena=None)
    ctx = _ctx(CrmConContexto({"contractVersion": 1, "llm": {"path": "/api/brains/llm", "model": "a/b"}}))

    turno = await _contexto_una_organizacion(ctx, CONV, PAYLOAD)

    assert turno is not None and isinstance(turno.llm, OpenAiLlm)
    assert str(turno.llm._client.base_url).startswith(f"{CRM_URL}/api/brains/llm")
    assert await turno.profile.get() == PERFIL_VIGENTE


async def test_el_turno_de_prueba_pide_la_cadena_de_prueba(vigente: list[dict[str, Any]]) -> None:
    vigente[0]["respuesta"] = Preparacion(perfil=PERFIL_VIGENTE, cadena=CADENA)
    payload = {**PAYLOAD, "conversation": {"id": CONV, "isTest": True}}

    await _contexto_una_organizacion(_ctx(CrmConContexto({"contractVersion": 2})), CONV, payload)

    assert vigente[1]["es_prueba"] is True


async def test_sin_organizacion_en_el_despacho_usa_la_de_la_instancia(vigente: list[dict[str, Any]]) -> None:
    await _contexto_una_organizacion(_ctx(CrmConContexto({"contractVersion": 1})), CONV, {})
    assert vigente[1]["org"] == "principal"


async def test_la_atencion_se_mezcla_en_el_contexto_que_usa_el_turno(vigente: list[dict[str, Any]]) -> None:
    contexto = {"contractVersion": 1}
    await _contexto_una_organizacion(_ctx(CrmConContexto(contexto)), CONV, PAYLOAD)
    assert vigente[1]["context"] is contexto  # el mismo objeto que consume get_context


@pytest.fixture
def arranque_wline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Como la levanta docker-compose.nea.dev.yml: un negocio, sin llave propia."""
    monkeypatch.setenv("VOCERO_MODE", "cloud")
    monkeypatch.setenv("CRM_ORGANIZATION", "principal")
    monkeypatch.setenv("CRM_BASE_URL", CRM_URL)
    monkeypatch.setenv("CRM_BRAIN_SECRET", SECRETO)
    monkeypatch.setenv("DATABASE_URL", "postgres://no-se-usa")
    for ausente in ("LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(ausente, "")
    monkeypatch.setattr(main, "PgStore", lambda *_a, **_k: _StoreDePrueba())
    monkeypatch.setattr(main, "RelayWorker", _WorkerQuieto)
    monkeypatch.setattr(main, "FollowupWorker", _WorkerQuieto)
    monkeypatch.setattr(main, "SenderWorker", _WorkerQuieto)


@respx.mock
async def test_sin_llave_propia_la_nea_de_un_negocio_arranca(arranque_wline: None) -> None:
    """El fallo que tumbaba el contenedor: el SDK no se construye sin llave."""
    respx.route(url__startswith=CRM_URL).mock(return_value=httpx.Response(404))
    app = main.create_app()
    async with app.router.lifespan_context(app):
        ctx = app.state.ctx
        assert not ctx.settings.multi_org
        assert isinstance(ctx.llm, LlmSinLlave)
        # Y si algún camino lo usara, revienta con el motivo, no finge un fallo
        # del proveedor.
        with pytest.raises(RuntimeError, match="sin llave"):
            await ctx.llm.complete([{"role": "user", "content": "hola"}])


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
