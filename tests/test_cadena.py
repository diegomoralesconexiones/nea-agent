"""Wline (025, US4): la cadena de respaldo y «Probar conexión».

Reglas de salto (R6, FR-044), con clientes falsos:

- plataforma (401/402/403, red, tiempo agotado): se saltan TODOS los eslabones
  de esa plataforma en el turno, sin reintento;
- modelo (404, 400 y demás 4xx): siguiente eslabón, sin reintento;
- pasajero (408/429/5xx): un reintento corto y luego el siguiente;
- vacía: siguiente eslabón.

20 s por intento acotados a lo que queda del turno (90 s), `LlmExhausted` si
todo falla, la traza para el cierre y F15: si la plataforma responde con otro
modelo que el certificado, el eslabón queda «requiere atención».
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

import app.cadena as cadena_mod
from app.cadena import (
    PRESUPUESTO_TURNO,
    TOPE_INTENTO,
    Eslabon,
    LlmCadena,
    Plataforma,
    iniciar_turno,
    probar_conexion,
    restante,
    sanear,
)
from app.llm import LlmExhausted, LlmReply, OpenAiLlm

BASE = "http://plataforma.test/v1"
LLAVE = "sk-proj-abc123XYZ7890"


@pytest.fixture(autouse=True)
def reintento_sin_espera(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cadena_mod, "ESPERA_REINTENTO", 0)


class LlmFalso:
    """Un eslabón con guion: cada llamada consume un paso (respuesta, error o colgarse)."""

    def __init__(self, *guion: Any) -> None:
        self.guion = list(guion)
        self.llamadas = 0

    async def complete(self, messages: list[dict[str, Any]], tools: Any = None) -> LlmReply:
        self.llamadas += 1
        paso = self.guion.pop(0) if self.guion else LlmReply(content="ok", model="m")
        if paso == "colgar":
            await asyncio.sleep(60)
        if isinstance(paso, Exception):
            raise paso
        return paso


class PoolFalso:
    def __init__(self) -> None:
        self.ejecutados: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.ejecutados.append((" ".join(sql.split()), args))
        return "UPDATE 1"


def _e(pid: str, modelo: str, llm: Any, certificado: str | None = None) -> Eslabon:
    return Eslabon(Plataforma(pid, f"Plat {pid}", BASE, LLAVE), modelo, llm, modelo_certificado=certificado)


def _fallo(tipo: str) -> LlmExhausted:
    return LlmExhausted(f"fallo {tipo}", tipo=tipo)


def _salto(pid: str, modelo: str, tipo: str) -> dict[str, str]:
    return {"plataforma": f"Plat {pid}", "modelo": modelo, "tipo": tipo}


# ── Saltos por tipo de error ──────────────────────────────────────────────


async def test_plataforma_inalcanzable_se_intenta_una_sola_vez() -> None:
    a1, a2 = LlmFalso(_fallo("plataforma")), LlmFalso()
    b = LlmFalso(LlmReply(content="hola", model="mb"), LlmReply(content="otra", model="mb"))
    cadena = LlmCadena([_e("pl_a", "m1", a1), _e("pl_a", "m2", a2), _e("pl_b", "mb", b)])
    traza = iniciar_turno()

    respuesta = await cadena.complete([])

    assert respuesta.content == "hola"
    assert (a1.llamadas, a2.llamadas, b.llamadas) == (1, 0, 1)
    assert traza == {
        "plataforma": "Plat pl_b",
        "modelo": "mb",
        "modeloRespondio": "mb",
        "saltos": [_salto("pl_a", "m1", "plataforma")],
    }
    # La siguiente llamada del MISMO turno ya no vuelve a la plataforma caída.
    await cadena.complete([])
    assert (a1.llamadas, a2.llamadas, b.llamadas) == (1, 0, 2)


async def test_modelo_inexistente_pasa_al_siguiente_sin_reintento() -> None:
    a1, a2 = LlmFalso(_fallo("modelo")), LlmFalso(LlmReply(content="ok", model="m2"))
    traza = iniciar_turno()
    await LlmCadena([_e("pl_a", "m1", a1), _e("pl_a", "m2", a2)]).complete([])
    assert (a1.llamadas, a2.llamadas) == (1, 1)  # misma plataforma: sí se intenta
    assert traza["saltos"] == [_salto("pl_a", "m1", "modelo")]


async def test_pasajero_un_reintento_y_luego_el_siguiente() -> None:
    a, b = LlmFalso(_fallo("pasajero"), _fallo("pasajero")), LlmFalso()
    traza = iniciar_turno()
    await LlmCadena([_e("pl_a", "m1", a), _e("pl_b", "m2", b)]).complete([])
    assert (a.llamadas, b.llamadas) == (2, 1)
    assert traza["saltos"] == [_salto("pl_a", "m1", "pasajero")]


async def test_pasajero_que_se_recupera_no_salta() -> None:
    a = LlmFalso(_fallo("pasajero"), LlmReply(content="ya", model="m1"))
    traza = iniciar_turno()
    assert (await LlmCadena([_e("pl_a", "m1", a)]).complete([])).content == "ya"
    assert traza["saltos"] == []


async def test_respuesta_vacia_pasa_al_siguiente() -> None:
    a, b = LlmFalso(_fallo("vacia")), LlmFalso()
    traza = iniciar_turno()
    await LlmCadena([_e("pl_a", "m1", a), _e("pl_a", "m2", b)]).complete([])
    assert (a.llamadas, b.llamadas) == (1, 1)
    assert traza["saltos"] == [_salto("pl_a", "m1", "vacia")]


async def test_toda_la_cadena_agotada_es_llm_exhausted() -> None:
    traza = iniciar_turno()
    cadena = LlmCadena([_e("pl_a", "m1", LlmFalso(_fallo("modelo"))), _e("pl_b", "m2", LlmFalso(_fallo("plataforma")))])
    with pytest.raises(LlmExhausted):
        await cadena.complete([])
    assert [s["tipo"] for s in traza["saltos"]] == ["modelo", "plataforma"]
    assert "plataforma" not in traza  # nadie respondió


async def test_sin_eslabones_es_llm_exhausted() -> None:
    with pytest.raises(LlmExhausted):
        await LlmCadena([]).complete([])


# ── Tiempos ───────────────────────────────────────────────────────────────


async def test_el_turno_tiene_90_s_y_cada_intento_20() -> None:
    assert (PRESUPUESTO_TURNO, TOPE_INTENTO) == (90.0, 20.0)
    iniciar_turno()
    queda = restante()
    assert queda is not None and 89 < queda <= 90


async def test_fuera_de_un_turno_no_hay_plazo() -> None:
    async def sin_turno() -> float | None:
        return restante()

    assert await asyncio.create_task(sin_turno()) is None


async def test_el_intento_se_corta_con_lo_que_queda_del_turno() -> None:
    colgado, b = LlmFalso("colgar"), LlmFalso()
    iniciar_turno(0.2)
    inicio = time.monotonic()
    with pytest.raises(LlmExhausted):
        await LlmCadena([_e("pl_a", "m1", colgado), _e("pl_b", "m2", b)]).complete([])
    assert time.monotonic() - inicio < 2
    assert b.llamadas == 0  # sin presupuesto no se empieza otro eslabón


async def test_turnos_concurrentes_no_comparten_traza() -> None:
    async def turno(modelo: str) -> dict[str, Any]:
        traza = iniciar_turno()
        await LlmCadena([_e("pl_a", modelo, LlmFalso(LlmReply(content="x", model=modelo)))]).complete([])
        return traza

    t1, t2 = await asyncio.gather(asyncio.create_task(turno("uno")), asyncio.create_task(turno("dos")))
    assert (t1["modelo"], t2["modelo"]) == ("uno", "dos")


# ── F15 y FR-045 ──────────────────────────────────────────────────────────


async def test_otro_modelo_que_el_certificado_requiere_atencion() -> None:
    pool = PoolFalso()
    llm = LlmFalso(LlmReply(content="x", model="m-0801"), LlmReply(content="y", model="m-0801"))
    eslabon = _e("pl_a", "m", llm, certificado="m-0731")
    cadena = LlmCadena([eslabon], pool=pool, organization_id="org_1")

    await cadena.complete([])
    await cadena.complete([])

    marcas = [a for sql, a in pool.ejecutados if "requiere_atencion" in sql]
    assert marcas == [("org_1", "pl_a", "m")]  # una vez, no en cada llamada
    assert eslabon.requiere_atencion is True


async def test_el_mismo_modelo_no_marca_nada() -> None:
    pool = PoolFalso()
    cadena = LlmCadena(
        [_e("pl_a", "m", LlmFalso(LlmReply(content="x", model="m-0731")), certificado="m-0731")],
        pool=pool,
        organization_id="org_1",
    )
    await cadena.complete([])
    assert not [sql for sql, _ in pool.ejecutados if "requiere_atencion" in sql]


async def test_el_agente_anota_exito_y_error_de_cada_plataforma() -> None:
    pool = PoolFalso()
    fallo = LlmExhausted(f"Error code: 401 - Incorrect API key provided: {LLAVE}", tipo="plataforma")
    cadena = LlmCadena(
        [_e("pl_a", "m1", LlmFalso(fallo)), _e("pl_b", "m2", LlmFalso())], pool=pool, organization_id="org_1"
    )
    iniciar_turno()
    await cadena.complete([])

    error = next(a for sql, a in pool.ejecutados if "ultimo_error =" in sql)
    exito = next(a for sql, a in pool.ejecutados if "ultimo_exito_en = now()" in sql)
    assert error[:2] == ("org_1", "pl_a") and LLAVE not in error[2] and len(error[2]) <= 300
    assert exito == ("org_1", "pl_b")


async def test_la_cadena_de_prueba_anota_el_modelo_que_respondio() -> None:
    """Bloque 5 (D1): la certificación guarda el modelo que respondió (F15)."""
    pool = PoolFalso()
    llm = LlmFalso(LlmReply(content="x", model="m-0731"))
    await LlmCadena([_e("pl_a", "m", llm)], pool=pool, organization_id="org_1", prueba=True).complete([])
    anotados = [a for sql, a in pool.ejecutados if "cadena_prueba" in sql]
    assert anotados == [("org_1", "m-0731")]
    assert "SET modelo_respondio = $2" in next(sql for sql, _ in pool.ejecutados if "cadena_prueba" in sql)


async def test_la_cadena_de_los_clientes_no_toca_la_de_prueba() -> None:
    pool = PoolFalso()
    await LlmCadena([_e("pl_a", "m", LlmFalso())], pool=pool, organization_id="org_1").complete([])
    assert not [sql for sql, _ in pool.ejecutados if "cadena_prueba" in sql]


async def test_un_fallo_de_la_base_no_tumba_el_turno() -> None:
    class PoolCaido:
        async def execute(self, *_a: Any) -> None:
            raise RuntimeError("la base no contesta")

    cadena = LlmCadena(
        [_e("pl_a", "m", LlmFalso(LlmReply(content="x", model="otro")), certificado="m")],
        pool=PoolCaido(),
        organization_id="org_1",
    )
    assert (await cadena.complete([])).content == "x"


async def test_la_cadena_no_transcribe() -> None:
    with pytest.raises(LlmExhausted):
        await LlmCadena([_e("pl_a", "m", LlmFalso())]).transcribe(b"OggS", "audio/ogg")


# ── El cliente de verdad: cómo se clasifica cada error (T040) ─────────────


def _completion(model: str = "m-0731", content: str | None = "hola", tool_calls: Any = None) -> dict[str, Any]:
    mensaje: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        mensaje["tool_calls"] = tool_calls
    return {"id": "x", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "message": mensaje, "finish_reason": "stop"}]}


@pytest.mark.parametrize(
    "estado, tipo",
    [(401, "plataforma"), (402, "plataforma"), (403, "plataforma"),
     (404, "modelo"), (400, "modelo"), (422, "modelo"),
     (408, "pasajero"), (429, "pasajero"), (500, "pasajero"), (503, "pasajero")],
)
async def test_el_cliente_clasifica_por_codigo(respx_mock: Any, estado: int, tipo: str) -> None:
    ruta = respx_mock.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(estado, json={"error": {"message": "no"}})
    )
    with pytest.raises(LlmExhausted) as exc:
        await OpenAiLlm("k", "m", base_url=BASE, reintentos=0).complete([{"role": "user", "content": "hola"}])
    assert exc.value.tipo == tipo
    assert ruta.call_count == 1  # reintentos=0: la cadena decide


@pytest.mark.parametrize("error", [httpx.ConnectError("caída"), httpx.ReadTimeout("colgado")])
async def test_red_o_tiempo_es_la_plataforma(respx_mock: Any, error: Exception) -> None:
    respx_mock.post(f"{BASE}/chat/completions").mock(side_effect=error)
    with pytest.raises(LlmExhausted) as exc:
        await OpenAiLlm("k", "m", base_url=BASE, reintentos=0).complete([])
    assert exc.value.tipo == "plataforma"


async def test_vacia_y_el_modelo_que_respondio(respx_mock: Any) -> None:
    respx_mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json=_completion(content=None)))
    with pytest.raises(LlmExhausted) as exc:
        await OpenAiLlm("k", "m", base_url=BASE, reintentos=0).complete([])
    assert exc.value.tipo == "vacia"

    respx_mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json=_completion()))
    respuesta = await OpenAiLlm("k", "m", base_url=BASE, reintentos=0).complete([])
    assert respuesta.model == "m-0731"


async def test_los_extras_del_eslabon_viajan_en_el_cuerpo(respx_mock: Any) -> None:
    ruta = respx_mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json=_completion()))
    llm = OpenAiLlm("k", "gpt-6-luna", base_url=BASE, reintentos=0,
                    reasoning_effort="", provider_sort="", extra_body={"reasoning_effort": "none"})
    await llm.complete([])
    cuerpo = json.loads(ruta.calls.last.request.content)
    assert cuerpo["reasoning_effort"] == "none"
    assert "reasoning" not in cuerpo and "provider" not in cuerpo  # los de esta Nea, apagados


# ── «Probar conexión» (R9) ────────────────────────────────────────────────

PING = [{"id": "c1", "type": "function", "function": {"name": "ping", "arguments": '{"ok": true}'}}]


async def _probar(pool: PoolFalso | None = None, extras: dict[str, Any] | None = None) -> dict[str, Any]:
    return await probar_conexion(
        Plataforma("pl_a", "OpenRouter", BASE, LLAVE), "m", extras or {}, pool=pool, organization_id="org_1"
    )


async def test_probar_conexion_exitosa_pide_ping_y_anota_el_exito(respx_mock: Any) -> None:
    ruta = respx_mock.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion(content=None, tool_calls=PING))
    )
    pool = PoolFalso()

    resultado = await _probar(pool, {"reasoning_effort": "none"})

    assert resultado == {"ok": True, "modeloRespondio": "m-0731"}
    cuerpo = json.loads(ruta.calls.last.request.content)
    assert cuerpo["tool_choice"] == {"type": "function", "function": {"name": "ping"}}
    assert [t["function"]["name"] for t in cuerpo["tools"]] == ["ping"]
    assert cuerpo["reasoning_effort"] == "none"
    assert ruta.calls.last.request.headers["authorization"] == f"Bearer {LLAVE}"
    assert pool.ejecutados == [(
        "UPDATE plataforma_modelo SET ultimo_exito_en = now() WHERE organization_id = $1 AND id = $2",
        ("org_1", "pl_a"),
    )]


@pytest.mark.parametrize(
    "estado, motivo",
    [(401, "credencial_invalida"), (403, "credencial_invalida"), (402, "sin_saldo"), (404, "modelo_inexistente")],
)
async def test_probar_conexion_motivos_por_codigo(respx_mock: Any, estado: int, motivo: str) -> None:
    respx_mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(estado, json={"error": {"message": "x"}}))
    pool = PoolFalso()
    resultado = await _probar(pool)
    assert resultado["ok"] is False and resultado["motivo"] == motivo
    sql, args = pool.ejecutados[-1]
    assert "ultimo_error_en = now()" in sql and args[:2] == ("org_1", "pl_a") and motivo in args[2]


async def test_probar_conexion_sin_herramientas(respx_mock: Any) -> None:
    respx_mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json=_completion()))
    assert (await _probar())["motivo"] == "no_usa_herramientas"


@pytest.mark.parametrize("error", [httpx.ConnectError("caída"), httpx.ReadTimeout("colgado")])
async def test_probar_conexion_sin_respuesta(respx_mock: Any, error: Exception) -> None:
    respx_mock.post(f"{BASE}/chat/completions").mock(side_effect=error)
    assert (await _probar())["motivo"] == "sin_respuesta"


async def test_probar_conexion_rechazo_saneado(respx_mock: Any) -> None:
    mensaje = f"Incorrect parameter for key {LLAVE}: " + "x" * 600
    respx_mock.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(400, json={"error": {"message": mensaje}}))
    resultado = await _probar()
    assert resultado["motivo"] == "rechazo"
    assert LLAVE not in resultado["detalle"] and len(resultado["detalle"]) <= 300


def test_sanear_quita_la_llave_y_sus_restos() -> None:
    texto = sanear(f"Incorrect API key provided: {LLAVE}. También sk-proj-****7890.", LLAVE)
    assert LLAVE not in texto and "7890" not in texto
    assert len(sanear("x" * 900, "")) == 300
