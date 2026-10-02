"""Wline (025): la configuración del panel, tal como la lee el turno.

- El perfil sale de la versión vigente, con el conocimiento en el MISMO formato
  que `renderKb` de Wline: el prompt no cambia por mudar la configuración.
- `tipos[key].atencion` se mezcla en `context.pipeline.lines` por `key`; las
  claves que el contexto no trae se ignoran (FR-025).
- Las llaves de las plataformas: AES-256-GCM, `nonce ‖ cifrado+tag`, y sin
  NEA_ENCRYPTION_KEY un error que lo dice.
- La vigente se lee con 2 consultas, y la cadena solo lleva eslabones
  certificados con los mismos extras (contra un Postgres de verdad).
"""
from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.cadena import LlmCadena
from app.config import Settings
from app.configuracion import (
    SinLlaveDeCifrado,
    cifrar,
    descifrar,
    mezclar_atencion,
    perfil_desde_contenido,
    preparar_contexto,
)
from app.crm_brains import _perfil_desde_contexto
from app.db import PgStore
from app.profile import profile_from_payload
from tests.test_pg_store import TEST_DATABASE_URL, base_de_prueba  # noqa: F401
from tests.wline_fixtures import contexto

CLAVE = base64.b64encode(bytes(range(32))).decode()

CONTENIDO: dict[str, Any] = {
    "identidad": {"nombre": "Sofi", "tono": "cálido", "saludo": "¡Hola! Soy Sofi."},
    "instrucciones": "Vende por caja.",
    "escalado": "Si pide factura, pasa a una persona.",
    "conocimiento": [
        {"tipo": "qa", "pregunta": "¿Hacen envíos?", "respuesta": "Sí, a todo el país."},
        {"tipo": "bloque", "contenido": "Horario: 9 a 18 h."},
    ],
    "tipos": {"mayoristas": {"criterio": "Compra por caja", "atencion": "Ofrece la lista por caja."}},
    "cadena": [],
}


# ── El perfil ─────────────────────────────────────────────────────────────


def test_el_perfil_sale_de_la_version_vigente() -> None:
    perfil = perfil_desde_contenido(CONTENIDO, "Nea")

    assert perfil.cloud is True  # el mismo chasis que con el perfil de Wline
    assert perfil.agent_name == "Sofi"
    assert perfil.tone == "cálido"
    assert perfil.greeting == "¡Hola! Soy Sofi."
    assert perfil.instructions == "Vende por caja."
    assert perfil.escalation_rules == "Si pide factura, pasa a una persona."
    assert perfil.kb_text == "P: ¿Hacen envíos?\nR: Sí, a todo el país.\n\nHorario: 9 a 18 h."


def test_es_el_mismo_perfil_que_llegaba_de_wline() -> None:
    """La versión 1 se migra de Wline: con el mismo contenido, el mismo perfil."""
    v1 = {
        "agent": {"name": "Sofi", "tone": "cálido", "instructions": "Vende por caja.",
                  "escalationRules": "Si pide factura, pasa a una persona.", "greeting": "¡Hola! Soy Sofi."},
        "knowledge": [
            {"kind": "qa", "question": "¿Hacen envíos?", "answer": "Sí, a todo el país."},
            {"kind": "block", "content": "Horario: 9 a 18 h."},
        ],
    }
    assert perfil_desde_contenido(CONTENIDO, "Nea") == profile_from_payload(_perfil_desde_contexto(v1), "Nea")


def test_conocimiento_como_renderKb() -> None:
    """`renderKb`: una pregunta vacía se pinta igual; un bloque vacío se omite."""
    contenido = {"conocimiento": [
        {"tipo": "qa", "pregunta": "", "respuesta": ""},
        {"tipo": "bloque", "contenido": ""},
        {"tipo": "bloque", "contenido": "x"},
    ]}
    assert perfil_desde_contenido(contenido, "Nea").kb_text == "P: \nR: \n\nx"


def test_sin_conocimiento_ni_nombre() -> None:
    perfil = perfil_desde_contenido({"conocimiento": []}, "Nea")
    assert perfil.kb_text is None  # = "(knowledge base vacío)" de Wline
    assert perfil.agent_name == "Nea"
    assert perfil.tone is None and perfil.instructions is None


# ── La atención por tipo ──────────────────────────────────────────────────


def test_la_atencion_se_mezcla_por_key() -> None:
    ctx = contexto()
    mezclar_atencion(ctx, {"tipos": {
        "mayoristas": {"criterio": "x", "atencion": "Ofrece la lista por caja."},
        "consumidores": {"criterio": "y", "atencion": "  "},
        "borrada_en_wline": {"atencion": "no debe aparecer"},
    }})
    lineas = {l["key"]: l for l in ctx["pipeline"]["lines"]}

    assert lineas["mayoristas"]["atencion"] == "Ofrece la lista por caja."
    assert "atencion" not in lineas["consumidores"]  # vacía = sin atención
    assert set(lineas) == {"consumidores", "mayoristas"}  # la que no llegó, ignorada


