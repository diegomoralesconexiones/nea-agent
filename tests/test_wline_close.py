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
from app.cadena import Eslabon, LlmCadena, Plataforma, restante
from app.config import Settings
from app.crm import CrmConflict, CrmUnreachable
from app.crm_brains import BrainsCrmClient
from app.dispatch_worker import drain_one
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
async def test_cierra_con_la_cadena(cliente: BrainsCrmClient) -> None:
    """025 (R16): qué plataforma y modelo respondieron, y los saltos."""
    ruta = respx.post(f"{CRM_URL}/api/brains/dispatch/dsp_1/close").mock(
        return_value=httpx.Response(200, json={})
    )
    cadena = {
        "plataforma": "OpenAI", "modelo": "gpt-6-luna", "modeloRespondio": "gpt-6-luna",
        "saltos": [{"plataforma": "OpenRouter", "modelo": "deepseek/x", "tipo": "plataforma"}],
    }
    await cliente.cerrar_despacho("dsp_1", 900, None, cadena)
    assert json.loads(ruta.calls.last.request.content) == {"providerMs": 900, "error": None, "cadena": cadena}


@respx.mock
async def test_la_cadena_se_ajusta_al_contrato(cliente: BrainsCrmClient) -> None:
    """Wline exige los tres nombres (≤ 120) y ≤ 10 saltos; si nadie respondió,
    los nombres van vacíos y los saltos igual cuentan qué pasó."""
    ruta = respx.post(f"{CRM_URL}/api/brains/dispatch/dsp_1/close").mock(
        return_value=httpx.Response(200, json={})
    )
    saltos = [{"plataforma": "P" * 200, "modelo": "m", "tipo": "modelo"}] * 12
    await cliente.cerrar_despacho("dsp_1", None, "LlmExhausted", {"saltos": saltos})

    enviada = json.loads(ruta.calls.last.request.content)["cadena"]
    assert (enviada["plataforma"], enviada["modelo"], enviada["modeloRespondio"]) == ("", "", "")
    assert len(enviada["saltos"]) == 10
    assert enviada["saltos"][0] == {"plataforma": "P" * 120, "modelo": "m", "tipo": "modelo"}


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
        self.cadenas: list[dict[str, Any] | None] = []

    def registrar(self, identity: str, conversation_id: str) -> None:
        pass

    async def precargar(self, conversation_id: str) -> dict[str, Any] | None:
        if self.revienta:
            raise RuntimeError("el CRM se cayó a media precarga")
        return None

    async def cerrar_despacho(
        self, dispatch_id: str, provider_ms: int | None, error: str | None,
        cadena: dict[str, Any] | None = None,
    ) -> None:
        self.cierres.append((dispatch_id, provider_ms, error))
        self.cadenas.append(cadena)


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


async def test_el_turno_tiene_90_s_desde_el_despacho(monkeypatch: pytest.MonkeyPatch) -> None:
    plazos: list[float | None] = []

    async def handle_flush(ctx: AppContext, identity: str, items: list[Any], **_: Any) -> None:
        plazos.append(restante())

    monkeypatch.setattr(dispatch, "handle_flush", handle_flush)
    await dispatch._procesar(_ctx(CrmQueAnota()), _payload())
    assert plazos[0] is not None and 89 < plazos[0] <= 90


async def test_el_cierre_lleva_la_traza_de_la_cadena(monkeypatch: pytest.MonkeyPatch) -> None:
    class Respondo:
        async def complete(self, messages: Any, tools: Any = None) -> LlmReply:
            return LlmReply(content="hola", model="m-0731")

    cadena = LlmCadena([Eslabon(Plataforma("pl_a", "OpenRouter", "http://x", "sk-x"), "m", Respondo())])

    async def handle_flush(ctx: AppContext, identity: str, items: list[Any], **_: Any) -> None:
        await cadena.complete([])

    monkeypatch.setattr(dispatch, "handle_flush", handle_flush)
    crm = CrmQueAnota()
    await dispatch._procesar(_ctx(crm), _payload())
    assert crm.cadenas == [
        {"plataforma": "OpenRouter", "modelo": "m", "modeloRespondio": "m-0731", "saltos": []}
    ]


async def test_un_turno_sin_modelo_cierra_sin_latencia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handle_flush(ctx: AppContext, identity: str, items: list[Any], **_: Any) -> None:
        pass

    monkeypatch.setattr(dispatch, "handle_flush", handle_flush)
    crm = CrmQueAnota()
    await dispatch._procesar(_ctx(crm), _payload())
    assert crm.cierres == [("dsp_1", None, None)]
    assert crm.cadenas == [None]  # sin cadena (v1 por el CRM), el cierre de siempre


async def test_un_turno_que_revienta_cierra_igual_con_el_error() -> None:
    crm = CrmQueAnota(revienta=True)
    await dispatch._procesar(_ctx(crm), _payload())  # no propaga

    assert len(crm.cierres) == 1
    _, _, error = crm.cierres[0]
    assert error is not None and "RuntimeError" in error


