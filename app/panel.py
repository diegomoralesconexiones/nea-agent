"""Wline (025, US1): el panel de control del agente, dentro de Nea.

Se entra por `/panel/*` de Wline, que valida la sesión y el rol (Propietario o
Admin) en cada petición y la reenvía firmada (contracts/panel.md). Nea no
tiene acceso público (024): sin la firma de Wline no se sirve ni la página.

Aquí viven la firma, la página estática y la API JSON que guarda, publica y
restaura versiones de la configuración (R5). Nada corre en reposo: son rutas
(FR-005). El turno no importa nada de este módulo (FR-007, FR-016).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncpg
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app import cadena, comportamiento
from app.cadena import Plataforma
from app.configuracion import SinLlaveDeCifrado, _json, _kb, cifrar, descifrar
from app.crm import CrmConflict, CrmError

logger = logging.getLogger("nea.panel")

ESTATICOS = Path(__file__).parent / "panel_static"
CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"
VENTANA_FIRMA = 60  # segundos de desfase aceptados entre Wline y Nea

# El reloj, aparte para que las pruebas lo fijen.
_ahora = time.time


class ErrorPanel(Exception):
    """`{error: {code, message, ...}}`, con el mensaje en español tal cual se muestra."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def _cabeceras(ruta: str) -> dict[str, str]:
    # La página corre en el origen de Wline: un XSS aquí sería un XSS en Wline.
    cabeceras = {"content-security-policy": CSP}
    if ruta.startswith("/panel/api/"):
        cabeceras["cache-control"] = "no-store"
    return cabeceras


async def _manejar_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ErrorPanel)
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message, **exc.extra}},
        status_code=exc.status,
        headers=_cabeceras(request.url.path),
    )


# ── La firma de Wline (contracts/panel.md §2) ────────────────────────────


def firma_esperada(secreto: str, metodo: str, ruta: str, resumen: str, cabecera: str) -> str:
    """HMAC-SHA256 con separación de dominio: una firma de despacho no vale aquí."""
    mensaje = f"wline:panel:v1\n{metodo}\n{ruta}\n{resumen}\n{cabecera}"
    return hmac.new(secreto.encode(), mensaje.encode(), hashlib.sha256).hexdigest()


def rol_permitido(rol: str, metodo: str, ruta: str) -> bool:
    """Propietario y Admin (FR-003); el juez del Laboratorio, solo `sistema` (R11)."""
    if ruta == "/panel/api/juez" or rol == "sistema":
        return rol == "sistema" and metodo == "GET" and ruta == "/panel/api/juez"
    return rol in ("owner", "admin")


@dataclass(frozen=True)
class Quien:
    """Quién pide, según la cabecera firmada. `organizacion` es el id de Wline:
    el mismo con que el turno lee la vigente, no el slug de CRM_ORGANIZATION."""

    id: str
    correo: str
    rol: str
    organizacion: str


def _ruta_firmada(request: Request) -> str:
    # Tal como llegó, sin decodificar, con su consulta: es lo que firmó Wline.
    ruta = (request.scope.get("raw_path") or request.url.path.encode()).decode("latin-1")
    consulta = (request.scope.get("query_string") or b"").decode("latin-1")
    return f"{ruta}?{consulta}" if consulta else ruta


def _rechazo(motivo: str) -> ErrorPanel:
    logger.warning("panel: petición rechazada (%s)", motivo)
    return ErrorPanel(401, "firma_invalida", "La petición no viene firmada por Wline")


async def firma(request: Request, response: Response) -> Quien:
    """Dependencia de TODAS las rutas del panel. Solo mira cabeceras: el
    resumen del cuerpo llega en `x-contenido-sha256` y cada ruta compara su
    cuerpo al leerlo (`cuerpo_firmado`; research-fase2 F1)."""
    secreto = request.app.state.ctx.settings.crm_brain_secret
    cabecera = request.headers.get("x-wline-panel", "")
    recibida = request.headers.get("x-wline-panel-firma", "")
    resumen = request.headers.get("x-contenido-sha256", "")
    if not (secreto and cabecera and recibida and resumen):
        raise _rechazo("faltan cabeceras o CRM_BRAIN_SECRET")
    esperada = firma_esperada(secreto, request.method, _ruta_firmada(request), resumen, cabecera)
    if not hmac.compare_digest(recibida, esperada):
        raise _rechazo("firma que no coincide")
    try:
        datos = json.loads(base64.urlsafe_b64decode(cabecera + "=" * (-len(cabecera) % 4)))
        quien = Quien(str(datos["u"]), str(datos["e"]), str(datos["r"]), str(datos["o"]))
        t = float(datos["t"])
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise _rechazo("cabecera de identidad ilegible") from None
    if abs(_ahora() - t) > VENTANA_FIRMA:
        raise _rechazo("fuera de la ventana de 60 s")
    if not (quien.organizacion and rol_permitido(quien.rol, request.method, request.url.path)):
        raise _rechazo(f"rol {quien.rol!r}")
    response.headers.update(_cabeceras(request.url.path))
    return quien


async def cuerpo_firmado(request: Request) -> Any:
    """El cuerpo JSON, si coincide con el resumen firmado."""
    cuerpo = await request.body()
    if not hmac.compare_digest(hashlib.sha256(cuerpo).hexdigest(), request.headers.get("x-contenido-sha256", "")):
        raise _rechazo("cuerpo que no coincide con su resumen")
    try:
        return json.loads(cuerpo or b"null")
    except ValueError:
        raise ErrorPanel(422, "invalido", "El cuerpo no es JSON válido") from None


router = APIRouter(prefix="/panel", dependencies=[Depends(firma)])


# ── La página (contracts/panel.md §3) ────────────────────────────────────


@router.get("")
@router.get("/")
async def pagina() -> FileResponse:
    # Las dos, sin redirigir: Next quita la barra final, y una redirección de
    # FastAPI mandaría el host interno de Nea.
    return FileResponse(ESTATICOS / "index.html", headers={"content-security-policy": CSP})


_ARCHIVOS = {"panel.js": "text/javascript", "panel.css": "text/css"}


@router.get("/static/{nombre}")
async def estatico(nombre: str) -> FileResponse:
    if nombre not in _ARCHIVOS:
        raise ErrorPanel(404, "no_encontrado", "No existe ese archivo")
    return FileResponse(ESTATICOS / nombre, media_type=_ARCHIVOS[nombre], headers={"content-security-policy": CSP})


