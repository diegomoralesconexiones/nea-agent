"""Wline (025, US1/US2/US4/US7): versiones, publicación y plataformas del panel.

Contra un Postgres de verdad (el job de `TEST_DATABASE_URL`), con un Wline
falso que responde `GET/PUT /api/bot/automation`:

- Migración inicial (R17): versión 1 «migración», cadena vacía, sin publicar.
- Publicar (R5): insertar → `PUT` a Wline → mover el puntero; si el `PUT` falla
  no queda nada (FR-008, SC-017). El número es `max(local, appliedVersion) + 1`:
  un número que Wline ya aplicó no se repite nunca.
- En fila (candado de R5), conflicto por sección (FR-072), restaurar (FR-071),
  reconciliar y realinear al abrir (FR-025, R5), límites de data-model §2.6.
- Plataformas (FR-040, FR-041): la llave sale solo como `…abcd`.
- El panel escribe con el id de organización de Wline que llega en la cabecera
  firmada, no con el slug de `CRM_ORGANIZATION`: es el que usa el turno.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
import pytest

from app import panel
from app.configuracion import descifrar, preparar_contexto
from app.crm import CrmConflict, CrmUnreachable
from app.db import PgStore
from app.main import create_app
from app.state import AppContext
from tests.conftest import FakeLLM, make_settings
from tests.test_pg_store import TEST_DATABASE_URL, base_de_prueba  # noqa: F401

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="sin TEST_DATABASE_URL: el panel se prueba contra un Postgres de verdad"
)

SECRETO = "clave-de-servicio-larga-0123456789abcdef"
CLAVE = base64.b64encode(bytes(range(32))).decode()
ORG = "org_1"  # el id de Wline, el que llega en la cabecera firmada
SLUG = "principal"  # CRM_ORGANIZATION: el panel no escribe con él

LINEAS = [
    {"key": "consumidores", "name": "Consumidores", "tipoCliente": "Consumidor",
     "stages": [{"name": "Clasificado", "anchor": "clasificado"}, {"name": "Cotizado"}]},
    {"key": "mayoristas", "name": "Mayoristas", "tipoCliente": "Mayorista",
     "stages": [{"name": "Clasificado", "anchor": "clasificado"}, {"name": "Cotizado"}]},
]
TIPO_CLIENTE = {
    "key": "tipo_cliente", "label": "Tipo de cliente", "type": "lista", "required": True,
    "options": ["Consumidor", "Mayorista"], "system": True,
}
REGLAS = {
    "fichaFields": [TIPO_CLIENTE],
    "threshold": "Clasificado",
    "capabilities": {"mover_pipeline": True},
    "lineCriteria": {"consumidores": "Compra para su casa", "mayoristas": None},
}


class CrmFalso:
    """Wline: `GET/PUT /api/bot/automation`, idempotente por versión."""

    def __init__(self) -> None:
        self.applied: int | None = None
        self.lineas = copy.deepcopy(LINEAS)
        self.opciones = ["Clasificado", "Cotizado"]
        self.rules: dict[str, Any] = copy.deepcopy(REGLAS)
        self.agent = {
            "name": "Sofi", "tone": "cálido", "instructions": "Vende por caja.",
            "escalationRules": "Si pide factura, pasa a una persona.", "greeting": "¡Hola!",
            "knowledge": [
                {"kind": "qa", "question": "¿Hacen envíos?", "answer": "Sí, a todo el país."},
                {"kind": "qa", "question": "", "answer": ""},
                {"kind": "block", "content": "Horario: 9 a 18 h."},
            ],
        }
        self.puts: list[tuple[int, dict[str, Any]]] = []
        self.falla_get: Exception | None = None
        self.falla_put: Exception | None = None
        self.aplica_y_falla = False  # el PUT vence, pero Wline sí lo aplicó
        self.relectura_falla = False
        self.al_put: Callable[[int], Awaitable[None]] | None = None
        self.espera = 0.0
        # El Laboratorio (R8): las corridas que pidió el panel.
        self.corridas: dict[str, dict[str, Any]] = {}
        self.falla_lab: Exception | None = None
        self.al_lab: Callable[[], Awaitable[None]] | None = None

    async def lab_run_start(self) -> str:
        if self.al_lab:
            await self.al_lab()
        if self.falla_lab:
            raise self.falla_lab
        run_id = f"run_{len(self.corridas) + 1}"
        self.corridas[run_id] = {"status": "running", "progress": {"done": 0, "total": 7}, "score": None, "cases": []}
        return run_id

    async def lab_run_get(self, run_id: str) -> dict[str, Any]:
        if run_id not in self.corridas:
            raise CrmConflict("not_found", {"error": {"code": "not_found", "message": "Corrida no encontrada"}})
        return copy.deepcopy(self.corridas[run_id])

    async def automation_get(self) -> dict[str, Any]:
        if self.falla_get:
            raise self.falla_get
        if self.relectura_falla and self.puts:
            raise CrmUnreachable("relectura: automation devolvió 503")
        return {
            "appliedVersion": self.applied,
            "lines": copy.deepcopy(self.lineas),
            "sharedStages": [{"name": "Nuevo", "anchor": "nuevo"}],
            "thresholdOptions": list(self.opciones),
            "rules": copy.deepcopy(self.rules),
            "capabilityCatalog": {"mover_pipeline": {"label": "Mover en el pipeline", "available": True}},
            "agent": copy.deepcopy(self.agent),
        }

    async def automation_put(self, version: int, rules: dict[str, Any]) -> dict[str, Any]:
        self.puts.append((version, rules))
        if self.al_put:
            await self.al_put(version)
        if self.espera:
            await asyncio.sleep(self.espera)
        if self.aplica_y_falla:
            self.applied, self.rules = version, rules
            raise CrmUnreachable("error de red hacia el CRM: ReadTimeout")
        if self.falla_put:
            raise self.falla_put
        self.applied, self.rules = version, rules
        return {"appliedVersion": version}


@dataclass
class Nea:
    c: httpx.AsyncClient
    crm: CrmFalso
    pool: Any
    settings: Any

    async def pedir(
        self, metodo: str, ruta: str, cuerpo: Any = None, *, correo: str = "duena@example.com", r: str = "owner"
    ) -> httpx.Response:
        datos = b"" if cuerpo is None else json.dumps(cuerpo).encode()
        ident = {"u": "usr_" + correo.split("@")[0], "e": correo, "r": r, "o": ORG, "t": int(time.time())}
        cabecera = base64.urlsafe_b64encode(json.dumps(ident).encode()).decode().rstrip("=")
        resumen = hashlib.sha256(datos).hexdigest()
        return await self.c.request(
            metodo,
            ruta,
            content=datos,
            headers={
                "content-type": "application/json",
                "x-wline-panel": cabecera,
                "x-wline-panel-firma": panel.firma_esperada(SECRETO, metodo, ruta, resumen, cabecera),
                "x-contenido-sha256": resumen,
            },
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


@asynccontextmanager
async def _nea(pool: Any, **ajustes: Any) -> AsyncIterator[Nea]:
    settings = make_settings(
        vocero_mode="cloud", crm_organization=SLUG, crm_brain_secret=SECRETO,
        **{"nea_encryption_key": CLAVE, **ajustes},
    )
    crm = CrmFalso()
    ctx = AppContext(settings=settings, store=SimpleNamespace(pool=pool), crm=crm, llm=FakeLLM())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx)), base_url="http://nea.test"
    ) as c:
        yield Nea(c, crm, pool, settings)


@pytest.fixture
async def nea(pool: Any) -> AsyncIterator[Nea]:
    async with _nea(pool) as n:
        yield n


async def estado(nea: Nea) -> dict[str, Any]:
    r = await nea.pedir("GET", "/panel/api/estado")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()


async def publicar(nea: Nea, base: int, secciones: dict[str, Any], **kw: Any) -> httpx.Response:
    return await nea.pedir("PUT", "/panel/api/configuracion", {"base": base, "secciones": secciones}, **kw)


async def numeros(pool: Any) -> list[int]:
    filas = await pool.fetch("SELECT numero FROM agente_version WHERE organization_id = $1 ORDER BY numero", ORG)
    return [f["numero"] for f in filas]


async def vigente(pool: Any) -> int | None:
    return await pool.fetchval("SELECT numero FROM agente_vigente WHERE organization_id = $1", ORG)


async def fila(pool: Any, numero: int) -> Any:
    return await pool.fetchrow(
        "SELECT * FROM agente_version WHERE organization_id = $1 AND numero = $2", ORG, numero
    )


async def contenido_vigente(pool: Any) -> dict[str, Any]:
    v = await fila(pool, await vigente(pool))
    return json.loads(v["contenido"])


async def _plataforma(nea: Nea, nombre: str, llave: str = "sk-or-secreta-1234") -> str:
    r = await nea.pedir(
        "POST", "/panel/api/plataformas", {"nombre": nombre, "baseUrl": "https://plataforma.test/v1", "llave": llave}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _certificar(pool: Any, pid: str, modelo: str, extras: dict[str, Any]) -> None:
    await pool.execute(
        """INSERT INTO certificacion (organization_id, plataforma_id, modelo, extras, aprobada, modelo_respondio)
           VALUES ($1, $2, $3, $4, true, $3)""",
        ORG, pid, modelo, json.dumps(extras),
    )


# ── Migración inicial (R17) ───────────────────────────────────────────────


async def test_migracion_inicial_es_la_version_1_sin_publicar(nea: Nea) -> None:
    e = await estado(nea)

    v = e["vigente"]
    assert v["numero"] == 1
    assert v["autor"] == "migración"
    c = v["contenido"]
    assert c["identidad"] == {"nombre": "Sofi", "tono": "cálido", "saludo": "¡Hola!"}
    assert c["instrucciones"] == "Vende por caja."
    assert c["escalado"] == "Si pide factura, pasa a una persona."
    assert c["conocimiento"] == [
        {"tipo": "qa", "pregunta": "¿Hacen envíos?", "respuesta": "Sí, a todo el país."},
        {"tipo": "qa", "pregunta": "", "respuesta": ""},  # vacía: se migra tal cual
        {"tipo": "bloque", "contenido": "Horario: 9 a 18 h."},
    ]
    # Los criterios por línea pasan a ser las características (FR-012).
    assert c["tipos"] == {
        "consumidores": {"criterio": "Compra para su casa", "atencion": ""},
        "mayoristas": {"criterio": "", "atencion": ""},
    }
    assert (c["ficha"], c["umbral"], c["capacidades"], c["cadena"]) == (
        [TIPO_CLIENTE], "Clasificado", {"mover_pipeline": True}, []
    )
    assert nea.crm.puts == []  # las reglas ya están en Wline
    assert any("vacía" in a for a in e["avisos"])

    assert e["lineas"] == LINEAS
    assert e["etapasUmbral"] == ["Clasificado", "Cotizado"]
    assert e["catalogoCapacidades"] == {"mover_pipeline": {"label": "Mover en el pipeline", "available": True}}
    assert e["plataformas"] == []

    await estado(nea)  # re-ejecutable: no duplica
    assert await numeros(nea.pool) == [1]


async def test_escribe_con_el_id_de_wline_y_el_turno_la_encuentra(nea: Nea) -> None:
    await estado(nea)
    assert await nea.pool.fetchval("SELECT count(*) FROM agente_version WHERE organization_id = $1", SLUG) == 0
    assert await vigente(nea.pool) == 1
    prep = await preparar_contexto(nea.pool, ORG, {}, nea.settings)
    assert prep.perfil is not None
    assert prep.perfil.agent_name == "Sofi"


async def test_wline_caido_al_abrir(nea: Nea) -> None:
    nea.crm.falla_get = CrmUnreachable("automation devolvió 503")
    r = await nea.pedir("GET", "/panel/api/estado")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "wline_inalcanzable"
    assert await numeros(nea.pool) == []


# ── Publicar (R5) ─────────────────────────────────────────────────────────


async def test_publicar_inserta_manda_a_wline_y_luego_mueve_el_puntero(nea: Nea) -> None:
    await estado(nea)
    vistos: list[tuple[int, int | None]] = []

    async def al_put(version: int) -> None:
        existe = await nea.pool.fetchval(
            "SELECT count(*) FROM agente_version WHERE organization_id = $1 AND numero = $2", ORG, version
        )
        vistos.append((existe, await vigente(nea.pool)))

    nea.crm.al_put = al_put
    r = await publicar(nea, 1, {"instrucciones": "Vende por caja y por pieza."})

    assert r.status_code == 200, r.text
    assert r.json()["numero"] == 2
    assert r.headers["cache-control"] == "no-store"
    assert vistos == [(1, 1)]  # al mandarlo, la fila ya estaba y el puntero seguía en 1
    assert await vigente(nea.pool) == 2
    assert nea.crm.puts == [(2, REGLAS)]  # solo lo que Wline hace cumplir
    v2 = await fila(nea.pool, 2)
    assert (v2["resumen"], v2["autor_correo"], v2["autor_id"]) == ("instrucciones", "duena@example.com", "usr_duena")


@pytest.mark.parametrize(
    ("falla", "estado_http", "codigo"),
    [
        (CrmUnreachable("automation devolvió 503"), 502, "wline_inalcanzable"),
        (
            CrmConflict(
                "invalid_body",
                {"error": {"code": "invalid_body", "message": "El umbral «Visita» no existe en todas las líneas"}},
            ),
            422,
            "invalid_body",
        ),
    ],
)
async def test_put_fallido_no_deja_nada(nea: Nea, falla: Exception, estado_http: int, codigo: str) -> None:
    await estado(nea)
    nea.crm.falla_put = falla
    r = await publicar(nea, 1, {"umbral": "Visita"})

    assert r.status_code == estado_http
    assert r.json()["error"]["code"] == codigo
    if estado_http == 422:  # el motivo de Wline, tal cual (FR-017)
        assert r.json()["error"]["message"] == "El umbral «Visita» no existe en todas las líneas"
    assert await numeros(nea.pool) == [1]
    assert await vigente(nea.pool) == 1
    assert nea.crm.applied is None


async def test_wline_caido_antes_de_insertar(nea: Nea) -> None:
    await estado(nea)
    nea.crm.falla_get = CrmUnreachable("automation devolvió 503")
    r = await publicar(nea, 1, {"instrucciones": "x"})
    assert r.status_code == 502
    assert await numeros(nea.pool) == [1]
    assert nea.crm.puts == []


async def test_put_que_vence_pero_wline_aplico_mueve_el_puntero(nea: Nea) -> None:
    await estado(nea)
    nea.crm.aplica_y_falla = True
    r = await publicar(nea, 1, {"instrucciones": "x"})
    assert r.status_code == 200, r.text
    assert r.json()["numero"] == 2
    assert await numeros(nea.pool) == [1, 2]
    assert await vigente(nea.pool) == 2


async def test_si_la_relectura_falla_se_borra_y_al_abrir_se_realinea(nea: Nea) -> None:
    await estado(nea)
    nea.crm.aplica_y_falla = True
    nea.crm.relectura_falla = True
    r = await publicar(nea, 1, {"instrucciones": "x"})
    assert r.status_code == 502
    assert await numeros(nea.pool) == [1]
    assert nea.crm.applied == 2  # Wline sí la aplicó

    nea.crm.aplica_y_falla = nea.crm.relectura_falla = False
    e = await estado(nea)
    # Se republica la vigente con número nuevo: el 2 no se repite.
    assert e["vigente"]["numero"] == 3
    assert nea.crm.puts[-1][0] == 3
    assert any("realineadas" in a for a in e["avisos"])


async def test_el_numero_es_max_local_y_applied_mas_uno(nea: Nea) -> None:
    await estado(nea)
    nea.crm.applied = 7  # p. ej. la base de Nea se restauró de un respaldo
    r = await publicar(nea, 1, {"instrucciones": "x"})
    assert r.json()["numero"] == 8
    assert await numeros(nea.pool) == [1, 8]


async def test_tipo_cliente_sale_de_wline_al_publicar(nea: Nea) -> None:
    """`tipo_cliente` es del sistema: sus opciones son los tipos vigentes y
    Wline rechaza otra cosa. Un tipo creado en Wline no bloquea al panel."""
    nea.crm.rules["fichaFields"] = [TIPO_CLIENTE, {"key": "zona", "label": "Zona", "type": "texto", "required": False}]
    await estado(nea)
    vigente_en_wline = {**TIPO_CLIENTE, "options": ["Consumidor", "Mayorista", "Institucional"]}
    nea.crm.rules["fichaFields"] = [vigente_en_wline, nea.crm.rules["fichaFields"][1]]

    r = await publicar(nea, 1, {"instrucciones": "x"})
    assert r.status_code == 200, r.text
    ficha = nea.crm.puts[-1][1]["fichaFields"]
    assert ficha[0] == vigente_en_wline
    assert ficha[1]["key"] == "zona"  # lo demás, como lo dejó el panel
    assert (await contenido_vigente(nea.pool))["ficha"][0] == vigente_en_wline


async def test_sin_cambios_no_publica(nea: Nea) -> None:
    await estado(nea)
    r = await publicar(nea, 1, {"instrucciones": "Vende por caja."})
    assert r.status_code == 200
    assert r.json()["numero"] == 1
    assert nea.crm.puts == []


# ── En fila y conflictos (R5, FR-072) ─────────────────────────────────────


async def test_dos_publicaciones_de_la_misma_seccion_a_la_vez(nea: Nea) -> None:
    await estado(nea)
    nea.crm.espera = 0.05
    a, b = await asyncio.gather(
        publicar(nea, 1, {"instrucciones": "A"}, correo="a@test"),
        publicar(nea, 1, {"instrucciones": "B"}, correo="b@test"),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    gana, pierde = (a, b) if a.status_code == 200 else (b, a)
    error = pierde.json()["error"]
    assert error["code"] == "conflicto"
    assert error["seccion"] == "instrucciones"
    assert error["autor"] == ("a@test" if gana is a else "b@test")
    assert error["fecha"]
    assert await numeros(nea.pool) == [1, 2]
    assert await vigente(nea.pool) == 2


async def test_dos_publicaciones_de_secciones_distintas_a_la_vez(nea: Nea) -> None:
    await estado(nea)
    nea.crm.espera = 0.05
    a, b = await asyncio.gather(
        publicar(nea, 1, {"instrucciones": "A"}),
        publicar(nea, 1, {"escalado": "B"}),
    )
    assert (a.status_code, b.status_code) == (200, 200)
    assert await vigente(nea.pool) == 3
    c = await contenido_vigente(nea.pool)
    assert (c["instrucciones"], c["escalado"]) == ("A", "B")  # la segunda no pisa a la primera


async def test_conflicto_por_seccion_dice_quien_y_cuando(nea: Nea) -> None:
    await estado(nea)
    assert (await publicar(nea, 1, {"instrucciones": "A"}, correo="a@test")).status_code == 200
    r = await publicar(nea, 1, {"instrucciones": "B"}, correo="b@test")
    assert r.status_code == 409
    error = r.json()["error"]
    assert (error["seccion"], error["autor"]) == ("instrucciones", "a@test")
    assert error["fecha"]
    assert "instrucciones" in error["message"]
    # Otra sección desde la misma base no se bloquea.
    r = await publicar(nea, 1, {"escalado": "C"}, correo="b@test")
    assert r.status_code == 200
    assert r.json()["numero"] == 3


# ── Al abrir: umbral, reconciliar y realinear (FR-025, R5) ────────────────


async def test_umbral_fuera_de_las_opciones_avisa_y_propone_clasificado(nea: Nea) -> None:
    nea.crm.rules["threshold"] = "Cotizado"
    await estado(nea)
    nea.crm.opciones = ["Clasificado"]  # se creó un tipo sin «Cotizado»
    e = await estado(nea)
    assert any("«Cotizado»" in a and "Clasificado" in a for a in e["avisos"])


async def test_reconcilia_los_tipos_eliminados_en_wline(nea: Nea) -> None:
    await estado(nea)
    nea.crm.lineas = [linea for linea in nea.crm.lineas if linea["key"] != "mayoristas"]
    e = await estado(nea)
    assert e["vigente"]["numero"] == 2
    assert "mayoristas" not in e["vigente"]["contenido"]["tipos"]
    assert (await fila(nea.pool, 2))["resumen"] == "tipos eliminados en Wline: mayoristas"
    assert (await fila(nea.pool, 2))["autor_correo"] == "sistema"
    assert any("mayoristas" in a for a in e["avisos"])
    assert nea.crm.puts[-1][1]["lineCriteria"] == {"consumidores": "Compra para su casa"}


async def test_realinea_sobre_la_migracion_con_la_cadena_vacia(nea: Nea) -> None:
    nea.crm.applied = 5  # Wline aplicó publicaciones de una base de Nea anterior
    e = await estado(nea)
    assert e["vigente"]["numero"] == 6
    assert e["vigente"]["contenido"]["cadena"] == []
    assert nea.crm.puts == [(6, REGLAS)]
    assert (await fila(nea.pool, 6))["resumen"] == "reglas realineadas con Wline"
    assert any("realineadas" in a for a in e["avisos"])


# ── Historial, restaurar y conocimiento (FR-070, FR-071, R11) ─────────────


async def test_restaurar_es_una_version_nueva_sin_lo_que_ya_no_existe(nea: Nea) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    pb = await _plataforma(nea, "Plataforma B")
    await _certificar(nea.pool, pa, "m1", {})
    await _certificar(nea.pool, pb, "m2", {})
    cadena = [{"plataforma_id": pa, "modelo": "m1", "extras": {}}, {"plataforma_id": pb, "modelo": "m2", "extras": {}}]
    assert (await publicar(nea, 1, {"instrucciones": "Vieja", "cadena": cadena})).status_code == 200
    assert (await publicar(nea, 2, {"instrucciones": "Nueva", "cadena": cadena[:1]})).status_code == 200
    await nea.pool.execute("DELETE FROM plataforma_modelo WHERE organization_id = $1 AND id = $2", ORG, pb)
    nea.crm.lineas = [linea for linea in nea.crm.lineas if linea["key"] != "mayoristas"]

    r = await nea.pedir("POST", "/panel/api/versiones/2/restaurar")
    assert r.status_code == 200, r.text
    assert r.json()["numero"] == 4
    avisos = r.json()["avisos"]
    assert any("mayoristas" in a for a in avisos)
    assert any("m2" in a for a in avisos)
    c = await contenido_vigente(nea.pool)
    assert c["instrucciones"] == "Vieja"
    assert "mayoristas" not in c["tipos"]
    assert c["cadena"] == cadena[:1]
    v4 = await fila(nea.pool, 4)
    assert (v4["restaurada_de"], v4["resumen"]) == (2, "restaurada de 2")

    historial = (await nea.pedir("GET", "/panel/api/versiones")).json()
    assert [v["numero"] for v in historial] == [4, 3, 2, 1]
    assert historial[0]["restauradaDe"] == 2
    assert historial[0]["autor"] == "duena@example.com"
    assert historial[0]["fecha"]
    assert historial[-1]["resumen"] == "migración"


async def test_restaurar_una_version_que_no_existe(nea: Nea) -> None:
    await estado(nea)
    r = await nea.pedir("POST", "/panel/api/versiones/99/restaurar")
    assert r.status_code == 404


async def test_agregar_conocimiento_es_una_publicacion_mas(nea: Nea) -> None:
    await estado(nea)
    r = await nea.pedir("POST", "/panel/api/conocimiento", {"pregunta": "¿Tienen garantía?", "respuesta": "Sí, 1 año."})
    assert r.status_code == 200, r.text
    assert r.json()["numero"] == 2
    c = await contenido_vigente(nea.pool)
    assert c["conocimiento"][-1] == {"tipo": "qa", "pregunta": "¿Tienen garantía?", "respuesta": "Sí, 1 año."}
    assert (await fila(nea.pool, 2))["resumen"] == "conocimiento"


# ── Validación (data-model §2.6) y cadena (FR-042, FR-048) ────────────────


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"base": 1, "secciones": {"instrucciones": "x" * 8001}},
        {"base": 1, "secciones": {"escalado": 5}},
        {"base": 1, "secciones": {"identidad": {"nombre": "x" * 4001}}},
        {"base": 1, "secciones": {"conocimiento": [{"tipo": "qa", "pregunta": "p", "respuesta": "r"}] * 501}},
        {"base": 1, "secciones": {"conocimiento": [{"tipo": "otro"}]}},
        {"base": 1, "secciones": {"tipos": {"consumidores": {"criterio": 3}}}},
        {"base": 1, "secciones": {"cadena": [{"plataforma_id": "pl_x", "modelo": "m", "extras": {}}] * 7}},
        {"base": 1, "secciones": {"cadena": [{"plataforma_id": "pl_x", "modelo": "m", "extras": {"x": "y" * 2048}}]}},
        {"base": 1, "secciones": {"cadena": [{"plataforma_id": "pl_no_existe", "modelo": "m", "extras": {}}]}},
        {"base": 1, "secciones": {"desconocida": 1}},
        {"base": 1, "secciones": {}},
        {"secciones": {"instrucciones": "x"}},
        [1, 2],
    ],
)
async def test_limites_de_validacion(nea: Nea, cuerpo: Any) -> None:
    await estado(nea)
    r = await nea.pedir("PUT", "/panel/api/configuracion", cuerpo)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalido"
    assert nea.crm.puts == []


async def test_avisa_si_toda_la_cadena_esta_en_una_plataforma(nea: Nea) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    await _certificar(nea.pool, pa, "m1", {})
    await _certificar(nea.pool, pa, "m2", {})
    cadena = [{"plataforma_id": pa, "modelo": "m1", "extras": {}}, {"plataforma_id": pa, "modelo": "m2", "extras": {}}]
    r = await publicar(nea, 1, {"cadena": cadena})
    assert r.status_code == 200, r.text
    assert any("una sola plataforma" in a for a in r.json()["avisos"])


async def test_un_eslabon_sin_certificar_no_se_publica(nea: Nea) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    await _certificar(nea.pool, pa, "m1", {})
    eslabon = {"plataforma_id": pa, "modelo": "m1", "extras": {"reasoning_effort": "none"}}  # otros extras
    r = await publicar(nea, 1, {"cadena": [eslabon]})
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "sin_certificar"
    assert error["eslabon"] == eslabon
    assert nea.crm.puts == []


# ── Plataformas (FR-040, FR-041, FR-043, FR-045) ──────────────────────────


async def test_plataformas_con_la_llave_enmascarada(nea: Nea) -> None:
    pid = await _plataforma(nea, "OpenRouter", "sk-or-secreta-1234")
    assert pid.startswith("pl_")

    lista = await nea.pedir("GET", "/panel/api/plataformas")
    assert lista.status_code == 200
    assert "sk-or-secreta" not in lista.text
    assert lista.json() == [{
        "id": pid, "nombre": "OpenRouter", "baseUrl": "https://plataforma.test/v1", "llave": "…1234",
        "ultimoExito": None, "ultimoError": None, "ultimoErrorEn": None,
    }]
    assert [p["llave"] for p in (await estado(nea))["plataformas"]] == ["…1234"]

    # Sin `llave`, se conserva la guardada.
    r = await nea.pedir("PUT", f"/panel/api/plataformas/{pid}", {"nombre": "OR"})
    assert r.status_code == 200
    guardada = await nea.pool.fetchrow(
        "SELECT nombre, llave_cifrada FROM plataforma_modelo WHERE organization_id = $1 AND id = $2", ORG, pid
    )
    assert guardada["nombre"] == "OR"
    assert descifrar(bytes(guardada["llave_cifrada"]), CLAVE) == "sk-or-secreta-1234"
    r = await nea.pedir("PUT", f"/panel/api/plataformas/{pid}", {"llave": "sk-nueva-9999"})
    assert r.status_code == 200
    assert (await nea.pedir("GET", "/panel/api/plataformas")).json()[0]["llave"] == "…9999"
    assert (await nea.pedir("PUT", "/panel/api/plataformas/pl_otra", {"nombre": "x"})).status_code == 404


@pytest.mark.parametrize(
    ("cuerpo", "estado_http"),
    [
        ({"nombre": "A", "baseUrl": "ftp://plataforma.test", "llave": "k"}, 422),
        ({"nombre": "A", "baseUrl": "http://api.ejemplo.com/v1", "llave": "k"}, 422),
        ({"nombre": "A", "baseUrl": "http://host.docker.internal:3000/api/dev/ai-mock/v1", "llave": "test-a"}, 201),
        ({"nombre": "A", "baseUrl": "http://localhost:3000/v1", "llave": "test-a"}, 201),
        ({"nombre": "", "baseUrl": "https://plataforma.test/v1", "llave": "k"}, 422),
        ({"nombre": "A", "baseUrl": "https://plataforma.test/v1", "llave": ""}, 422),
    ],
)
async def test_alta_valida_la_direccion(nea: Nea, cuerpo: dict[str, Any], estado_http: int) -> None:
    r = await nea.pedir("POST", "/panel/api/plataformas", cuerpo)
    assert r.status_code == estado_http, r.text


async def test_nombre_repetido(nea: Nea) -> None:
    await _plataforma(nea, "OpenRouter")
    r = await nea.pedir(
        "POST", "/panel/api/plataformas", {"nombre": "OpenRouter", "baseUrl": "https://otra.test/v1", "llave": "k"}
    )
    assert r.status_code == 409


async def test_sin_llave_de_cifrado_no_registra(pool: Any) -> None:
    async with _nea(pool, nea_encryption_key="") as nea:
        r = await nea.pedir(
            "POST", "/panel/api/plataformas", {"nombre": "A", "baseUrl": "https://plataforma.test/v1", "llave": "k"}
        )
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "sin_llave_de_cifrado"
        assert "NEA_ENCRYPTION_KEY" in r.json()["error"]["message"]


async def test_no_se_borra_una_plataforma_que_usa_la_vigente(nea: Nea) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    await _certificar(nea.pool, pa, "m1", {})
    assert (await publicar(nea, 1, {"cadena": [{"plataforma_id": pa, "modelo": "m1", "extras": {}}]})).status_code == 200

    r = await nea.pedir("DELETE", f"/panel/api/plataformas/{pa}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "en_uso"

    assert (await publicar(nea, 2, {"cadena": []})).status_code == 200
    r = await nea.pedir("DELETE", f"/panel/api/plataformas/{pa}")
    assert r.status_code == 204
    assert await nea.pool.fetchval("SELECT count(*) FROM certificacion WHERE organization_id = $1", ORG) == 0
    assert (await nea.pedir("DELETE", f"/panel/api/plataformas/{pa}")).status_code == 404


async def test_probar_conexion_usa_la_llave_guardada(nea: Nea, monkeypatch: pytest.MonkeyPatch) -> None:
    pa = await _plataforma(nea, "Plataforma A", "sk-or-secreta-1234")
    llamada: dict[str, Any] = {}

    async def falso(plataforma: Any, modelo: str, extras: dict[str, Any], *, pool: Any = None, organization_id: str = "") -> dict[str, Any]:
        llamada.update(llave=plataforma.llave, modelo=modelo, extras=extras, org=organization_id, pool=pool)
        return {"ok": True, "modeloRespondio": "m1-2026-09"}

    monkeypatch.setattr("app.cadena.probar_conexion", falso)
    r = await nea.pedir("POST", f"/panel/api/plataformas/{pa}/probar", {"modelo": "m1", "extras": {"reasoning_effort": "none"}})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["modeloRespondio"] == "m1-2026-09"
    assert isinstance(r.json()["ms"], int)
    assert llamada == {
        "llave": "sk-or-secreta-1234", "modelo": "m1", "extras": {"reasoning_effort": "none"},
        "org": ORG, "pool": nea.pool,
    }
    assert "sk-or-secreta" not in r.text
    assert (await nea.pedir("POST", "/panel/api/plataformas/pl_otra/probar", {"modelo": "m1", "extras": {}})).status_code == 404


# ── Bloque 5 · certificación (T064, R8; plan-bloque5 D1–D3) ──────────────


async def certificar(nea: Nea, pid: str, modelo: str = "m1", extras: dict[str, Any] | None = None) -> httpx.Response:
    return await nea.pedir(
        "POST", "/panel/api/certificaciones", {"plataforma_id": pid, "modelo": modelo, "extras": extras or {}}
    )


def terminar(crm: CrmFalso, run_id: str, veredictos: list[str], *, judge_failed: int = 0, status: str = "done") -> None:
    casos = [{"persona": f"p{i}", "status": "done", "veredicto": v} for i, v in enumerate(veredictos)]
    casos += [{"persona": f"j{i}", "status": "judge_failed", "veredicto": None} for i in range(judge_failed)]
    crm.corridas[run_id] = {
        "status": status, "progress": {"done": len(casos), "total": len(casos)}, "score": 90, "cases": casos,
    }


async def pruebas(pool: Any) -> int:
    return await pool.fetchval("SELECT count(*) FROM cadena_prueba WHERE organization_id = $1", ORG)


async def test_certificar_deja_la_cadena_de_prueba_antes_de_pedir_la_corrida(nea: Nea) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    vista: dict[str, Any] = {}

    async def al_lab() -> None:
        vista["fila"] = await nea.pool.fetchrow(
            "SELECT eslabon, vence_en - now() AS queda FROM cadena_prueba WHERE organization_id = $1", ORG
        )

    nea.crm.al_lab = al_lab
    r = await certificar(nea, pa, "m1", {"reasoning_effort": "none"})

    assert r.status_code == 202, r.text
    run_id = r.json()["runId"]
    assert json.loads(vista["fila"]["eslabon"]) == {"plataforma_id": pa, "modelo": "m1", "extras": {"reasoning_effort": "none"}}
    # D2: el tope del Laboratorio (20 min) más un minuto.
    assert timedelta(minutes=20) < vista["fila"]["queda"] <= timedelta(minutes=21)
    assert await nea.pool.fetchval("SELECT lab_run_id FROM cadena_prueba WHERE organization_id = $1", ORG) == run_id
    e = await estado(nea)  # «certificando…»
    assert e["certificando"] == {"plataformaId": pa, "modelo": "m1", "extras": {"reasoning_effort": "none"}, "runId": run_id}


async def test_una_certificacion_a_la_vez(nea: Nea) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    assert (await certificar(nea, pa)).status_code == 202
    r = await certificar(nea, pa, "m2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "laboratorio_ocupado"
    assert len(nea.crm.corridas) == 1  # no se le pidió otra corrida a Wline


@pytest.mark.parametrize(
    ("falla", "estado_http", "codigo"),
    [
        (
            CrmConflict("run_in_progress", {"error": {"code": "run_in_progress", "message": "Ya hay una corrida en curso"}}),
            409,
            "laboratorio_ocupado",
        ),
        (CrmUnreachable("lab/runs devolvió 503"), 502, "wline_inalcanzable"),
    ],
)
async def test_si_wline_no_arranca_la_corrida_no_queda_cadena_de_prueba(
    nea: Nea, falla: Exception, estado_http: int, codigo: str
) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    nea.crm.falla_lab = falla
    r = await certificar(nea, pa)
    assert r.status_code == estado_http
    assert r.json()["error"]["code"] == codigo
    assert await pruebas(nea.pool) == 0


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"plataforma_id": "pl_no_existe", "modelo": "m1", "extras": {}},
        {"plataforma_id": "PA", "modelo": "", "extras": {}},
        {"plataforma_id": "PA", "modelo": "m1", "extras": {"x": "y" * 2048}},
        {"plataforma_id": "PA", "modelo": "m1", "extras": [1]},
    ],
)
async def test_certificar_valida_el_eslabon(nea: Nea, cuerpo: dict[str, Any]) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    cuerpo = {**cuerpo, "plataforma_id": pa if cuerpo["plataforma_id"] == "PA" else cuerpo["plataforma_id"]}
    r = await nea.pedir("POST", "/panel/api/certificaciones", cuerpo)
    assert r.status_code == 422, r.text
    assert nea.crm.corridas == {}


async def test_la_corrida_en_curso_no_cierra_nada(nea: Nea) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    run_id = (await certificar(nea, pa)).json()["runId"]
    r = await nea.pedir("GET", f"/panel/api/certificaciones/{run_id}")
    assert r.status_code == 200
    assert r.json() == {"status": "running", "progress": {"done": 0, "total": 7}, "score": None}
    assert await pruebas(nea.pool) == 1


@pytest.mark.parametrize(
    ("veredictos", "judge_failed", "aprobada"),
    [
        (["verde"] * 6 + ["amarillo"], 0, True),  # el amarillo pasa (A1)
        (["verde"] * 6 + ["rojo"], 0, False),
        (["verde"] * 6, 1, False),
    ],
)
async def test_al_terminar_guarda_la_certificacion(
    nea: Nea, veredictos: list[str], judge_failed: int, aprobada: bool
) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    await _certificar(nea.pool, pa, "m1", {})  # una anterior, con otros extras y marcada
    await nea.pool.execute("UPDATE certificacion SET requiere_atencion = true WHERE organization_id = $1", ORG)
    run_id = (await certificar(nea, pa, "m1", {"reasoning_effort": "none"})).json()["runId"]
    # D1: la cadena de prueba anota el modelo que respondió en cada turno.
    await nea.pool.execute("UPDATE cadena_prueba SET modelo_respondio = 'm1-0731' WHERE organization_id = $1", ORG)
    terminar(nea.crm, run_id, veredictos, judge_failed=judge_failed)

    r = await nea.pedir("GET", f"/panel/api/certificaciones/{run_id}")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "done", "progress": {"done": 7, "total": 7}, "score": 90, "aprobada": aprobada,
    }
    cert = await nea.pool.fetchrow(
        "SELECT * FROM certificacion WHERE organization_id = $1 AND plataforma_id = $2 AND modelo = 'm1'", ORG, pa
    )
    assert cert["aprobada"] is aprobada
    assert (cert["lab_run_id"], float(cert["puntaje"]), cert["modelo_respondio"]) == (run_id, 90.0, "m1-0731")
    assert json.loads(cert["extras"]) == {"reasoning_effort": "none"}
    assert cert["requiere_atencion"] is False
    assert await pruebas(nea.pool) == 0
    # Consultar otra vez da lo mismo.
    assert (await nea.pedir("GET", f"/panel/api/certificaciones/{run_id}")).json() == r.json()


async def test_una_corrida_fallida_no_certifica_ni_descertifica(nea: Nea) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    await _certificar(nea.pool, pa, "m1", {})
    run_id = (await certificar(nea, pa, "m1", {})).json()["runId"]
    terminar(nea.crm, run_id, [], status="failed")

    r = await nea.pedir("GET", f"/panel/api/certificaciones/{run_id}")
    assert r.json()["status"] == "failed"
    assert r.json()["aprobada"] is False
    cert = await nea.pool.fetchrow("SELECT aprobada, lab_run_id FROM certificacion WHERE organization_id = $1", ORG)
    assert (cert["aprobada"], cert["lab_run_id"]) == (True, None)  # la anterior, intacta
    assert await pruebas(nea.pool) == 0


async def test_al_abrir_cierra_una_certificacion_terminada(nea: Nea) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    run_id = (await certificar(nea, pa)).json()["runId"]
    terminar(nea.crm, run_id, ["verde"] * 7)  # la página se cerró antes de enterarse

    e = await estado(nea)
    assert e["certificando"] is None
    assert [c["aprobada"] for c in e["certificaciones"] if c["plataformaId"] == pa] == [True]
    assert await pruebas(nea.pool) == 0


async def test_una_corrida_que_no_es_del_panel(nea: Nea) -> None:
    await estado(nea)
    assert (await nea.pedir("GET", "/panel/api/certificaciones/run_ajena")).status_code == 404


# ── Bloque 5 · publicar exige certificación (T065, FR-048) ────────────────


@pytest.mark.parametrize("cambio", ["modelo", "plataforma", "extras"])
async def test_cambiar_plataforma_modelo_o_extras_descertifica(nea: Nea, cambio: str) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    pb = await _plataforma(nea, "Plataforma B")
    await _certificar(nea.pool, pa, "m1", {})
    certificado = {"plataforma_id": pa, "modelo": "m1", "extras": {}}
    otro = {
        "modelo": {**certificado, "modelo": "m2"},
        "plataforma": {**certificado, "plataforma_id": pb},
        "extras": {**certificado, "extras": {"reasoning_effort": "none"}},
    }[cambio]

    r = await publicar(nea, 1, {"cadena": [certificado, otro]})
    assert r.status_code == 409
    assert (r.json()["error"]["code"], r.json()["error"]["eslabon"]) == ("sin_certificar", otro)
    assert (await publicar(nea, 1, {"cadena": [certificado]})).status_code == 200


async def test_una_recertificacion_reprobada_descertifica(nea: Nea) -> None:
    await estado(nea)
    pa = await _plataforma(nea, "Plataforma A")
    await _certificar(nea.pool, pa, "m1", {})
    run_id = (await certificar(nea, pa, "m1", {})).json()["runId"]
    terminar(nea.crm, run_id, ["verde"] * 6 + ["rojo"])
    await nea.pedir("GET", f"/panel/api/certificaciones/{run_id}")

    r = await publicar(nea, 1, {"cadena": [{"plataforma_id": pa, "modelo": "m1", "extras": {}}]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "sin_certificar"


# ── Bloque 5 · el juez del Laboratorio (T066, R11; D4, D7) ────────────────

COMPORTAMIENTO = (
    "Nombre: Sofi\nTono: cálido\nInstrucciones: Vende por caja.\nEscalado: Si pide factura, pasa a una persona."
)
CONOCIMIENTO = "P: ¿Hacen envíos?\nR: Sí, a todo el país.\n\nP: \nR: \n\nHorario: 9 a 18 h."


async def test_el_juez_lee_la_vigente(nea: Nea) -> None:
    await estado(nea)
    r = await nea.pedir("GET", "/panel/api/juez", r="sistema")
    assert r.status_code == 200, r.text
    assert r.json() == {"comportamiento": COMPORTAMIENTO, "conocimiento": CONOCIMIENTO}

    identidad = {"nombre": "Sofi", "tono": "", "saludo": ""}
    assert (await publicar(nea, 1, {"instrucciones": "Vende por pieza.", "identidad": identidad})).status_code == 200
    r = await nea.pedir("GET", "/panel/api/juez", r="sistema")
    assert r.json()["comportamiento"] == (
        "Nombre: Sofi\nInstrucciones: Vende por pieza.\nEscalado: Si pide factura, pasa a una persona."
    )


async def test_el_juez_sin_vigente_arma_el_texto_de_wline_sin_migrar(nea: Nea) -> None:
    r = await nea.pedir("GET", "/panel/api/juez", r="sistema")
    assert r.json() == {"comportamiento": COMPORTAMIENTO, "conocimiento": CONOCIMIENTO}
    assert await numeros(nea.pool) == []  # abrir el panel sigue siendo lo único que migra


async def test_el_juez_con_el_conocimiento_vacio(nea: Nea) -> None:
    nea.crm.agent["knowledge"] = []
    r = await nea.pedir("GET", "/panel/api/juez", r="sistema")
    assert r.json()["conocimiento"] == "(knowledge base vacío)"  # como renderKb


async def test_el_juez_es_solo_para_wline(nea: Nea) -> None:
    await estado(nea)
    assert (await nea.pedir("GET", "/panel/api/juez")).status_code == 401  # owner
    assert (await nea.pedir("GET", "/panel/api/juez", r="admin")).status_code == 401
    assert (await nea.pedir("GET", "/panel/api/estado", r="sistema")).status_code == 401


# ── Bloque 5 · guardar sin haber abierto el panel (D6) ────────────────────


async def test_aplicar_conocimiento_sin_abrir_el_panel_migra_primero(nea: Nea) -> None:
    r = await nea.pedir("POST", "/panel/api/conocimiento", {"pregunta": "¿Garantía?", "respuesta": "Un año."})
    assert r.status_code == 200, r.text
    assert r.json()["numero"] == 2
    assert await numeros(nea.pool) == [1, 2]
    assert (await fila(nea.pool, 1))["resumen"] == "migración"


# ── 026 · Los dos avisos (FR-117, FR-119; contracts/fase-a §2 y §5) ──────────

AVISO = "Ya lo comunico con una persona de nuestro equipo."
AVISO_HOSTIL = "No puedo responder a este tipo de mensajes. Le pasaré con una persona perteneciente al equipo."
INICIAL_DE_WLINE = "Gracias por escribir. Una persona del equipo continuará esta conversación en breve."


def _wline_026(nea: Nea) -> None:
    """Un Wline que ya anuncia los avisos en su documento de reglas."""
    nea.crm.rules["handoffNotice"] = INICIAL_DE_WLINE
    nea.crm.rules["hostileNotice"] = None


async def test_el_aviso_de_pase_viaja_a_wline_como_handoffNotice(nea: Nea) -> None:
    _wline_026(nea)
    await estado(nea)

    r = await publicar(nea, 1, {"aviso_pase": AVISO})

    assert r.status_code == 200, r.text
    _, reglas = nea.crm.puts[-1]
    assert reglas["handoffNotice"] == AVISO
    assert reglas["hostileNotice"] is None
    assert (await contenido_vigente(nea.pool))["aviso_pase"] == AVISO
    assert (await fila(nea.pool, r.json()["numero"]))["resumen"] == "aviso_pase"


async def test_el_aviso_hostil_viaja_como_hostileNotice_y_null_lo_quita(nea: Nea) -> None:
    _wline_026(nea)
    await estado(nea)

    r = await publicar(nea, 1, {"aviso_hostil": AVISO_HOSTIL})
    assert r.status_code == 200, r.text
    assert nea.crm.puts[-1][1]["hostileNotice"] == AVISO_HOSTIL

    r = await publicar(nea, r.json()["numero"], {"aviso_hostil": None})
    assert r.status_code == 200, r.text
    assert nea.crm.puts[-1][1]["hostileNotice"] is None
    assert "aviso_hostil" not in await contenido_vigente(nea.pool)


async def test_sin_las_secciones_los_avisos_viajan_null(nea: Nea) -> None:
    _wline_026(nea)
    await estado(nea)

    assert (await publicar(nea, 1, {"instrucciones": "Vende por pieza."})).status_code == 200

    _, reglas = nea.crm.puts[-1]
    assert reglas["handoffNotice"] is None and reglas["hostileNotice"] is None


async def test_a_un_wline_anterior_no_se_le_mandan_los_avisos(nea: Nea) -> None:
    """Su esquema de reglas es cerrado: un campo que no conoce le haría rechazar
    la publicación. Nea se despliega antes que Wline, así que tiene que convivir."""
    await estado(nea)  # este Wline no anuncia handoffNotice

    r = await publicar(nea, 1, {"aviso_pase": AVISO, "aviso_hostil": AVISO_HOSTIL})

    assert r.status_code == 200, r.text
    assert nea.crm.puts == [(2, REGLAS)]  # ni una clave nueva
    c = await contenido_vigente(nea.pool)
    assert (c["aviso_pase"], c["aviso_hostil"]) == (AVISO, AVISO_HOSTIL)  # quedan en la versión


@pytest.mark.parametrize("seccion", ["aviso_pase", "aviso_hostil"])
@pytest.mark.parametrize("valor", ["", "   ", "x" * 301, 7, ["a"]])
async def test_un_aviso_vacio_largo_o_que_no_es_texto_se_rechaza(nea: Nea, seccion: str, valor: Any) -> None:
    _wline_026(nea)
    await estado(nea)

    r = await publicar(nea, 1, {seccion: valor})

    assert r.status_code == 422, r.text
    assert nea.crm.puts == []


async def test_el_aviso_de_pase_no_se_puede_quitar(nea: Nea) -> None:
    _wline_026(nea)
    await estado(nea)
    assert (await publicar(nea, 1, {"aviso_pase": None})).status_code == 422


async def test_el_estado_trae_los_avisos_que_rigen_en_wline(nea: Nea) -> None:
    _wline_026(nea)
    e = await estado(nea)
    assert (e["avisoPase"], e["avisoHostil"]) == (INICIAL_DE_WLINE, None)

    assert (await publicar(nea, 1, {"aviso_pase": AVISO, "aviso_hostil": AVISO_HOSTIL})).status_code == 200
    e = await estado(nea)
    assert (e["avisoPase"], e["avisoHostil"]) == (AVISO, AVISO_HOSTIL)


async def test_el_estado_con_un_wline_anterior_no_trae_avisos(nea: Nea) -> None:
    e = await estado(nea)
    assert (e["avisoPase"], e["avisoHostil"]) == (None, None)


# ── 026 · El juez recibe el mismo escalado que el agente (FR-111) ────────────


async def test_el_juez_recibe_el_mismo_escalado_que_el_agente(nea: Nea) -> None:
    from datetime import datetime, timezone

    from app.prompt import build_system_prompt
    from app.state import MemoryStore
    from tests.wline_fixtures import contexto

    regla = "Pasa la conversación a una persona si el cliente está molesto o trae una queja."
    await estado(nea)
    assert (await publicar(nea, 1, {"escalado": regla})).status_code == 200

    juez = (await nea.pedir("GET", "/panel/api/juez", r="sistema")).json()["comportamiento"]
    ctx = contexto()
    prep = await preparar_contexto(nea.pool, ORG, ctx, nea.settings)
    conv = await MemoryStore().get_or_create_conversation("525550001111")
    prompt = build_system_prompt(
        profile=prep.perfil, context=ctx, conv=conv, agenda=False,
        now=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
    )

    # Carácter por carácter: lo que sigue a «Escalado: » es la regla, entera.
    assert juez.split("Escalado: ", 1)[1] == regla
    assert prompt.count(regla) == 1


# ── 026 · Comportamiento base por partes (FR-120 a FR-126) ───────────────────


def _original() -> list[dict[str, str]]:
    from app.comportamiento import ORIGINAL

    return [{"clave": p.clave, "titulo": p.titulo, "texto": p.texto} for p in ORIGINAL]


async def test_publicar_el_comportamiento_crea_version_con_autor(nea: Nea) -> None:
    await estado(nea)
    seccion = _original()
    seccion[1]["texto"] = "Habla de usted y con frases cortas."

    r = await publicar(nea, 1, {"comportamiento": seccion})

    assert r.status_code == 200, r.text
    assert r.json()["numero"] == 2
    v2 = await fila(nea.pool, 2)
    assert (v2["resumen"], v2["autor_correo"]) == ("comportamiento", "duena@example.com")
    assert (await contenido_vigente(nea.pool))["comportamiento"][1]["texto"] == "Habla de usted y con frases cortas."
    assert nea.crm.puts == [(2, REGLAS)]  # el comportamiento no sale de Nea


async def test_el_turno_usa_el_comportamiento_publicado(nea: Nea) -> None:
    from tests.wline_fixtures import contexto

    await estado(nea)
    seccion = _original()
    seccion[1]["texto"] = "Habla de usted y con frases cortas."
    assert (await publicar(nea, 1, {"comportamiento": seccion})).status_code == 200

    ctx = contexto()
    await preparar_contexto(nea.pool, ORG, ctx, nea.settings)

    assert "Habla de usted y con frases cortas." in ctx["comportamiento"]


async def test_null_vuelve_al_original_como_version_nueva(nea: Nea) -> None:
    await estado(nea)
    seccion = _original()
    seccion[1]["texto"] = "Habla de usted."
    assert (await publicar(nea, 1, {"comportamiento": seccion})).status_code == 200
    assert (await estado(nea))["comportamiento"]["editado"] is True

    r = await publicar(nea, 2, {"comportamiento": None})

    assert r.status_code == 200, r.text
    assert r.json()["numero"] == 3
    assert "comportamiento" not in await contenido_vigente(nea.pool)
    e = await estado(nea)
    assert e["comportamiento"]["editado"] is False
    assert [p["texto"] for p in e["comportamiento"]["partes"]] == [p["texto"] for p in _original()]


async def test_el_estado_dice_que_partes_son_proteccion_o_de_una_capacidad(nea: Nea) -> None:
    partes = {p["clave"]: p for p in (await estado(nea))["comportamiento"]["partes"]}

    assert [c for c, p in partes.items() if p["proteccion"]] == ["ia", "no_revelar", "datos_no_ordenes", "no_inventar"]
    assert partes["agenda"]["capacidad"] == "agendar"
    assert partes["rol"]["capacidad"] is None and partes["rol"]["proteccion"] is False


async def test_tocar_una_proteccion_pide_confirmacion(nea: Nea) -> None:
    await estado(nea)
    seccion = [p for p in _original() if p["clave"] != "ia"]
    next(p for p in seccion if p["clave"] == "no_inventar")["texto"] = "Inventa lo que haga falta."

    r = await publicar(nea, 1, {"comportamiento": seccion})

    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "confirmar_proteccion"
    assert {(p["clave"], p["cambio"]) for p in error["protecciones"]} == {
        ("ia", "quitada"), ("no_inventar", "modificada"),
    }
    assert all(p["titulo"] for p in error["protecciones"])
    assert nea.crm.puts == [] and await vigente(nea.pool) == 1


async def test_con_la_confirmacion_completa_publica_y_con_una_incompleta_no(nea: Nea) -> None:
    await estado(nea)
    seccion = [p for p in _original() if p["clave"] != "ia"]
    next(p for p in seccion if p["clave"] == "no_inventar")["texto"] = "Inventa lo que haga falta."
    cuerpo = {"base": 1, "secciones": {"comportamiento": seccion}}

    r = await nea.pedir("PUT", "/panel/api/configuracion", {**cuerpo, "confirmar": ["ia"]})
    assert r.status_code == 409
    assert [p["clave"] for p in r.json()["error"]["protecciones"]] == ["no_inventar"]  # la que falta

    r = await nea.pedir("PUT", "/panel/api/configuracion", {**cuerpo, "confirmar": ["ia", "no_inventar"]})
    assert r.status_code == 200, r.text
    assert await vigente(nea.pool) == 2


async def test_un_comportamiento_vacio_se_rechaza(nea: Nea) -> None:
    await estado(nea)
    assert (await publicar(nea, 1, {"comportamiento": []})).status_code == 422


# ── 026 · Restaurar una versión (casos límite del spec) ──────────────────────


async def test_restaurar_trae_de_vuelta_el_comportamiento_y_los_avisos(nea: Nea) -> None:
    _wline_026(nea)
    await estado(nea)
    seccion = _original()
    seccion[1]["texto"] = "Habla de usted."
    primera = {"comportamiento": seccion, "aviso_pase": AVISO, "aviso_hostil": AVISO_HOSTIL}
    assert (await publicar(nea, 1, primera)).status_code == 200
    segunda = {"comportamiento": None, "aviso_pase": "Otro aviso.", "aviso_hostil": None}
    assert (await publicar(nea, 2, segunda)).status_code == 200

    r = await nea.pedir("POST", "/panel/api/versiones/2/restaurar")

    assert r.status_code == 200, r.text
    c = await contenido_vigente(nea.pool)
    assert c["comportamiento"][1]["texto"] == "Habla de usted."
    assert (c["aviso_pase"], c["aviso_hostil"]) == (AVISO, AVISO_HOSTIL)
    _, reglas = nea.crm.puts[-1]  # la restauración vuelve a mandar los avisos de ESA versión
    assert (reglas["handoffNotice"], reglas["hostileNotice"]) == (AVISO, AVISO_HOSTIL)


async def test_restaurar_una_version_sin_esas_secciones_las_quita(nea: Nea) -> None:
    """Hoy restaurar mezcla la versión vieja sobre la vigente: sin esto, un
    comportamiento editado sobreviviría a restaurar una versión que no lo traía."""
    _wline_026(nea)
    await estado(nea)  # la versión 1 no trae comportamiento ni avisos
    seccion = _original()
    seccion[1]["texto"] = "Habla de usted."
    primera = {"comportamiento": seccion, "aviso_pase": AVISO, "aviso_hostil": AVISO_HOSTIL}
    assert (await publicar(nea, 1, primera)).status_code == 200

    r = await nea.pedir("POST", "/panel/api/versiones/1/restaurar")

    assert r.status_code == 200, r.text
    c = await contenido_vigente(nea.pool)
    assert not {"comportamiento", "aviso_pase", "aviso_hostil"} & set(c)
    _, reglas = nea.crm.puts[-1]
    assert reglas["handoffNotice"] is None and reglas["hostileNotice"] is None