async def test_en_el_inbox_durable_cierra_y_ademas_pasa_a_humano() -> None:
    """Merge con upstream (despacho durable): el cierre de Wline (N7) y el pase
    a humano del worker conviven; el turno no se repite."""

    class CrmConHandoff(CrmQueAnota):
        def __init__(self) -> None:
            super().__init__(revienta=True)
            self.handoffs: list[tuple[str, str]] = []

        async def post_handoff(self, conversation_id: str, motivo: str) -> None:
            self.handoffs.append((conversation_id, motivo))

    crm = CrmConHandoff()
    ctx = _ctx(crm)
    await ctx.store.enqueue_dispatch("principal", _payload())

    assert await drain_one(ctx)

    assert len(crm.cierres) == 1 and "RuntimeError" in (crm.cierres[0][2] or "")
    assert crm.handoffs == [(CONV, "error")]
    assert not await drain_one(ctx)


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


# ── 025: lo que el panel le pide a Wline (contracts/panel.md §5) ─────────


@respx.mock
async def test_automation_get_con_el_secreto(cliente: BrainsCrmClient) -> None:
    ruta = respx.get(f"{CRM_URL}/api/brains/automation").mock(
        return_value=httpx.Response(200, json={"appliedVersion": 12, "lines": []})
    )
    assert await cliente.automation_get() == {"appliedVersion": 12, "lines": []}
    pedido = ruta.calls.last.request
    assert pedido.headers["authorization"] == f"Bearer {SECRETO}"
    assert pedido.headers["x-vocero-organization"] == "principal"


@respx.mock
async def test_automation_put_manda_version_y_reglas(cliente: BrainsCrmClient) -> None:
    ruta = respx.put(f"{CRM_URL}/api/brains/automation").mock(
        return_value=httpx.Response(200, json={"appliedVersion": 13})
    )
    reglas = {"fichaFields": [], "threshold": "Clasificado", "capabilities": {}, "lineCriteria": {}}
    assert await cliente.automation_put(13, reglas) == {"appliedVersion": 13}
    assert json.loads(ruta.calls.last.request.content) == {"version": 13, "rules": reglas}


@pytest.mark.parametrize(
    "estado, codigo",
    [(409, "version_vieja"), (422, "umbral_invalido")],
)
@respx.mock
async def test_automation_put_rechazado_trae_codigo_y_mensaje(
    cliente: BrainsCrmClient, estado: int, codigo: str
) -> None:
    cuerpo = {"error": {"code": codigo, "message": "El umbral no existe en todas las líneas"}}
    respx.put(f"{CRM_URL}/api/brains/automation").mock(return_value=httpx.Response(estado, json=cuerpo))
    with pytest.raises(CrmConflict) as exc:
        await cliente.automation_put(13, {})
    assert exc.value.code == codigo
    assert exc.value.payload == cuerpo  # el panel muestra el mensaje tal cual (FR-017)


@respx.mock
async def test_automation_caido_o_sin_red_es_crm_unreachable(cliente: BrainsCrmClient) -> None:
    respx.get(f"{CRM_URL}/api/brains/automation").mock(return_value=httpx.Response(503))
    with pytest.raises(CrmUnreachable):
        await cliente.automation_get()
    respx.put(f"{CRM_URL}/api/brains/automation").mock(side_effect=httpx.ReadTimeout("vencido"))
    with pytest.raises(CrmUnreachable):
        await cliente.automation_put(13, {})  # el panel relee appliedVersion (R5)


@respx.mock
async def test_laboratorio_iniciar_y_consultar(cliente: BrainsCrmClient) -> None:
    respx.post(f"{CRM_URL}/api/brains/lab/runs").mock(return_value=httpx.Response(202, json={"runId": "run_1"}))
    estado = {"status": "running", "progress": {"done": 1, "total": 7}, "score": None, "cases": []}
    respx.get(f"{CRM_URL}/api/brains/lab/runs/run_1").mock(return_value=httpx.Response(200, json=estado))

    assert await cliente.lab_run_start() == "run_1"
    assert await cliente.lab_run_get("run_1") == estado


@respx.mock
async def test_laboratorio_ocupado(cliente: BrainsCrmClient) -> None:
    respx.post(f"{CRM_URL}/api/brains/lab/runs").mock(
        return_value=httpx.Response(409, json={"error": {"code": "run_in_progress", "message": "Ya hay una corrida"}})
    )
    with pytest.raises(CrmConflict) as exc:
        await cliente.lab_run_start()
    assert exc.value.code == "run_in_progress"


# ── 025: el perfil del contexto queda solo para v1 ───────────────────────


@pytest.mark.parametrize(
    "contexto, con_perfil",
    [
        ({"contractVersion": 1, "agent": {"name": "Sofi"}, "knowledge": []}, True),
        ({"agent": {"name": "Sofi"}}, True),  # el CRM de origen, sin versión
        ({"contractVersion": 2}, False),  # v2: el perfil vive en Nea
    ],
)
@respx.mock
async def test_el_perfil_del_contexto_solo_en_v1(
    cliente: BrainsCrmClient, contexto: dict[str, Any], con_perfil: bool
) -> None:
    respx.get(f"{CRM_URL}/api/brains/context").mock(return_value=httpx.Response(200, json=contexto))
    cliente.registrar(IDENTITY, CONV)

    await cliente.precargar(CONV)
    assert (await cliente.get_profile() is not None) is con_perfil

    await cliente.get_context(IDENTITY)  # consume la precarga
    await cliente.get_context(IDENTITY)  # y vuelve a pedir
    assert (await cliente.get_profile() is not None) is con_perfil