# ── Versiones y publicación (R5, R17; data-model §2 y §3) ────────────────

# Las publicaciones van en fila: un candado por app, creado en `montar`.
# ponytail: vale con un proceso (uvicorn sin --workers); con varios,
# `pg_advisory_lock` por organización.
def _candado(request: Request) -> asyncio.Lock:
    lock: asyncio.Lock = request.app.state.candado_del_panel
    return lock

SECCIONES = (
    "identidad", "instrucciones", "escalado", "conocimiento", "tipos",
    "ficha", "umbral", "capacidades", "cadena",
    "comportamiento", "aviso_pase", "aviso_hostil",
)
# 026: secciones en las que «ausente» significa algo (el comportamiento
# original, el aviso inicial de Wline, sin aviso hostil). Publicarlas con
# `None` las quita de la versión nueva.
OPCIONALES = ("comportamiento", "aviso_pase", "aviso_hostil")
MAX_AVISO = 300
MIGRACION = ("migración", "migración")
SISTEMA = ("sistema", "sistema")
_WLINE_CAIDO = "No pude hablar con Wline y no se guardó nada en ningún lado. Intenta de nuevo en un momento."


def _iso(valor: datetime | None) -> str | None:
    return valor.isoformat() if valor else None


def _invalido(mensaje: str) -> ErrorPanel:
    return ErrorPanel(422, "invalido", mensaje)


async def _automation(crm: Any) -> dict[str, Any]:
    try:
        return await crm.automation_get()
    except CrmError as exc:
        logger.warning("panel: no pude leer automation de Wline (%s)", exc)
        raise ErrorPanel(502, "wline_inalcanzable", _WLINE_CAIDO) from None


async def _vigente(pool: Any, org: str) -> Any:
    return await pool.fetchrow(
        """
        SELECT v.numero, v.contenido, v.autor_correo, v.creada_en
        FROM agente_vigente p JOIN agente_version v USING (organization_id, numero)
        WHERE p.organization_id = $1
        """,
        org,
    )


def _reglas(contenido: dict[str, Any], auto: dict[str, Any] | None = None) -> dict[str, Any]:
    """Lo que Wline hace cumplir (R2); todo lo demás se queda en Nea.

    026: los dos avisos viajan con las reglas, el texto de ESTA versión o
    `None` si no trae la sección. Solo si Wline los anuncia en su `GET`
    (`rules.handoffNotice`): el Wline anterior a la 026 valida las reglas con
    un esquema cerrado y rechazaría la publicación, y Nea se despliega antes.
    """
    reglas: dict[str, Any] = {
        "fichaFields": contenido.get("ficha") or [],
        "threshold": contenido.get("umbral") or "Clasificado",
        "capabilities": contenido.get("capacidades") or {},
        "lineCriteria": {
            str(k): str((t or {}).get("criterio") or "").strip() or None
            for k, t in (contenido.get("tipos") or {}).items()
        },
    }
    if "handoffNotice" in ((auto or {}).get("rules") or {}):
        reglas["handoffNotice"] = contenido.get("aviso_pase") or None
        reglas["hostileNotice"] = contenido.get("aviso_hostil") or None
    return reglas


def _con_tipo_cliente_de_wline(contenido: dict[str, Any], auto: dict[str, Any]) -> dict[str, Any]:
    """`tipo_cliente` es del sistema: sus opciones son los tipos vigentes en
    Wline, que rechaza cualquier otra cosa. Se toma de Wline al publicar, así
    un tipo creado o renombrado allá no bloquea las publicaciones del panel."""
    sistema = next(
        (f for f in (auto.get("rules") or {}).get("fichaFields") or []
         if isinstance(f, dict) and f.get("key") == "tipo_cliente"),
        None,
    )
    if sistema is None:
        return contenido
    ficha = [f for f in contenido.get("ficha") or [] if isinstance(f, dict)]
    if any(f.get("key") == "tipo_cliente" for f in ficha):
        ficha = [sistema if f.get("key") == "tipo_cliente" else f for f in ficha]
    else:
        ficha = [sistema, *ficha]
    return {**contenido, "ficha": ficha}