def test_sin_pipeline_no_revienta() -> None:
    ctx: dict[str, Any] = {"contact": {}}
    mezclar_atencion(ctx, CONTENIDO)
    assert ctx == {"contact": {}}


# ── El cifrado de las llaves ──────────────────────────────────────────────


def test_cifrado_aes_gcm_nonce_mas_cifrado_y_tag() -> None:
    blob, ultimos4 = cifrar("sk-or-v1-secreta-1234", CLAVE)

    assert ultimos4 == "1234"
    assert b"secreta" not in blob
    assert len(blob) == 12 + len("sk-or-v1-secreta-1234") + 16
    claro = AESGCM(base64.b64decode(CLAVE)).decrypt(blob[:12], blob[12:], None)
    assert claro == b"sk-or-v1-secreta-1234"
    assert descifrar(blob, CLAVE) == "sk-or-v1-secreta-1234"


def test_cada_cifrado_lleva_su_nonce() -> None:
    assert cifrar("sk-igual", CLAVE)[0] != cifrar("sk-igual", CLAVE)[0]


@pytest.mark.parametrize("clave", ["", "  ", base64.b64encode(b"corta").decode(), "no-es-base64!"])
def test_sin_llave_de_cifrado_el_error_lo_dice(clave: str) -> None:
    with pytest.raises(SinLlaveDeCifrado, match="NEA_ENCRYPTION_KEY"):
        cifrar("sk-x", clave)
    with pytest.raises(SinLlaveDeCifrado, match="NEA_ENCRYPTION_KEY"):
        descifrar(b"\x00" * 40, clave)


def test_nea_encryption_key_es_opcional(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).nea_encryption_key == ""
    monkeypatch.setenv("NEA_ENCRYPTION_KEY", CLAVE)
    assert Settings(_env_file=None).nea_encryption_key == CLAVE


# ── Contra Postgres: la vigente y la cadena ───────────────────────────────

ORG = "org_uno"


