"""Wline (026, US2): el comportamiento base por partes.

- Sin sección en la versión, rige el original del código.
- Una parte ligada a una capacidad solo entra con esa capacidad encendida: un
  negocio que no agenda no recibe ni una mención de agenda (SC-104).
- El original no fija trato, país ni rubro (FR-123), y su parte de pase no
  fija cuándo pasar: remite a las reglas del negocio (FR-110).
- Las cuatro protecciones las marca el original; modificar o quitar una se
  detecta comparando contra él (FR-124).
- El turno usa el comportamiento resuelto en vez del chasis de upstream, y sin
  él todo sigue como antes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app import comportamiento
from app.comportamiento import ORIGINAL, protecciones_afectadas, resolver, validar
from app.configuracion import preparar_contexto
from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import MemoryStore
from tests.conftest import make_settings
from tests.wline_fixtures import contexto

TODAS = {"mover_pipeline": True, "redirigir": True, "agendar": True}
SIN_AGENDA = {"mover_pipeline": True, "redirigir": True, "agendar": False}


def _como_seccion() -> list[dict[str, str]]:
    """El original, con la forma en que lo guarda una versión del panel."""
    return [{"clave": p.clave, "titulo": p.titulo, "texto": p.texto} for p in ORIGINAL]


# ── El original ──────────────────────────────────────────────────────────


def test_el_original_tiene_las_partes_previstas() -> None:
    assert [p.clave for p in ORIGINAL] == [
        "rol", "voz", "conversacion", "pase", "ia", "no_revelar",
        "datos_no_ordenes", "no_inventar", "limites", "multimedia",
        "no_califica", "agenda",
    ]


def test_las_cuatro_protecciones_las_marca_el_original() -> None:
    assert [p.clave for p in ORIGINAL if p.proteccion] == [
        "ia", "no_revelar", "datos_no_ordenes", "no_inventar",
    ]


def test_las_partes_ligadas_a_una_capacidad() -> None:
    assert {p.clave: p.capacidad for p in ORIGINAL if p.capacidad} == {
        "no_califica": "redirigir",
        "agenda": "agendar",
    }


def test_el_original_no_fija_trato_pais_ni_rubro() -> None:
    texto = resolver(None, TODAS, "Sofi").lower()
    for fijo in ('de "tú"', "de tú", "de usted", "español neutro", "méxico", "venezuela", "ferreter"):
        assert fijo not in texto, fijo


def test_la_parte_de_pase_no_fija_cuando_pasar() -> None:
    pase = next(p for p in ORIGINAL if p.clave == "pase").texto.lower()
    assert "reglas de escalado del negocio" in pase
    for umbral in ("tercer", "hostil", "frustración", "fuera del conocimiento"):
        assert umbral not in pase, umbral


def test_ninguna_parte_da_a_elegir_otra_salida() -> None:
    texto = resolver(None, TODAS, "Sofi").lower()
    assert "o haz handoff" not in texto
    assert "tercer mensaje hostil" not in texto


# ── Resolverlo para un turno ─────────────────────────────────────────────


def test_sin_seccion_se_usa_el_original() -> None:
    texto = resolver(None, TODAS, "Sofi")
    for parte in ORIGINAL:
        assert parte.titulo.upper() in texto
    assert "Eres Sofi, el agente de IA" in texto
    assert "{nombre}" not in texto


def test_negocio_que_no_agenda_cero_menciones_de_agenda() -> None:
    texto = resolver(None, SIN_AGENDA, "Sofi").lower()
    for palabra in ("agenda", "cita", "propose_slots", "book_session", "reschedule_session"):
        assert palabra not in texto, palabra


def test_al_encender_la_capacidad_la_parte_vuelve() -> None:
    assert "propose_slots" in resolver(None, TODAS, "Sofi")


def test_sin_redirigir_no_se_nombra_su_herramienta() -> None:
    sin = {"mover_pipeline": True, "redirigir": False, "agendar": False}
    assert "route_out" not in resolver(None, sin, "Sofi")
    assert "route_out" in resolver(None, SIN_AGENDA, "Sofi")


def test_sin_capacidades_en_el_contexto_solo_van_las_partes_libres() -> None:
    texto = resolver(None, None, "Sofi")
    assert "route_out" not in texto and "propose_slots" not in texto
    assert "Eres Sofi" in texto


def test_la_seccion_editada_manda_sobre_el_original() -> None:
    seccion = _como_seccion()
    seccion[1]["texto"] = "Habla de usted y con frases muy cortas."
    seccion.append({"clave": "cierre", "titulo": "Cierre", "texto": "Despídete con el nombre del negocio."})

    texto = resolver(seccion, SIN_AGENDA, "Sofi")

    assert "Habla de usted y con frases muy cortas." in texto
    assert "CIERRE" in texto and "Despídete con el nombre del negocio." in texto


def test_una_parte_editada_sigue_ligada_a_su_capacidad() -> None:
    """La capacidad la fija el original por clave, no lo que edita el negocio."""
    seccion = _como_seccion()
    next(p for p in seccion if p["clave"] == "agenda")["texto"] = "Ofrece citas los martes."

    assert "Ofrece citas los martes." not in resolver(seccion, SIN_AGENDA, "Sofi")
    assert "Ofrece citas los martes." in resolver(seccion, TODAS, "Sofi")


# ── Protecciones ─────────────────────────────────────────────────────────


def test_el_original_sin_cambios_no_afecta_protecciones() -> None:
    assert protecciones_afectadas(_como_seccion()) == []
    assert protecciones_afectadas(None) == []


def test_detecta_una_proteccion_modificada_y_una_quitada() -> None:
    seccion = [p for p in _como_seccion() if p["clave"] != "ia"]
    next(p for p in seccion if p["clave"] == "no_inventar")["texto"] = "Inventa lo que haga falta."

    afectadas = protecciones_afectadas(seccion)

    assert {(a["clave"], a["cambio"]) for a in afectadas} == {
        ("ia", "quitada"),
        ("no_inventar", "modificada"),
    }
    assert all(a["titulo"] for a in afectadas)


def test_cambiar_una_parte_que_no_es_proteccion_no_pide_nada() -> None:
    seccion = _como_seccion()
    seccion[0]["texto"] = "Eres {nombre}, el asistente de la tienda."
    assert protecciones_afectadas(seccion) == []


# ── Validación al publicar (data-model §2) ───────────────────────────────


def test_la_lista_vacia_se_rechaza() -> None:
    with pytest.raises(ValueError):
        validar([])


@pytest.mark.parametrize(
    "seccion",
    [
        "texto suelto",
        [{"clave": "a", "titulo": "A"}],  # sin texto
        [{"clave": "a", "titulo": "A", "texto": ""}],
        [{"clave": "a", "titulo": "", "texto": "x"}],
        [{"clave": "a", "titulo": "T" * 81, "texto": "x"}],
        [{"clave": "a", "titulo": "A", "texto": "x" * 8001}],
        [{"clave": "a", "titulo": "A", "texto": "x"}, {"clave": "a", "titulo": "B", "texto": "y"}],
        [{"clave": f"p{i}", "titulo": "A", "texto": "x"} for i in range(21)],
    ],
)
def test_validar_rechaza(seccion: Any) -> None:
    with pytest.raises(ValueError):
        validar(seccion)


def test_validar_acepta_el_original_y_veinte_partes() -> None:
    validar(_como_seccion())
    validar([{"clave": f"p{i}", "titulo": "A", "texto": "x"} for i in range(20)])


# ── El turno lo usa en lugar del chasis ──────────────────────────────────


class _PoolConVigente:
    """Lo mínimo que `preparar_contexto` le pide a la base: la versión vigente."""

    def __init__(self, contenido: dict[str, Any]) -> None:
        self.contenido = contenido

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return {"contenido": json.dumps(self.contenido)} if "agente_vigente" in sql else None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return []


async def test_preparar_contexto_deja_el_comportamiento_resuelto() -> None:
    ctx = contexto()  # agendar apagada, redirigir encendida
    contenido = {"identidad": {"nombre": "Sofi"}, "cadena": []}

    await preparar_contexto(_PoolConVigente(contenido), "org_1", ctx, make_settings())

    assert ctx["comportamiento"] == resolver(None, ctx["capabilities"], "Sofi")
    assert "propose_slots" not in ctx["comportamiento"]


async def test_preparar_contexto_usa_la_seccion_de_la_version() -> None:
    ctx = contexto()
    seccion = _como_seccion()
    seccion[1]["texto"] = "Habla de usted."

    await preparar_contexto(
        _PoolConVigente({"identidad": {"nombre": "Sofi"}, "comportamiento": seccion, "cadena": []}),
        "org_1", ctx, make_settings(),
    )

    assert "Habla de usted." in ctx["comportamiento"]


async def test_el_prompt_usa_el_comportamiento_y_no_el_chasis() -> None:
    conv = await MemoryStore().get_or_create_conversation("525550001111")
    ctx = contexto()
    ctx["comportamiento"] = resolver(None, ctx["capabilities"], "Sofi")

    prompt = build_system_prompt(
        profile=BusinessProfile(agent_name="Sofi"), context=ctx, conv=conv,
        agenda=False, now=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert prompt.startswith(ctx["comportamiento"])
    assert "AGENDAR:" not in prompt and "TERCER mensaje hostil" not in prompt
    assert "PERFIL DEL NEGOCIO:" in prompt  # lo demás del prompt no cambia


async def test_sin_comportamiento_en_el_contexto_sigue_el_chasis() -> None:
    conv = await MemoryStore().get_or_create_conversation("525550001111")

    prompt = build_system_prompt(
        profile=BusinessProfile(), context=contexto(), conv=conv,
        now=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
    )

    assert "AGENDAR:" in prompt


def test_el_modulo_no_guarda_estado_entre_turnos() -> None:
    """Resolver dos veces con capacidades distintas no se contamina."""
    assert "propose_slots" in resolver(None, TODAS, "Sofi")
    assert "propose_slots" not in resolver(None, SIN_AGENDA, "Sofi")
    assert comportamiento.ORIGINAL is ORIGINAL