# ── 026: el silencio deliberado viaja en el cierre (contracts/fase-a §3.1) ──
#
# Tras la despedida del cierre «sin rumbo», el relleno no se contesta. Wline
# trata un turno sin mensaje como una falla y pasa la conversación a una
# persona; para que no lo haga aquí, el cierre dice que el silencio fue a
# propósito.


@respx.mock
async def test_el_cierre_lleva_el_silencio_deliberado(cliente: BrainsCrmClient) -> None:
    ruta = respx.post(f"{CRM_URL}/api/brains/dispatch/dsp_1/close").mock(
        return_value=httpx.Response(200, json={"outcome": "modelo_sin_accion"})
    )
    await cliente.cerrar_despacho("dsp_1", None, None, silencio="sin_rumbo")

    assert json.loads(ruta.calls.last.request.content) == {
        "providerMs": None, "error": None, "silencio": "sin_rumbo",
    }


@respx.mock
async def test_sin_silencio_el_cierre_no_cambia(cliente: BrainsCrmClient) -> None:
    ruta = respx.post(f"{CRM_URL}/api/brains/dispatch/dsp_1/close").mock(
        return_value=httpx.Response(200, json={})
    )
    await cliente.cerrar_despacho("dsp_1", 10, None)

    assert "silencio" not in json.loads(ruta.calls.last.request.content)


class CrmQueAnotaElSilencio(CrmQueAnota):
    def __init__(self) -> None:
        super().__init__()
        self.silencios: list[str | None] = []

    async def cerrar_despacho(  # type: ignore[override]
        self, dispatch_id: str, provider_ms: int | None, error: str | None,
        cadena: dict[str, Any] | None = None, silencio: str | None = None,
    ) -> None:
        await super().cerrar_despacho(dispatch_id, provider_ms, error, cadena)
        self.silencios.append(silencio)


async def test_el_despacho_cierra_con_lo_que_marco_el_turno(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import wline

    async def handle_flush(ctx: AppContext, identity: str, items: list[Any], **_: Any) -> None:
        wline.marcar_silencio("sin_rumbo")

    monkeypatch.setattr(dispatch, "handle_flush", handle_flush)
    crm = CrmQueAnotaElSilencio()
    await dispatch._procesar(_ctx(crm), _payload())

    assert crm.silencios == ["sin_rumbo"]


async def test_un_turno_que_no_marca_cierra_como_siempre(turno_con_modelo: None) -> None:
    """Sin marca no se manda el argumento: los CRM que no lo conocen siguen igual."""
    crm = CrmQueAnota()  # su cerrar_despacho no acepta `silencio`
    await dispatch._procesar(_ctx(crm), _payload())

    assert len(crm.cierres) == 1


async def test_la_marca_de_un_turno_no_pasa_al_siguiente(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import wline

    veces = {"n": 0}

    async def handle_flush(ctx: AppContext, identity: str, items: list[Any], **_: Any) -> None:
        veces["n"] += 1
        if veces["n"] == 1:
            wline.marcar_silencio("sin_rumbo")

    monkeypatch.setattr(dispatch, "handle_flush", handle_flush)
    crm = CrmQueAnotaElSilencio()
    await dispatch._procesar(_ctx(crm), _payload("dsp_1"))
    await dispatch._procesar(_ctx(crm), _payload("dsp_2"))

    assert crm.silencios == ["sin_rumbo", None]


async def test_el_relleno_tras_el_cierre_sin_rumbo_marca_el_silencio() -> None:
    """El camino real del turno: conversación ya despedida, llega un «gracias»."""
    from app import wline
    from app.state import utcnow
    from app.turn import handle_flush

    ctx = _ctx(CrmQueAnota())
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(conv.id, crm_conversation_id=CONV, stalled_at=utcnow())
    payload = _payload()
    payload["messages"] = [{"id": "msg_9", "type": "text", "text": "gracias"}]

    marca = wline.medir_silencio()
    await handle_flush(ctx, IDENTITY, dispatch.mensajes_del_despacho(payload), crm_conversation_id=CONV)

    assert marca == ["sin_rumbo"]


async def test_un_mensaje_con_contenido_no_marca_silencio() -> None:
    from app import wline
    from app.state import utcnow
    from app.turn import handle_flush

    ctx = _ctx(CrmQueAnota())
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(conv.id, crm_conversation_id=CONV, stalled_at=utcnow())
    payload = _payload()
    payload["messages"] = [{"id": "msg_9", "type": "text", "text": "¿cuánto cuesta el envío?"}]

    marca = wline.medir_silencio()
    await handle_flush(ctx, IDENTITY, dispatch.mensajes_del_despacho(payload), crm_conversation_id=CONV)

    assert marca == []