class PoolQueCuenta:
    """El pool de verdad, contando las consultas."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self.consultas = 0

    async def fetch(self, *a: Any) -> Any:
        self.consultas += 1
        return await self._pool.fetch(*a)

    async def fetchrow(self, *a: Any) -> Any:
        self.consultas += 1
        return await self._pool.fetchrow(*a)

    async def execute(self, *a: Any) -> Any:
        return await self._pool.execute(*a)


con_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="sin TEST_DATABASE_URL: las consultas se prueban contra un Postgres de verdad",
)


@pytest.fixture
async def pool(base_de_prueba):  # noqa: F811
    store = PgStore(base_de_prueba)
    await store.connect()
    await store.pool.execute(
        "TRUNCATE agente_vigente, agente_version, certificacion, plataforma_modelo, cadena_prueba CASCADE"
    )
    try:
        yield store.pool
    finally:
        await store.aclose()


async def _plataforma(pool: Any, org: str, pid: str, llave: str) -> None:
    blob, ultimos4 = cifrar(llave, CLAVE)
    await pool.execute(
        """INSERT INTO plataforma_modelo (organization_id, id, nombre, base_url, llave_cifrada, llave_ultimos4)
           VALUES ($1, $2, $3, 'http://plataforma.test/v1', $4, $5)""",
        org, pid, f"Plataforma {pid}", blob, ultimos4,
    )


async def _certificar(pool: Any, org: str, pid: str, modelo: str, extras: dict[str, Any], aprobada: bool = True) -> None:
    await pool.execute(
        """INSERT INTO certificacion (organization_id, plataforma_id, modelo, extras, aprobada, modelo_respondio)
           VALUES ($1, $2, $3, $4, $5, $3)""",
        org, pid, modelo, json.dumps(extras), aprobada,
    )


async def _vigente(pool: Any, org: str, numero: int, contenido: dict[str, Any]) -> None:
    await pool.execute(
        """INSERT INTO agente_version (organization_id, numero, contenido, resumen, autor_id, autor_correo)
           VALUES ($1, $2, $3, 'prueba', 'u1', 'u1@test')""",
        org, numero, json.dumps(contenido),
    )
    await pool.execute(
        """INSERT INTO agente_vigente (organization_id, numero) VALUES ($1, $2)
           ON CONFLICT (organization_id) DO UPDATE SET numero = EXCLUDED.numero""",
        org, numero,
    )


def _settings(clave: str = CLAVE) -> Settings:
    return Settings(_env_file=None, nea_encryption_key=clave)


def _eslabones(cadena: LlmCadena | None) -> list[tuple[str, str]]:
    assert cadena is not None
    return [(e.plataforma.id, e.modelo) for e in cadena.eslabones]


@con_postgres
async def test_la_vigente_se_lee_con_2_consultas(pool: Any) -> None:
    await _plataforma(pool, ORG, "pl_a", "sk-de-a-1111")
    await _certificar(pool, ORG, "pl_a", "m1", {"reasoning_effort": "none"})
    await _vigente(pool, ORG, 3, {**CONTENIDO, "cadena": [
        {"plataforma_id": "pl_a", "modelo": "m1", "extras": {"reasoning_effort": "none"}},
    ]})
    contador = PoolQueCuenta(pool)
    ctx = contexto()

    prep = await preparar_contexto(contador, ORG, ctx, _settings())

    assert contador.consultas == 2
    assert prep.perfil is not None and prep.perfil.agent_name == "Sofi"
    assert _eslabones(prep.cadena) == [("pl_a", "m1")]
    eslabon = prep.cadena.eslabones[0]
    assert eslabon.plataforma.llave == "sk-de-a-1111"  # descifrada para el turno
    assert eslabon.llm._client.timeout == 20  # 20 s por intento
    assert eslabon.llm._extra_body == {"reasoning_effort": "none"}
    assert {l["key"]: l.get("atencion") for l in ctx["pipeline"]["lines"]}["mayoristas"] == "Ofrece la lista por caja."


@con_postgres
async def test_solo_eslabones_certificados_con_los_mismos_extras(pool: Any) -> None:
    for pid in ("pl_a", "pl_b"):
        await _plataforma(pool, ORG, pid, f"sk-{pid}-0000")
    await _certificar(pool, ORG, "pl_a", "bueno", {})
    await _certificar(pool, ORG, "pl_a", "otros_extras", {"x": 1})
    await _certificar(pool, ORG, "pl_b", "reprobado", {}, aprobada=False)
    await _vigente(pool, ORG, 1, {"cadena": [
        {"plataforma_id": "pl_b", "modelo": "reprobado", "extras": {}},
        {"plataforma_id": "pl_a", "modelo": "otros_extras", "extras": {"x": 2}},
        {"plataforma_id": "pl_a", "modelo": "sin_certificar", "extras": {}},
        {"plataforma_id": "pl_a", "modelo": "bueno", "extras": {}},
    ]})

    prep = await preparar_contexto(pool, ORG, contexto(), _settings())

    assert _eslabones(prep.cadena) == [("pl_a", "bueno")]


@con_postgres
async def test_sin_eslabones_certificados_no_hay_cadena(pool: Any) -> None:
    await _vigente(pool, ORG, 1, {**CONTENIDO, "cadena": []})
    prep = await preparar_contexto(pool, ORG, contexto(), _settings())
    assert prep.cadena is None
    assert prep.perfil is not None  # el perfil sí: la cadena es otra cosa


@con_postgres
async def test_sin_vigente_no_hay_nada_y_el_contexto_no_cambia(pool: Any) -> None:
    ctx = contexto()
    prep = await preparar_contexto(PoolQueCuenta(pool), ORG, ctx, _settings())
    assert prep.perfil is None and prep.cadena is None
    assert ctx == contexto()


@con_postgres
async def test_cada_organizacion_lee_la_suya(pool: Any) -> None:
    await _vigente(pool, "otra_org", 1, CONTENIDO)
    prep = await preparar_contexto(pool, ORG, contexto(), _settings())
    assert prep.perfil is None


@con_postgres
async def test_sin_llave_de_cifrado_los_eslabones_no_se_usan(pool: Any) -> None:
    await _plataforma(pool, ORG, "pl_a", "sk-de-a-1111")
    await _certificar(pool, ORG, "pl_a", "m1", {})
    await _vigente(pool, ORG, 1, {"cadena": [{"plataforma_id": "pl_a", "modelo": "m1", "extras": {}}]})

    prep = await preparar_contexto(pool, ORG, contexto(), _settings(clave=""))

    assert prep.cadena is None  # no revienta el turno: pasa a la transición


@con_postgres
async def test_la_cadena_de_prueba_solo_en_turnos_de_prueba_y_antes_de_vencer(pool: Any) -> None:
    await _plataforma(pool, ORG, "pl_a", "sk-de-a-1111")
    await _plataforma(pool, ORG, "pl_c", "sk-candidata-2222")
    await _certificar(pool, ORG, "pl_a", "m1", {})
    await _vigente(pool, ORG, 1, {"cadena": [{"plataforma_id": "pl_a", "modelo": "m1", "extras": {}}]})
    await pool.execute(
        """INSERT INTO cadena_prueba (organization_id, eslabon, lab_run_id, vence_en)
           VALUES ($1, $2, 'run_1', now() + interval '15 minutes')""",
        ORG, json.dumps({"plataforma_id": "pl_c", "modelo": "candidato", "extras": {"t": 1}}),
    )

    normal = await preparar_contexto(pool, ORG, contexto(), _settings())
    prueba = await preparar_contexto(pool, ORG, contexto(), _settings(), es_prueba=True)

    assert _eslabones(normal.cadena) == [("pl_a", "m1")]
    assert _eslabones(prueba.cadena) == [("pl_c", "candidato")]  # sin certificar: se está certificando
    assert prueba.cadena.eslabones[0].modelo_certificado is None
    # Solo la cadena de prueba anota el modelo que respondió (plan-bloque5 D1).
    assert (prueba.cadena._prueba, normal.cadena._prueba) == (True, False)

    await pool.execute("UPDATE cadena_prueba SET vence_en = now() - interval '1 second'")
    vencida = await preparar_contexto(pool, ORG, contexto(), _settings(), es_prueba=True)
    assert _eslabones(vencida.cadena) == [("pl_a", "m1")]