async def _publicar(
    ctx: Any,
    org: str,
    contenido: dict[str, Any],
    resumen: str,
    autor: tuple[str, str],
    auto: dict[str, Any],
    restaurada_de: int | None = None,
) -> int:
    """Insertar → `PUT` a Wline → mover el puntero. Con el candado tomado.

    `auto` es el `GET /api/bot/automation` leído justo antes, dentro del mismo
    candado: el número nuevo es `max(local, appliedVersion) + 1`, así nunca se
    repite uno que Wline ya aplicó (R5). Si algo falla, la fila se borra y el
    puntero no se mueve: nada queda a medias (FR-008, SC-017).
    """
    pool, crm = ctx.store.pool, ctx.crm
    contenido = _con_tipo_cliente_de_wline(contenido, auto)
    local = await pool.fetchval(
        "SELECT coalesce(max(numero), 0) FROM agente_version WHERE organization_id = $1", org
    )
    numero = max(local, int(auto.get("appliedVersion") or 0)) + 1
    try:
        await pool.execute(
            """INSERT INTO agente_version
               (organization_id, numero, contenido, resumen, autor_id, autor_correo, restaurada_de)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            org, numero, json.dumps(contenido), resumen, autor[0], autor[1], restaurada_de,
        )
    except asyncpg.UniqueViolationError:
        raise ErrorPanel(409, "conflicto", "Otra publicación tomó ese número. Recarga el panel.") from None
    movido = False
    try:
        try:
            await crm.automation_put(numero, _reglas(contenido, auto))
        except CrmConflict as exc:
            if exc.code == "version_vieja":
                raise ErrorPanel(409, "conflicto", "Wline ya tiene una versión más nueva. Recarga el panel.") from None
            error = exc.payload.get("error")
            mensaje = error.get("message") if isinstance(error, dict) else None
            raise ErrorPanel(422, exc.code, str(mensaje or "Wline rechazó las reglas")) from None
        except CrmError as exc:
            # Venció o falló la red: ¿Wline la aplicó? Si la relectura dice N,
            # sí; si no, o si tampoco responde, no se publicó.
            try:
                aplicada = (await crm.automation_get()).get("appliedVersion")
            except CrmError:
                aplicada = None
            if aplicada != numero:
                logger.warning("panel: la versión %s no llegó a Wline (%s)", numero, exc)
                raise ErrorPanel(502, "wline_inalcanzable", _WLINE_CAIDO) from None
        await pool.execute(
            """INSERT INTO agente_vigente (organization_id, numero) VALUES ($1, $2)
               ON CONFLICT (organization_id) DO UPDATE SET numero = EXCLUDED.numero, actualizada_en = now()""",
            org, numero,
        )
        movido = True
    finally:
        if not movido:
            await pool.execute(
                "DELETE FROM agente_version WHERE organization_id = $1 AND numero = $2", org, numero
            )
    logger.info("panel: versión %s publicada (%s)", numero, resumen)
    return numero


async def _publicar_sin_bloquear(ctx: Any, org: str, contenido: dict[str, Any], resumen: str, auto: dict[str, Any], avisos: list[str]) -> bool:
    """Las publicaciones automáticas al abrir: si Wline las rechaza, el panel abre igual y lo dice."""
    try:
        await _publicar(ctx, org, contenido, resumen, SISTEMA, auto)
        return True
    except ErrorPanel as exc:
        avisos.append(f"No pude publicar «{resumen}»: {exc.message}")
        return False


def _texto(valor: Any, tope: int, donde: str) -> None:
    if not isinstance(valor, str):
        raise _invalido(f"«{donde}» debe ser texto")
    if len(valor) > tope:
        raise _invalido(f"«{donde}» pasa de {tope} caracteres")


def _validar(seccion: str, valor: Any, plataformas: set[str]) -> None:
    """Límites de data-model §2.6. Las reglas (ficha, umbral, capacidades) las valida Wline al publicar."""
    if seccion == "identidad":
        if not isinstance(valor, dict):
            raise _invalido("La identidad lleva nombre, tono y saludo")
        for k in ("nombre", "tono", "saludo"):
            _texto(valor.get(k, ""), 4000, f"identidad.{k}")
    elif seccion in ("instrucciones", "escalado"):
        _texto(valor, 8000, seccion)
    elif seccion == "conocimiento":
        if not isinstance(valor, list) or len(valor) > 500:
            raise _invalido("El conocimiento es una lista de hasta 500 entradas")
        for i, e in enumerate(valor, 1):
            if not isinstance(e, dict) or e.get("tipo") not in ("qa", "bloque"):
                raise _invalido(f"La entrada {i} del conocimiento no es pregunta y respuesta ni bloque")
            for k in ("pregunta", "respuesta") if e["tipo"] == "qa" else ("contenido",):
                _texto(e.get(k, ""), 4000, f"conocimiento {i}, {k}")
    elif seccion == "tipos":
        if not isinstance(valor, dict) or not all(isinstance(t, dict) for t in valor.values()):
            raise _invalido("Los tipos de cliente llevan criterio y atención")
        for key, t in valor.items():
            for k in ("criterio", "atencion"):
                _texto(t.get(k, ""), 4000, f"{key}, {k}")
    elif seccion == "ficha":
        if not isinstance(valor, list):
            raise _invalido("La ficha es una lista de campos")
    elif seccion == "umbral":
        _texto(valor, 80, "umbral")
    elif seccion == "capacidades":
        if not isinstance(valor, dict) or not all(isinstance(v, bool) for v in valor.values()):
            raise _invalido("Cada capacidad se enciende o se apaga")
    elif seccion == "cadena":
        if not isinstance(valor, list) or len(valor) > 6:
            raise _invalido("La cadena tiene de 0 a 6 eslabones")
        for i, e in enumerate(valor, 1):
            if not (
                isinstance(e, dict)
                and isinstance(e.get("modelo"), str) and e["modelo"].strip() and len(e["modelo"]) <= 200
                and isinstance(e.get("extras", {}), dict)
            ):
                raise _invalido(f"El eslabón {i} necesita plataforma, modelo y extras")
            if len(json.dumps(e.get("extras") or {})) > 2048:
                raise _invalido(f"Los extras del eslabón {i} pasan de 2 KB")
            if e.get("plataforma_id") not in plataformas:
                raise _invalido(f"El eslabón {i} usa una plataforma que no existe")
    elif seccion == "comportamiento":
        # 026 (FR-121, FR-126): `None` es «volver al original»; vacío no se publica.
        if valor is not None:
            try:
                comportamiento.validar(valor)
            except ValueError as exc:
                raise _invalido(str(exc)) from None
    elif seccion in ("aviso_pase", "aviso_hostil"):
        # 026 (FR-117, FR-119): el de pase nunca queda vacío; el hostil es
        # opcional y `None` lo quita (entonces sale el aviso de pase).
        if valor is None and seccion == "aviso_hostil":
            return
        nombre = "aviso de pase" if seccion == "aviso_pase" else "aviso para mensajes hostiles"
        if not isinstance(valor, str) or not valor.strip():
            raise _invalido(f"El {nombre} no puede quedar vacío")
        if len(valor) > MAX_AVISO:
            raise _invalido(f"El {nombre} pasa de {MAX_AVISO} caracteres")
    else:
        raise _invalido(f"«{seccion}» no es una sección del panel")


async def _ids_de_plataformas(pool: Any, org: str) -> set[str]:
    filas = await pool.fetch("SELECT id FROM plataforma_modelo WHERE organization_id = $1", org)
    return {f["id"] for f in filas}


async def _sin_certificar(pool: Any, org: str, cadena: list[dict[str, Any]]) -> dict[str, Any] | None:
    """El primer eslabón sin certificación aprobada con esa plataforma, modelo y extras (FR-048)."""
    for e in cadena:
        cert = await pool.fetchrow(
            """SELECT extras FROM certificacion
               WHERE organization_id = $1 AND plataforma_id = $2 AND modelo = $3 AND aprobada""",
            org, e["plataforma_id"], e["modelo"],
        )
        if cert is None or _json(cert["extras"]) != (e.get("extras") or {}):
            return e
    return None


async def _conflicto(pool: Any, org: str, base: int, vigente: int, secciones: dict[str, Any]) -> None:
    """FR-072: si alguna de estas secciones cambió entre `base` y la vigente, 409 con quién y cuándo."""
    filas = await pool.fetch(
        """SELECT numero, contenido, autor_correo, creada_en FROM agente_version
           WHERE organization_id = $1 AND numero BETWEEN $2 AND $3 ORDER BY numero""",
        org, base, vigente,
    )
    if not filas or filas[0]["numero"] != base:
        raise ErrorPanel(409, "conflicto", "La versión desde la que editaste ya no existe. Recarga el panel.",
                         seccion=None, autor=None, fecha=None)
    previo = _json(filas[0]["contenido"])
    for f in filas[1:]:
        actual = _json(f["contenido"])
        for s in secciones:
            if actual.get(s) != previo.get(s):
                raise ErrorPanel(
                    409, "conflicto",
                    f"«{s}» cambió mientras editabas: lo publicó {f['autor_correo']}. "
                    "Recarga el panel para ver su cambio antes de guardar el tuyo.",
                    seccion=s, autor=f["autor_correo"], fecha=_iso(f["creada_en"]),
                )
        previo = actual


async def _guardar(
    ctx: Any,
    quien: Quien,
    secciones: dict[str, Any],
    auto: dict[str, Any],
    *,
    base: int | None = None,
    resumen: str | None = None,
    restaurada_de: int | None = None,
    avisos: list[str] | None = None,
    confirmar: list[str] | None = None,
) -> dict[str, Any]:
    """Publica la vigente con estas secciones encima. Con el candado tomado.

    `confirmar`: las protecciones del comportamiento base que quien publica
    aceptó modificar o quitar (FR-124). `None` = no se pregunta: restaurar una
    versión vuelve a un estado que ya se publicó así.
    """
    pool, org = ctx.store.pool, quien.organizacion
    avisos = list(avisos or [])
    vig, _ = await _migrar_si_falta(pool, org, auto)
    if base is not None and base != vig["numero"]:
        await _conflicto(pool, org, base, vig["numero"], secciones)
    actual = _json(vig["contenido"])
    cambiadas = [s for s in SECCIONES if s in secciones and secciones[s] != actual.get(s)]
    if not cambiadas:
        return {"numero": vig["numero"], "avisos": avisos}
    if confirmar is not None and "comportamiento" in cambiadas:
        faltan = [
            p for p in comportamiento.protecciones_por_confirmar(
                secciones["comportamiento"], actual.get("comportamiento")
            )
            if p["clave"] not in confirmar
        ]
        if faltan:
            raise ErrorPanel(
                409, "confirmar_proteccion",
                "Este cambio modifica o quita protecciones del agente: "
                + ", ".join(f"«{p['titulo']}» ({p['cambio']})" for p in faltan)
                + ". Confírmalo para publicar.",
                protecciones=faltan,
            )
    # Una sección opcional publicada con `None` sale de la versión nueva.
    nuevo = {k: v for k, v in {**actual, **secciones}.items() if not (k in OPCIONALES and v is None)}
    eslabones = nuevo.get("cadena") or []
    if "cadena" in cambiadas:
        falta = await _sin_certificar(pool, org, eslabones)
        if falta is not None:
            raise ErrorPanel(
                409, "sin_certificar",
                f"El eslabón {falta['modelo']} no está certificado con esos extras: certifícalo antes de publicar.",
                eslabon=falta,
            )
    if eslabones and len({e["plataforma_id"] for e in eslabones}) == 1:
        avisos.append(
            "Todos los eslabones están en una sola plataforma: si esa plataforma falla, "
            "el agente pasa cada conversación a una persona."
        )
    numero = await _publicar(
        ctx, org, nuevo, resumen or ", ".join(cambiadas), (quien.id, quien.correo), auto, restaurada_de
    )
    return {"numero": numero, "avisos": avisos}


def _desde_wline(auto: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """La versión 1 (R17): lo que hoy tiene Wline. Los criterios por línea pasan a ser
    las características de cada tipo (FR-012); las entradas vacías se migran tal cual."""
    agente = auto.get("agent") or {}
    reglas = auto.get("rules") or {}
    criterios = reglas.get("lineCriteria") or {}
    conocimiento: list[dict[str, str]] = []
    avisos: list[str] = []
    for i, k in enumerate(agente.get("knowledge") or [], 1):
        if not isinstance(k, dict):
            continue
        if k.get("kind") == "block":
            entrada = {"tipo": "bloque", "contenido": str(k.get("content") or "")}
        else:
            entrada = {"tipo": "qa", "pregunta": str(k.get("question") or ""), "respuesta": str(k.get("answer") or "")}
        conocimiento.append(entrada)
        if not all(v.strip() for c, v in entrada.items() if c != "tipo"):
            avisos.append(f"La entrada {i} del conocimiento llegó vacía de Wline: complétala o bórrala.")
    contenido = {
        "identidad": {
            "nombre": str(agente.get("name") or ""),
            "tono": str(agente.get("tone") or ""),
            "saludo": str(agente.get("greeting") or ""),
        },
        "instrucciones": str(agente.get("instructions") or ""),
        "escalado": str(agente.get("escalationRules") or ""),
        "conocimiento": conocimiento,
        "tipos": {
            str(linea["key"]): {"criterio": str(criterios.get(linea["key"]) or ""), "atencion": ""}
            for linea in auto.get("lines") or []
        },
        "ficha": reglas.get("fichaFields") or [],
        "umbral": reglas.get("threshold") or "Clasificado",
        "capacidades": reglas.get("capabilities") or {},
        "cadena": [],
    }
    return contenido, avisos


async def _migrar_si_falta(pool: Any, org: str, auto: dict[str, Any]) -> tuple[Any, list[str]]:
    """La vigente; sin ninguna versión, crea la 1 «migración» con lo que tiene
    Wline (R17), sin publicar: las reglas ya están allá. Re-ejecutable."""
    vig = await _vigente(pool, org)
    if vig is not None:
        return vig, []
    contenido, avisos = _desde_wline(auto)
    await pool.execute(
        """INSERT INTO agente_version (organization_id, numero, contenido, resumen, autor_id, autor_correo)
           VALUES ($1, 1, $2, 'migración', $3, $4) ON CONFLICT DO NOTHING""",
        org, json.dumps(contenido), *MIGRACION,
    )
    await pool.execute(
        "INSERT INTO agente_vigente (organization_id, numero) VALUES ($1, 1) ON CONFLICT DO NOTHING", org
    )
    return await _vigente(pool, org), avisos


async def _plataformas(pool: Any, org: str) -> list[dict[str, Any]]:
    filas = await pool.fetch(
        """SELECT id, nombre, base_url, llave_ultimos4, ultimo_exito_en, ultimo_error, ultimo_error_en
           FROM plataforma_modelo WHERE organization_id = $1 ORDER BY creada_en, nombre""",
        org,
    )
    return [
        {
            "id": f["id"], "nombre": f["nombre"], "baseUrl": f["base_url"],
            "llave": "…" + f["llave_ultimos4"],  # lo único que sale de la llave (FR-041)
            "ultimoExito": _iso(f["ultimo_exito_en"]), "ultimoError": f["ultimo_error"],
            "ultimoErrorEn": _iso(f["ultimo_error_en"]),
        }
        for f in filas
    ]


async def _certificaciones(pool: Any, org: str) -> list[dict[str, Any]]:
    filas = await pool.fetch(
        """SELECT plataforma_id, modelo, extras, aprobada, requiere_atencion, modelo_respondio, certificada_en
           FROM certificacion WHERE organization_id = $1""",
        org,
    )
    return [
        {
            "plataformaId": f["plataforma_id"], "modelo": f["modelo"], "extras": _json(f["extras"]),
            "aprobada": f["aprobada"], "requiereAtencion": f["requiere_atencion"],
            "modeloRespondio": f["modelo_respondio"], "fecha": _iso(f["certificada_en"]),
        }
        for f in filas
    ]


@router.get("/api/estado")
async def estado(request: Request, quien: Quien = Depends(firma)) -> dict[str, Any]:
    """Al abrir (FR-006): migra si hace falta (R17), reconcilia los tipos
    eliminados (FR-025), realinea con Wline (R5) y avisa del umbral."""
    ctx = request.app.state.ctx
    pool, org = ctx.store.pool, quien.organizacion
    async with _candado(request):
        auto = await _automation(ctx.crm)
        vig, avisos = await _migrar_si_falta(pool, org, auto)
        contenido = _json(vig["contenido"])

        claves = {str(linea.get("key")) for linea in auto.get("lines") or []}
        sobran = sorted(set(contenido.get("tipos") or {}) - claves)
        if sobran:
            contenido = {**contenido, "tipos": {k: t for k, t in contenido["tipos"].items() if k in claves}}
            resumen = "tipos eliminados en Wline: " + ", ".join(sobran)
            if await _publicar_sin_bloquear(ctx, org, contenido, resumen, auto, avisos):
                avisos.append(f"Se quitaron las características de {', '.join(sobran)}: ya no existen en Wline.")
        elif int(auto.get("appliedVersion") or 0) > vig["numero"]:
            # Wline aplicó una publicación que Nea descartó, o la base de Nea
            # se restauró de un respaldo: Wline vuelve a las reglas de la vigente.
            if await _publicar_sin_bloquear(ctx, org, contenido, "reglas realineadas con Wline", auto, avisos):
                avisos.append("Reglas realineadas con Wline: tenía aplicada una versión que este panel no conservó.")
        vig = await _vigente(pool, org)
        contenido = _json(vig["contenido"])

        umbral = contenido.get("umbral")
        if umbral and umbral not in (auto.get("thresholdOptions") or []):
            avisos.append(
                f"El umbral «{umbral}» ya no existe en todas las líneas; el agente aplica Clasificado. "
                "Elige otro en Reglas del CRM."
            )
        certificando = await _certificacion_pendiente(ctx, org)
        return {
            "certificando": certificando,
            "vigente": {
                "numero": vig["numero"], "contenido": contenido,
                "autor": vig["autor_correo"], "fecha": _iso(vig["creada_en"]),
            },
            "lineas": auto.get("lines") or [],
            "etapasUmbral": auto.get("thresholdOptions") or [],
            "catalogoCapacidades": auto.get("capabilityCatalog") or {},
            "plataformas": await _plataformas(pool, org),
            "certificaciones": await _certificaciones(pool, org),
            "avisos": avisos,
            # 026: el comportamiento base por partes (la versión o el original)
            # y los avisos que Wline aplica hoy; `None` si aún no los conoce.
            "comportamiento": comportamiento.estado(contenido.get("comportamiento")),
            "avisoPase": (auto.get("rules") or {}).get("handoffNotice"),
            "avisoHostil": (auto.get("rules") or {}).get("hostileNotice"),
        }


@router.put("/api/configuracion")
async def configuracion(request: Request, quien: Quien = Depends(firma)) -> dict[str, Any]:
    """Guardar y publicar `{base, secciones}` (contracts/panel.md §4)."""
    ctx = request.app.state.ctx
    datos = await cuerpo_firmado(request)
    if not (
        isinstance(datos, dict) and isinstance(datos.get("base"), int)
        and isinstance(datos.get("secciones"), dict) and datos["secciones"]
    ):
        raise _invalido("Se espera {base, secciones} con al menos una sección")
    secciones: dict[str, Any] = dict(datos["secciones"])
    async with _candado(request):
        plataformas = await _ids_de_plataformas(ctx.store.pool, quien.organizacion)
        for seccion, valor in secciones.items():
            _validar(seccion, valor, plataformas)
        if "cadena" in secciones:
            secciones["cadena"] = [
                {"plataforma_id": e["plataforma_id"], "modelo": e["modelo"].strip(), "extras": e.get("extras") or {}}
                for e in secciones["cadena"]
            ]
        auto = await _automation(ctx.crm)
        confirmar = datos.get("confirmar")
        return await _guardar(
            ctx, quien, secciones, auto, base=datos["base"],
            confirmar=[str(c) for c in confirmar] if isinstance(confirmar, list) else [],
        )


# ── Historial, restaurar y conocimiento (FR-070, FR-071, R11) ────────────


@router.get("/api/versiones")
async def versiones(request: Request, quien: Quien = Depends(firma)) -> list[dict[str, Any]]:
    filas = await request.app.state.ctx.store.pool.fetch(
        """SELECT numero, autor_correo, creada_en, resumen, restaurada_de FROM agente_version
           WHERE organization_id = $1 ORDER BY numero DESC""",
        quien.organizacion,
    )
    return [
        {"numero": f["numero"], "autor": f["autor_correo"], "fecha": _iso(f["creada_en"]),
         "resumen": f["resumen"], "restauradaDe": f["restaurada_de"]}
        for f in filas
    ]


@router.post("/api/versiones/{numero}/restaurar")
async def restaurar(numero: int, request: Request, quien: Quien = Depends(firma)) -> dict[str, Any]:
    """Una versión nueva con el contenido de `numero`, sin los tipos que Wline
    ya no tiene ni los eslabones de plataformas borradas (R5)."""
    ctx = request.app.state.ctx
    pool, org = ctx.store.pool, quien.organizacion
    await cuerpo_firmado(request)
    async with _candado(request):
        fila = await pool.fetchrow(
            "SELECT contenido FROM agente_version WHERE organization_id = $1 AND numero = $2", org, numero
        )
        if fila is None:
            raise ErrorPanel(404, "no_encontrada", f"No existe la versión {numero}")
        auto = await _automation(ctx.crm)
        contenido = _json(fila["contenido"])
        avisos: list[str] = []
        claves = {str(linea.get("key")) for linea in auto.get("lines") or []}
        tipos = contenido.get("tipos") or {}
        if sobran := sorted(set(tipos) - claves):
            avisos.append(f"Se omitieron {', '.join(sobran)}: esos tipos ya no existen en Wline.")
        existentes = await _ids_de_plataformas(pool, org)
        eslabones = contenido.get("cadena") or []
        for e in eslabones:
            if e.get("plataforma_id") not in existentes:
                avisos.append(f"Se omitió el eslabón {e.get('modelo')}: su plataforma ya no existe.")
        contenido = {
            # 026: una sección opcional que esta versión no traía sale también
            # de la nueva; sin esto se conservaría la de la vigente.
            **{s: None for s in OPCIONALES},
            **contenido,
            "tipos": {k: t for k, t in tipos.items() if k in claves},
            "cadena": [e for e in eslabones if e.get("plataforma_id") in existentes],
        }
        return await _guardar(
            ctx, quien, contenido, auto, resumen=f"restaurada de {numero}", restaurada_de=numero, avisos=avisos
        )


@router.post("/api/conocimiento")
async def conocimiento(request: Request, quien: Quien = Depends(firma)) -> dict[str, Any]:
    """Una entrada P/R más, publicada como cualquier cambio («aplicar sugerencia», R11)."""
    ctx = request.app.state.ctx
    datos = await cuerpo_firmado(request)
    if not isinstance(datos, dict):
        raise _invalido("Se espera {pregunta, respuesta}")
    pregunta, respuesta = datos.get("pregunta"), datos.get("respuesta")
    for valor, donde in ((pregunta, "pregunta"), (respuesta, "respuesta")):
        _texto(valor, 4000, donde)
        if not valor.strip():
            raise _invalido(f"Falta la {donde}")
    async with _candado(request):
        auto = await _automation(ctx.crm)
        vig, _ = await _migrar_si_falta(ctx.store.pool, quien.organizacion, auto)
        actual = _json(vig["contenido"]).get("conocimiento") or []
        nuevo = [*actual, {"tipo": "qa", "pregunta": pregunta.strip(), "respuesta": respuesta.strip()}]
        _validar("conocimiento", nuevo, set())
        return await _guardar(ctx, quien, {"conocimiento": nuevo}, auto, resumen="conocimiento")


# ── Certificación de eslabones (T064, R8; plan-bloque5 D1–D3) ────────────
#
# «Certificar» deja una cadena de prueba de un eslabón y le pide a Wline una
# corrida del Laboratorio. Mientras exista, TODO turno de prueba (isTest)
# piensa con ella. Al terminar la corrida: aprobada si no hubo ningún `rojo`
# ni `judge_failed` (A1); se guarda con el modelo que respondió (F15).

# El tope del Laboratorio (20 min, Wline T108) más uno: la corrida termina, o
# el runner la falla, antes de que la cadena de prueba venza.
VIGENCIA_PRUEBA = timedelta(minutes=21)


def _mensaje_de_wline(exc: CrmConflict, por_defecto: str) -> str:
    error = exc.payload.get("error")
    return str((error.get("message") if isinstance(error, dict) else None) or por_defecto)


async def _cerrar_certificacion(pool: Any, org: str, prueba: Any, corrida: dict[str, Any]) -> bool:
    """Guarda el resultado de una corrida terminada y borra la cadena de prueba.

    Una corrida `failed` (tope o error del runner) no evaluó al modelo: no se
    guarda nada y la certificación anterior, si había, queda como estaba.
    """
    eslabon = _json(prueba["eslabon"])
    casos = corrida.get("cases") or []
    aprobada = corrida.get("status") == "done" and bool(casos) and not any(
        c.get("veredicto") == "rojo" or c.get("status") == "judge_failed" for c in casos
    )
    if corrida.get("status") == "done":
        try:
            await pool.execute(
                """INSERT INTO certificacion (organization_id, plataforma_id, modelo, extras, lab_run_id,
                                              puntaje, aprobada, modelo_respondio, requiere_atencion, certificada_en)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, false, now())
                   ON CONFLICT (organization_id, plataforma_id, modelo) DO UPDATE SET
                     extras = EXCLUDED.extras, lab_run_id = EXCLUDED.lab_run_id, puntaje = EXCLUDED.puntaje,
                     aprobada = EXCLUDED.aprobada, modelo_respondio = EXCLUDED.modelo_respondio,
                     requiere_atencion = false, certificada_en = now()""",
                org, eslabon["plataforma_id"], eslabon["modelo"], json.dumps(eslabon.get("extras") or {}),
                prueba["lab_run_id"], corrida.get("score"), aprobada, prueba["modelo_respondio"],
            )
        except asyncpg.ForeignKeyViolationError:
            aprobada = False  # la plataforma se borró mientras tanto
    await pool.execute(
        "DELETE FROM cadena_prueba WHERE organization_id = $1 AND lab_run_id = $2", org, prueba["lab_run_id"]
    )
    logger.info("panel: certificación %s de %s/%s: %s", prueba["lab_run_id"], eslabon["plataforma_id"],
                eslabon["modelo"], "aprobada" if aprobada else "no aprobada")
    return aprobada


async def _certificacion_pendiente(ctx: Any, org: str) -> dict[str, Any] | None:
    """Al abrir: cierra la certificación cuya corrida ya terminó (p. ej. si se
    cerró la página); si sigue en curso, la devuelve para mostrar «certificando…»."""
    pool = ctx.store.pool
    prueba = await pool.fetchrow("SELECT * FROM cadena_prueba WHERE organization_id = $1", org)
    if prueba is None or not prueba["lab_run_id"]:
        return None
    try:
        corrida = await ctx.crm.lab_run_get(prueba["lab_run_id"])
    except CrmError:
        corrida = {}  # no impide abrir el panel; se cierra en la siguiente
    if corrida.get("status") in ("done", "failed"):
        await _cerrar_certificacion(pool, org, prueba, corrida)
        return None
    eslabon = _json(prueba["eslabon"])
    return {
        "plataformaId": eslabon["plataforma_id"], "modelo": eslabon["modelo"],
        "extras": eslabon.get("extras") or {}, "runId": prueba["lab_run_id"],
    }


@router.post("/api/certificaciones", status_code=202)
async def certificar(request: Request, quien: Quien = Depends(firma)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    pool, org = ctx.store.pool, quien.organizacion
    datos = await cuerpo_firmado(request)
    if not isinstance(datos, dict):
        raise _invalido("Se espera {plataforma_id, modelo, extras}")
    async with _candado(request):
        _validar("cadena", [datos], await _ids_de_plataformas(pool, org))
        eslabon = {
            "plataforma_id": datos["plataforma_id"], "modelo": datos["modelo"].strip(),
            "extras": datos.get("extras") or {},
        }
        if await pool.fetchval(
            "SELECT 1 FROM cadena_prueba WHERE organization_id = $1 AND vence_en > now()", org
        ):
            raise ErrorPanel(409, "laboratorio_ocupado", "Ya hay una certificación en curso: espera a que termine.")
        # Primero la cadena de prueba: los turnos de la corrida la usan desde el primero.
        await pool.execute(
            """INSERT INTO cadena_prueba (organization_id, eslabon, vence_en) VALUES ($1, $2, now() + $3)
               ON CONFLICT (organization_id) DO UPDATE SET eslabon = EXCLUDED.eslabon,
                 vence_en = EXCLUDED.vence_en, lab_run_id = NULL, modelo_respondio = NULL""",
            org, json.dumps(eslabon), VIGENCIA_PRUEBA,
        )
        try:
            run_id = await ctx.crm.lab_run_start()
        except CrmError as exc:
            await pool.execute("DELETE FROM cadena_prueba WHERE organization_id = $1", org)
            if isinstance(exc, CrmConflict):
                if exc.code == "run_in_progress":
                    raise ErrorPanel(
                        409, "laboratorio_ocupado", "El Laboratorio ya tiene una corrida en curso: espera a que termine."
                    ) from None
                raise ErrorPanel(409, exc.code, _mensaje_de_wline(exc, "Wline no arrancó la corrida")) from None
            raise ErrorPanel(502, "wline_inalcanzable", "No pude pedirle la corrida a Wline; no quedó nada pendiente.") from None
        await pool.execute("UPDATE cadena_prueba SET lab_run_id = $2 WHERE organization_id = $1", org, run_id)
    logger.info("panel: certificando %s/%s en la corrida %s", eslabon["plataforma_id"], eslabon["modelo"], run_id)
    return {"runId": run_id}


@router.get("/api/certificaciones/{run_id}")
async def avance(run_id: str, request: Request, quien: Quien = Depends(firma)) -> dict[str, Any]:
    """El avance (lo consulta la página solo mientras está abierta); al terminar, el resultado."""
    ctx = request.app.state.ctx
    pool, org = ctx.store.pool, quien.organizacion
    async with _candado(request):
        prueba = await pool.fetchrow(
            "SELECT * FROM cadena_prueba WHERE organization_id = $1 AND lab_run_id = $2", org, run_id
        )
        cert = await pool.fetchrow(
            "SELECT aprobada FROM certificacion WHERE organization_id = $1 AND lab_run_id = $2", org, run_id
        )
        if prueba is None and cert is None:
            raise ErrorPanel(404, "no_encontrada", "Esa corrida no es una certificación de este panel")
        try:
            corrida = await ctx.crm.lab_run_get(run_id)
        except CrmError:
            raise ErrorPanel(502, "wline_inalcanzable", "No pude consultar la corrida en Wline.") from None
        respuesta: dict[str, Any] = {
            "status": corrida.get("status"), "progress": corrida.get("progress"), "score": corrida.get("score"),
        }
        if respuesta["status"] in ("done", "failed"):
            respuesta["aprobada"] = (
                await _cerrar_certificacion(pool, org, prueba, corrida) if prueba is not None
                else bool(cert and cert["aprobada"])
            )
        return respuesta


# ── El juez del Laboratorio (T066, R11) ──────────────────────────────────


@router.get("/api/juez")
async def juez(request: Request, quien: Quien = Depends(firma)) -> dict[str, str]:
    """Lo que el juez del Laboratorio compara (solo `r: sistema`): el mismo
    texto que Wline armaba con `agent_profile` y `kb_entry`, desde la vigente.
    Sin vigente, lo que migraría de Wline, sin guardarlo: abrir el panel sigue
    siendo lo único que migra."""
    ctx = request.app.state.ctx
    vig = await _vigente(ctx.store.pool, quien.organizacion)
    if vig is not None:
        contenido = _json(vig["contenido"])
    else:
        contenido, _ = _desde_wline(await _automation(ctx.crm))
    identidad = contenido.get("identidad") or {}
    lineas = [
        f"Nombre: {identidad.get('nombre') or ctx.settings.agent_name}",
        f"Tono: {identidad['tono']}" if identidad.get("tono") else None,
        f"Instrucciones: {contenido['instrucciones']}" if contenido.get("instrucciones") else None,
        f"Escalado: {contenido['escalado']}" if contenido.get("escalado") else None,
    ]
    return {
        "comportamiento": "\n".join(linea for linea in lineas if linea),
        "conocimiento": _kb(contenido.get("conocimiento")) or "(knowledge base vacío)",
    }


# ── Plataformas del modelo (FR-040, FR-041, FR-043, FR-045) ──────────────
#
# La llave se cifra con NEA_ENCRYPTION_KEY y solo sale `…abcd`: ni en una
# respuesta, ni en un log, ni hacia Wline (SC-014).

_HOSTS_DE_DESARROLLO = {"localhost", "127.0.0.1", "host.docker.internal"}


def _nombre(valor: Any) -> str:
    _texto(valor, 80, "nombre")
    if not valor.strip():
        raise _invalido("La plataforma necesita un nombre")
    return str(valor).strip()


def _base_url(valor: Any) -> str:
    _texto(valor, 500, "dirección")
    url = urlparse(valor.strip())
    host = url.hostname or ""
    desarrollo = host in _HOSTS_DE_DESARROLLO or (host and "." not in host)
    if not ((url.scheme == "https" and host) or (url.scheme == "http" and desarrollo)):
        raise _invalido("La dirección debe ser https://… (http:// solo hacia un host de desarrollo)")
    return str(valor).strip().rstrip("/")


def _cifrar(llave: Any, clave: str) -> tuple[bytes, str]:
    _texto(llave, 500, "llave")
    if not llave.strip():
        raise _invalido("Falta la llave de la plataforma")
    try:
        return cifrar(llave.strip(), clave)
    except SinLlaveDeCifrado as exc:
        raise ErrorPanel(503, "sin_llave_de_cifrado", str(exc)) from None


async def _plataforma(pool: Any, org: str, pid: str) -> Any:
    fila = await pool.fetchrow(
        "SELECT * FROM plataforma_modelo WHERE organization_id = $1 AND id = $2", org, pid
    )
    if fila is None:
        raise ErrorPanel(404, "no_encontrada", "No existe esa plataforma")
    return fila


@router.get("/api/plataformas")
async def plataformas(request: Request, quien: Quien = Depends(firma)) -> list[dict[str, Any]]:
    return await _plataformas(request.app.state.ctx.store.pool, quien.organizacion)


@router.post("/api/plataformas", status_code=201)
async def alta(request: Request, quien: Quien = Depends(firma)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    datos = await cuerpo_firmado(request)
    if not isinstance(datos, dict):
        raise _invalido("Se espera {nombre, baseUrl, llave}")
    nombre, base = _nombre(datos.get("nombre")), _base_url(datos.get("baseUrl"))
    blob, ultimos4 = _cifrar(datos.get("llave"), ctx.settings.nea_encryption_key)
    pid = "pl_" + secrets.token_hex(8)
    try:
        await ctx.store.pool.execute(
            """INSERT INTO plataforma_modelo (organization_id, id, nombre, base_url, llave_cifrada, llave_ultimos4)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            quien.organizacion, pid, nombre, base, blob, ultimos4,
        )
    except asyncpg.UniqueViolationError:
        raise ErrorPanel(409, "nombre_repetido", f"Ya hay una plataforma llamada «{nombre}»") from None
    return {"id": pid}


