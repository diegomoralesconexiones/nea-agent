"""Wline (025, US1): la configuración del agente que guarda el panel, leída por el turno.

Nada de HTTP aquí: el turno no depende del panel (FR-007). La única entrada es
`preparar_contexto`, que da todo lo que el turno toma de la versión vigente: el
perfil, la cadena de respaldo y la mezcla de «cómo atenderlo» por tipo. La usan
el turno y, en la fase 2, la vista de contexto completo, para que la vista
muestre exactamente lo que el turno usa.

Dos consultas por turno y sin caché (pre-plan §7): la vigente, y las
plataformas de su cadena con sus certificaciones.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.cadena import TOPE_INTENTO, Eslabon, LlmCadena, Plataforma
from app.llm import OpenAiLlm
from app.profile import BusinessProfile

logger = logging.getLogger("nea.configuracion")


# ── Las llaves de las plataformas (R7) ───────────────────────────────────


class SinLlaveDeCifrado(RuntimeError):
    """NEA_ENCRYPTION_KEY falta o no sirve: no se guardan ni se leen llaves."""


def _aesgcm(clave_b64: str) -> AESGCM:
    try:
        clave = base64.b64decode(clave_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        clave = b""
    if len(clave) != 32:
        raise SinLlaveDeCifrado(
            "NEA_ENCRYPTION_KEY no está configurada (32 bytes en base64): sin ella "
            "no se pueden registrar ni usar las llaves de las plataformas"
        )
    return AESGCM(clave)


def cifrar(llave: str, clave_b64: str) -> tuple[bytes, str]:
    """`nonce ‖ cifrado+tag` y los últimos 4, lo único que se muestra."""
    aes = _aesgcm(clave_b64)
    nonce = os.urandom(12)
    return nonce + aes.encrypt(nonce, llave.encode(), None), llave[-4:]


def descifrar(blob: bytes, clave_b64: str) -> str:
    return _aesgcm(clave_b64).decrypt(blob[:12], blob[12:], None).decode()


# ── El perfil y la atención por tipo ─────────────────────────────────────


def _kb(conocimiento: Any) -> str | None:
    """El mismo texto que `renderKb` de Wline; sin entradas, None (su «vacío»)."""
    partes: list[str] = []
    for k in conocimiento or []:
        if not isinstance(k, dict):
            continue
        if k.get("tipo") == "qa":
            partes.append(f"P: {k.get('pregunta') or ''}\nR: {k.get('respuesta') or ''}")
        elif k.get("contenido"):
            partes.append(str(k["contenido"]))
    return "\n\n".join(partes) or None


def perfil_desde_contenido(contenido: dict[str, Any], nombre_por_defecto: str) -> BusinessProfile:
    identidad = contenido.get("identidad") or {}
    return BusinessProfile(
        cloud=True,  # el mismo chasis que con el perfil que mandaba Wline
        agent_name=str(identidad.get("nombre") or nombre_por_defecto),
        tone=identidad.get("tono") or None,
        instructions=contenido.get("instrucciones") or None,
        escalation_rules=contenido.get("escalado") or None,
        greeting=identidad.get("saludo") or None,
        kb_text=_kb(contenido.get("conocimiento")),
    )


def mezclar_atencion(context: dict[str, Any], contenido: dict[str, Any]) -> None:
    """`tipos[key].atencion` → `context.pipeline.lines[].atencion`, por `key`.

    Solo las líneas que llegan en el contexto: un tipo que Wline ya borró se
    ignora desde el siguiente turno (FR-025).
    """
    tipos = contenido.get("tipos") or {}
    for linea in ((context or {}).get("pipeline") or {}).get("lines") or []:
        atencion = str((tipos.get(str(linea.get("key"))) or {}).get("atencion") or "").strip()
        if atencion:
            linea["atencion"] = atencion


@dataclass(frozen=True)
class PerfilFijo:
    """El perfil de la vigente con la interfaz de `ProfileProvider`."""

    perfil: BusinessProfile

    async def get(self) -> BusinessProfile:
        return self.perfil


# ── La preparación del turno ─────────────────────────────────────────────


@dataclass(frozen=True)
class Preparacion:
    perfil: BusinessProfile | None = None  # None = no hay versión vigente
    cadena: LlmCadena | None = None  # None = ningún eslabón certificado utilizable


def _json(valor: Any) -> Any:
    return json.loads(valor) if isinstance(valor, str) else valor


# Un cliente por eslabón, reusado entre turnos: cada uno tiene su pool HTTP, y
# abrirlo en cada turno pagaría el saludo TLS en la primera llamada del modelo.
# ponytail: sin tope; crece con cada configuración distinta que se use.
_CLIENTES: dict[tuple[str, str, str, str], OpenAiLlm] = {}


def _cliente(base_url: str, llave: str, modelo: str, extras: dict[str, Any]) -> OpenAiLlm:
    clave = (base_url, llave, modelo, json.dumps(extras, sort_keys=True))
    if clave not in _CLIENTES:
        _CLIENTES[clave] = OpenAiLlm(
            llave,
            modelo,
            transcribe_model="",
            base_url=base_url,
            # Los ajustes de velocidad de esta Nea no aplican: cada eslabón
            # lleva sus extras, los mismos con que se certificó.
            reasoning_effort="",
            provider_sort="",
            timeout=TOPE_INTENTO,
            reintentos=0,
            extra_body=extras,
        )
    return _CLIENTES[clave]


async def _armar_cadena(
    pool: Any, organization_id: str, eslabones: list[Any], clave_cifrado: str, *, certificados: bool
) -> LlmCadena | None:
    ids = sorted({str(e.get("plataforma_id")) for e in eslabones if isinstance(e, dict)})
    if not ids:
        return None
    filas = await pool.fetch(
        """
        SELECT p.id, p.nombre, p.base_url, p.llave_cifrada,
               c.modelo, c.extras, c.aprobada, c.modelo_respondio, c.requiere_atencion
        FROM plataforma_modelo p
        LEFT JOIN certificacion c
          ON c.organization_id = p.organization_id AND c.plataforma_id = p.id
        WHERE p.organization_id = $1 AND p.id = ANY($2::text[])
        """,
        organization_id,
        ids,
    )
    plataformas = {f["id"]: f for f in filas}
    certificaciones = {(f["id"], f["modelo"]): f for f in filas if f["modelo"] is not None}

    out: list[Eslabon] = []
    for e in eslabones:
        if not isinstance(e, dict):
            continue
        pid, modelo, extras = str(e.get("plataforma_id")), str(e.get("modelo") or ""), e.get("extras") or {}
        p = plataformas.get(pid)
        cert = certificaciones.get((pid, modelo))
        # En v2 Nea solo piensa con eslabones certificados con esos mismos
        # extras (R8, R10). La cadena de prueba es justo la que se certifica.
        if p is None or (certificados and not (cert and cert["aprobada"] and _json(cert["extras"]) == extras)):
            continue
        try:
            llave = descifrar(bytes(p["llave_cifrada"]), clave_cifrado)
        except (SinLlaveDeCifrado, InvalidTag, UnicodeDecodeError) as exc:
            logger.warning("no pude descifrar la llave de %s (%s) — eslabón fuera del turno", p["nombre"], type(exc).__name__)
            continue
        out.append(
            Eslabon(
                Plataforma(pid, p["nombre"], p["base_url"], llave),
                modelo,
                _cliente(p["base_url"], llave, modelo, extras),
                modelo_certificado=cert["modelo_respondio"] if certificados and cert else None,
                requiere_atencion=bool(certificados and cert and cert["requiere_atencion"]),
            )
        )
    return LlmCadena(out, pool=pool, organization_id=organization_id) if out else None


async def preparar_contexto(
    pool: Any,
    organization_id: str,
    context: dict[str, Any],
    settings: Any,
    *,
    es_prueba: bool = False,
) -> Preparacion:
    """Lo que el turno toma de la versión vigente, y la atención mezclada en `context`.

    Los turnos de prueba (`isTest`) piensan con la cadena de prueba mientras no
    venza: así el Laboratorio certifica un eslabón sin tocar la cadena de los
    clientes (R8).
    """
    if pool is None:
        return Preparacion()
    fila = await pool.fetchrow(
        """
        SELECT v.contenido FROM agente_vigente p
        JOIN agente_version v USING (organization_id, numero)
        WHERE p.organization_id = $1
        """,
        organization_id,
    )
    if fila is None:
        return Preparacion()
    contenido = _json(fila["contenido"])
    mezclar_atencion(context, contenido)

    eslabones, certificados = list(contenido.get("cadena") or []), True
    if es_prueba:
        prueba = await pool.fetchrow(
            "SELECT eslabon FROM cadena_prueba WHERE organization_id = $1 AND vence_en > now()",
            organization_id,
        )
        if prueba is not None:
            eslabones, certificados = [_json(prueba["eslabon"])], False
    return Preparacion(
        perfil=perfil_desde_contenido(contenido, settings.agent_name),
        cadena=await _armar_cadena(
            pool, organization_id, eslabones, settings.nea_encryption_key, certificados=certificados
        ),
    )