@router.put("/api/plataformas/{pid}")
async def editar(pid: str, request: Request, quien: Quien = Depends(firma)) -> dict[str, Any]:
    ctx = request.app.state.ctx
    pool, org = ctx.store.pool, quien.organizacion
    datos = await cuerpo_firmado(request)
    if not isinstance(datos, dict):
        raise _invalido("Se espera {nombre?, baseUrl?, llave?}")
    fila = await _plataforma(pool, org, pid)
    nombre = _nombre(datos["nombre"]) if "nombre" in datos else fila["nombre"]
    base = _base_url(datos["baseUrl"]) if "baseUrl" in datos else fila["base_url"]
    blob, ultimos4 = fila["llave_cifrada"], fila["llave_ultimos4"]
    if datos.get("llave"):  # sin llave, se conserva la guardada
        blob, ultimos4 = _cifrar(datos["llave"], ctx.settings.nea_encryption_key)
    try:
        await pool.execute(
            """UPDATE plataforma_modelo SET nombre = $3, base_url = $4, llave_cifrada = $5, llave_ultimos4 = $6
               WHERE organization_id = $1 AND id = $2""",
            org, pid, nombre, base, blob, ultimos4,
        )
    except asyncpg.UniqueViolationError:
        raise ErrorPanel(409, "nombre_repetido", f"Ya hay una plataforma llamada «{nombre}»") from None
    return {"id": pid}


@router.delete("/api/plataformas/{pid}", status_code=204)
async def borrar(pid: str, request: Request, quien: Quien = Depends(firma)) -> None:
    pool, org = request.app.state.ctx.store.pool, quien.organizacion
    async with _candado(request):  # que ninguna publicación la meta en la cadena mientras tanto
        vig = await _vigente(pool, org)
        eslabones = (_json(vig["contenido"]).get("cadena") or []) if vig else []
        if any(e.get("plataforma_id") == pid for e in eslabones):
            raise ErrorPanel(409, "en_uso", "La cadena vigente usa esta plataforma: quítala de la cadena y publica primero.")
        # Sus certificaciones caen con ella (ON DELETE CASCADE).
        if await pool.execute("DELETE FROM plataforma_modelo WHERE organization_id = $1 AND id = $2", org, pid) == "DELETE 0":
            raise ErrorPanel(404, "no_encontrada", "No existe esa plataforma")


@router.post("/api/plataformas/{pid}/probar")
async def probar(pid: str, request: Request, quien: Quien = Depends(firma)) -> dict[str, Any]:
    """«Probar conexión» desde donde el agente la usará (R9, FR-043)."""
    ctx = request.app.state.ctx
    pool, org = ctx.store.pool, quien.organizacion
    datos = await cuerpo_firmado(request)
    if not isinstance(datos, dict):
        raise _invalido("Se espera {modelo, extras}")
    modelo, extras = datos.get("modelo"), datos.get("extras") or {}
    _texto(modelo, 200, "modelo")
    if not modelo.strip() or not isinstance(extras, dict) or len(json.dumps(extras)) > 2048:
        raise _invalido("Indica el modelo, y extras de hasta 2 KB")
    fila = await _plataforma(pool, org, pid)
    try:
        llave = descifrar(bytes(fila["llave_cifrada"]), ctx.settings.nea_encryption_key)
    except SinLlaveDeCifrado as exc:
        raise ErrorPanel(503, "sin_llave_de_cifrado", str(exc)) from None
    except (InvalidTag, UnicodeDecodeError):
        raise ErrorPanel(409, "llave_ilegible", "No pude descifrar la llave guardada: vuelve a escribirla.") from None
    inicio = time.monotonic()
    resultado = await cadena.probar_conexion(
        Plataforma(pid, fila["nombre"], fila["base_url"], llave), modelo.strip(), extras,
        pool=pool, organization_id=org,
    )
    if resultado.get("ok"):
        resultado = {**resultado, "ms": round((time.monotonic() - inicio) * 1000)}
    return resultado


def montar(app: FastAPI) -> None:
    app.state.candado_del_panel = asyncio.Lock()
    app.include_router(router)
    app.add_exception_handler(ErrorPanel, _manejar_error)
