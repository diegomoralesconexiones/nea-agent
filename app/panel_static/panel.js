// Panel del agente (Wline 025). Sin framework: todo el texto entra como nodo
// de texto (nunca innerHTML), y las rutas son absolutas porque la misma
// página se sirve en /panel y en /panel/.
"use strict";

(() => {
  const API = "/panel/api";
  const $ = (selector) => document.querySelector(selector);

  let estado = null; // la última respuesta de GET /estado
  const bases = {}; // la versión desde la que se editó cada sección (FR-072)

  // ── Utilidades ──────────────────────────────────────────────────────────

  const PROPIEDADES = new Set(["value", "checked", "disabled", "selected", "hidden"]);

  function el(tag, props, ...hijos) {
    const nodo = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") nodo.className = v;
      else if (k.startsWith("on")) nodo.addEventListener(k.slice(2), v);
      else if (PROPIEDADES.has(k)) nodo[k] = v;
      else nodo.setAttribute(k, v === true ? "" : String(v));
    }
    for (const h of hijos.flat()) {
      if (h === null || h === undefined || h === false) continue;
      nodo.append(h instanceof Node ? h : document.createTextNode(String(h)));
    }
    return nodo;
  }

  // Una etiqueta con su control. En las secciones de contenido, los vacíos
  // se señalan (las entradas que la migración trajo vacías, por ejemplo).
  function campo(etiqueta, control, ayuda, senalarVacio) {
    const nodo = el("label", { class: "campo" }, el("span", {}, etiqueta), control, ayuda ? el("small", {}, ayuda) : null);
    if (senalarVacio) {
      const marcar = () => nodo.classList.toggle("vacio", control.value.trim() === "");
      control.addEventListener("input", marcar);
      marcar();
    }
    return nodo;
  }

  const texto = (valor, props) => el("input", { type: "text", value: valor || "", ...props });
  const area = (valor, filas, props) => el("textarea", { rows: filas || 3, value: valor || "", ...props });

  function decir(nodo, mensaje, tipo) {
    nodo.textContent = mensaje || "";
    nodo.className = "mensaje" + (tipo ? " " + tipo : "");
  }

  function fecha(iso) {
    return iso ? new Date(iso).toLocaleString("es-MX", { dateStyle: "medium", timeStyle: "short" }) : "—";
  }

  // JSON con las claves ordenadas: para comparar extras como lo hace Nea.
  function canonico(v) {
    if (Array.isArray(v)) return "[" + v.map(canonico).join(",") + "]";
    if (v && typeof v === "object") {
      return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonico(v[k])).join(",") + "}";
    }
    return JSON.stringify(v);
  }

  function objetoJson(valor) {
    try {
      const v = JSON.parse(valor.trim() || "{}");
      return v && typeof v === "object" && !Array.isArray(v) ? v : null;
    } catch {
      return null;
    }
  }

  async function api(metodo, ruta, cuerpo) {
    const init = { method: metodo, headers: {} };
    if (cuerpo !== undefined) {
      init.headers["content-type"] = "application/json";
      init.body = JSON.stringify(cuerpo);
    }
    const res = await fetch(API + ruta, init);
    let datos = null;
    if (res.status !== 204) {
      try {
        datos = await res.json();
      } catch {
        datos = null;
      }
    }
    if (!res.ok) {
      const error = new Error((datos && datos.error && datos.error.message) || "El panel respondió " + res.status);
      error.status = res.status;
      error.datos = (datos && datos.error) || {};
      throw error;
    }
    return datos;
  }

  function explicar(error) {
    if (error.status === 401) return "Tu sesión de Wline terminó: vuelve a entrar.";
    if (error.datos && error.datos.code === "conflicto" && error.datos.fecha) {
      return error.message + " (" + fecha(error.datos.fecha) + ")";
    }
    return error.message || "Algo falló. Intenta de nuevo.";
  }

  async function recargar() {
    estado = await api("GET", "/estado");
  }

  function pintarAvisos(lista) {
    $("#avisos").replaceChildren(...(lista || []).map((a) => el("p", { class: "aviso" }, a)));
  }

  function agregarAvisos(lista) {
    for (const a of lista || []) $("#avisos").append(el("p", { class: "aviso" }, a));
  }

  function pintarCabecera() {
    const v = estado.vigente;
    $("#vigente").textContent = "Versión " + v.numero + " · publicada por " + v.autor + " el " + fecha(v.fecha);
  }

  // ── Secciones que se publican (contracts/panel.md §4) ───────────────────

  function pintarIdentidad(cuerpo, c) {
    const id = c.identidad || {};
    const nombre = texto(id.nombre, { maxlength: 4000 });
    const tono = texto(id.tono, { maxlength: 4000 });
    const saludo = area(id.saludo, 3, { maxlength: 4000 });
    cuerpo.replaceChildren(
      campo("Nombre", nombre, null, true),
      campo("Tono", tono, "Por ejemplo: cálido y directo.", true),
      campo("Saludo", saludo, "El primer mensaje a un cliente nuevo.", true)
    );
    return () => ({ identidad: { nombre: nombre.value, tono: tono.value, saludo: saludo.value } });
  }

  function pintarInstrucciones(cuerpo, c) {
    const instrucciones = area(c.instrucciones, 8, { maxlength: 8000 });
    const escalado = area(c.escalado, 5, { maxlength: 8000 });
    cuerpo.replaceChildren(
      campo("Instrucciones", instrucciones, null, true),
      campo("Reglas de escalado", escalado, "Cuándo el agente pasa la conversación a una persona.", true)
    );
    return () => ({ instrucciones: instrucciones.value, escalado: escalado.value });
  }

  function pintarConocimiento(cuerpo, c) {
    const lista = el("ol", { class: "entradas" });
    const filas = [];
    function agregar(entrada) {
      const fila = { tipo: entrada.tipo === "bloque" ? "bloque" : "qa" };
      const quitar = el("button", { type: "button", class: "quitar" }, "Quitar");
      if (fila.tipo === "qa") {
        fila.pregunta = texto(entrada.pregunta, { maxlength: 4000 });
        fila.respuesta = area(entrada.respuesta, 2, { maxlength: 4000 });
        fila.nodo = el("li", { class: "entrada" }, campo("Pregunta", fila.pregunta, null, true), campo("Respuesta", fila.respuesta, null, true), quitar);
      } else {
        fila.contenido = area(entrada.contenido, 3, { maxlength: 4000 });
        fila.nodo = el("li", { class: "entrada" }, campo("Bloque de texto", fila.contenido, null, true), quitar);
      }
      quitar.addEventListener("click", () => {
        filas.splice(filas.indexOf(fila), 1);
        fila.nodo.remove();
      });
      filas.push(fila);
      lista.append(fila.nodo);
    }
    (c.conocimiento || []).forEach(agregar);
    cuerpo.replaceChildren(
      lista,
      el(
        "div",
        { class: "acciones" },
        el("button", { type: "button", onclick: () => agregar({ tipo: "qa" }) }, "Agregar pregunta y respuesta"),
        el("button", { type: "button", onclick: () => agregar({ tipo: "bloque" }) }, "Agregar bloque de texto")
      )
    );
    return () => ({
      conocimiento: filas.map((f) =>
        f.tipo === "qa"
          ? { tipo: "qa", pregunta: f.pregunta.value, respuesta: f.respuesta.value }
          : { tipo: "bloque", contenido: f.contenido.value }
      ),
    });
  }

  function pintarTipos(cuerpo, c) {
    const tipos = c.tipos || {};
    const filas = (estado.lineas || []).map((linea) => {
      const t = tipos[linea.key] || {};
      const criterio = area(t.criterio, 2, { maxlength: 4000 });
      const atencion = area(t.atencion, 2, { maxlength: 4000 });
      const pendiente = el("span", { class: "etiqueta pendiente" }, "sin características");
      const marcar = () => {
        pendiente.hidden = criterio.value.trim() !== "";
      };
      criterio.addEventListener("input", marcar);
      marcar();
      return {
        key: linea.key,
        criterio,
        atencion,
        nodo: el(
          "article",
          { class: "tipo" },
          el("h3", {}, linea.name + " ", pendiente),
          el("p", { class: "tenue" }, "En la ficha: " + linea.tipoCliente),
          campo("Cómo reconocerlo", criterio, "Sin esto, el agente no clasifica a nadie en este tipo.", true),
          campo("Cómo atenderlo", atencion, "Indicaciones para conversar con este tipo de cliente.")
        ),
      };
    });
    cuerpo.replaceChildren(...(filas.length ? filas.map((f) => f.nodo) : [el("p", { class: "tenue" }, "Wline no tiene tipos de cliente.")]));
    return () => ({
      tipos: Object.fromEntries(filas.map((f) => [f.key, { criterio: f.criterio.value, atencion: f.atencion.value }])),
    });
  }

  const TIPOS_DE_CAMPO = [
    ["texto", "Texto"],
    ["numero", "Número"],
    ["si_no", "Sí / no"],
    ["lista", "Lista"],
  ];

  function pintarReglas(cuerpo, c) {
    // La ficha: `tipo_cliente` es del sistema (sus opciones son los tipos).
    const ficha = el("div", {});
    const campos = [];
    function agregarCampo(f) {
      const fila = { f };
      if (f.system) {
        fila.nodo = el(
          "div",
          { class: "campo-ficha sistema" },
          el("strong", {}, f.label),
          el("span", { class: "tenue" }, " · del sistema · opciones: " + (f.options || []).join(", "))
        );
      } else {
        fila.key = texto(f.key, { maxlength: 40, placeholder: "clave_en_minusculas" });
        fila.label = texto(f.label, { maxlength: 60 });
        fila.tipo = el("select", {}, ...TIPOS_DE_CAMPO.map(([v, t]) => el("option", { value: v, selected: f.type === v }, t)));
        fila.requerido = el("input", { type: "checkbox", checked: !!f.required });
        fila.opciones = texto((f.options || []).join(", "), { placeholder: "Opción 1, Opción 2" });
        const opciones = campo("Opciones (separadas por coma)", fila.opciones);
        const sincronizar = () => {
          opciones.hidden = fila.tipo.value !== "lista";
        };
        fila.tipo.addEventListener("change", sincronizar);
        sincronizar();
        fila.nodo = el(
          "div",
          { class: "campo-ficha" },
          campo("Clave", fila.key),
          campo("Nombre", fila.label),
          campo("Tipo", fila.tipo),
          el("label", { class: "check" }, fila.requerido, "Obligatorio"),
          opciones,
          el("button", {
            type: "button",
            class: "quitar",
            onclick: () => {
              campos.splice(campos.indexOf(fila), 1);
              fila.nodo.remove();
            },
          }, "Quitar")
        );
      }
      campos.push(fila);
      ficha.append(fila.nodo);
    }
    (c.ficha || []).forEach(agregarCampo);

    // El umbral, solo entre las etapas que existen en todas las líneas.
    const opciones = estado.etapasUmbral || [];
    const vigenteValido = opciones.includes(c.umbral);
    const umbral = el(
      "select",
      {},
      ...opciones.map((o) => el("option", { value: o, selected: o === (vigenteValido ? c.umbral : "Clasificado") }, o))
    );

    const capacidades = Object.entries(estado.catalogoCapacidades || {}).map(([clave, info]) => {
      const casilla = el("input", { type: "checkbox", checked: !!(c.capacidades || {})[clave], disabled: !info.available });
      // La etiqueta del catálogo de Wline, tal cual (ya dice si está por venir).
      return { clave, casilla, nodo: el("label", { class: "check" }, casilla, info.label) };
    });

    cuerpo.replaceChildren(
      el("h3", {}, "Ficha de clasificación"),
      ficha,
      el("button", { type: "button", onclick: () => agregarCampo({ type: "texto" }) }, "Agregar campo"),
      el("h3", {}, "Umbral de intervención humana"),
      ...(vigenteValido
        ? []
        : [el("p", { class: "aviso" }, "El umbral «" + c.umbral + "» ya no existe en todas las líneas; el agente aplica Clasificado. Se propone Clasificado: publica para fijarlo.")]),
      campo("Cuando el lead llega a esta etapa, la IA se pausa y entra una persona", umbral),
      el("h3", {}, "Capacidades"),
      ...capacidades.map((x) => x.nodo)
    );
    return () => ({
      ficha: campos.map((x) =>
        x.f.system
          ? x.f
          : {
              key: x.key.value.trim(),
              label: x.label.value.trim(),
              type: x.tipo.value,
              required: x.requerido.checked,
              ...(x.tipo.value === "lista"
                ? { options: x.opciones.value.split(",").map((s) => s.trim()).filter(Boolean) }
                : {}),
            }
      ),
      umbral: umbral.value,
      capacidades: Object.fromEntries(capacidades.map((x) => [x.clave, x.casilla.checked])),
    });
  }

  function marcaDeCertificacion(plataformaId, modelo, extras) {
    const cert = (estado.certificaciones || []).find((x) => x.plataformaId === plataformaId && x.modelo === modelo);
    if (!cert || !cert.aprobada || !extras || canonico(cert.extras) !== canonico(extras)) {
      return ["sin certificar", "pendiente"];
    }
    if (cert.requiereAtencion) return ["requiere atención: la plataforma respondió con otro modelo", "atencion"];
    return ["certificado", "ok"];
  }

  function pintarCadena(cuerpo, c) {
    const lista = el("ol", { class: "cadena" });
    const filas = [];
    const vacia = el("p", { class: "peligro" }, "Cadena vacía: el agente pasa cada conversación a una persona hasta que publiques al menos un eslabón certificado.");
    const unica = el("p", { class: "aviso" }, "Todos los eslabones están en una sola plataforma: si esa plataforma falla, el agente pasa cada conversación a una persona.");
    const sinPlataformas = el("p", { class: "tenue" }, "Registra una plataforma arriba para armar la cadena.");
    const agregarBoton = el("button", { type: "button" }, "Agregar eslabón");

    function actualizar() {
      vacia.hidden = filas.length > 0;
      unica.hidden = !(filas.length && new Set(filas.map((f) => f.plataforma.value)).size === 1);
      sinPlataformas.hidden = (estado.plataformas || []).length > 0;
      agregarBoton.disabled = filas.length >= 6 || !(estado.plataformas || []).length;
      filas.forEach((f) => lista.append(f.nodo));
    }

    function agregar(e) {
      const fila = {};
      fila.plataforma = el(
        "select",
        {},
        ...(estado.plataformas || []).map((p) => el("option", { value: p.id, selected: p.id === e.plataforma_id }, p.nombre))
      );
      fila.modelo = texto(e.modelo, { maxlength: 200, placeholder: "proveedor/modelo" });
      fila.extras = area(JSON.stringify(e.extras || {}), 2, { class: "mono" });
      const marca = el("span", { class: "etiqueta" });
      const pintarMarca = () => {
        const [t, clase] = marcaDeCertificacion(fila.plataforma.value, fila.modelo.value.trim(), objetoJson(fila.extras.value));
        marca.textContent = t;
        marca.className = "etiqueta " + clase;
      };
      [fila.plataforma, fila.modelo, fila.extras].forEach((x) => x.addEventListener("input", pintarMarca));
      fila.plataforma.addEventListener("change", () => {
        pintarMarca();
        actualizar();
      });
      const mover = (paso) => {
        const i = filas.indexOf(fila);
        const j = i + paso;
        if (j < 0 || j >= filas.length) return;
        [filas[i], filas[j]] = [filas[j], filas[i]];
        actualizar();
      };
      fila.nodo = el(
        "li",
        { class: "eslabon" },
        el("div", { class: "rejilla" }, campo("Plataforma", fila.plataforma), campo("Modelo", fila.modelo)),
        campo("Extras (JSON)", fila.extras, "Los mismos con que se certificó; por ejemplo {\"reasoning_effort\": \"none\"}."),
        marca,
        el(
          "div",
          { class: "acciones" },
          el("button", { type: "button", onclick: () => mover(-1) }, "Subir"),
          el("button", { type: "button", onclick: () => mover(1) }, "Bajar"),
          el("button", {
            type: "button",
            class: "quitar",
            onclick: () => {
              filas.splice(filas.indexOf(fila), 1);
              fila.nodo.remove();
              actualizar();
            },
          }, "Quitar")
        )
      );
      filas.push(fila);
      pintarMarca();
      actualizar();
    }

    agregarBoton.addEventListener("click", () => agregar({}));
    (c.cadena || []).forEach(agregar);
    cuerpo.replaceChildren(
      el("p", { class: "tenue" }, "El primero es el principal; si falla, responde el siguiente. Hasta 6 eslabones, cada uno certificado."),
      vacia,
      unica,
      lista,
      sinPlataformas,
      agregarBoton
    );
    actualizar();
    return () => ({
      cadena: filas.map((f, i) => {
        const extras = objetoJson(f.extras.value);
        if (!extras) throw new Error("Los extras del eslabón " + (i + 1) + " no son un objeto JSON válido.");
        return { plataforma_id: f.plataforma.value, modelo: f.modelo.value.trim(), extras };
      }),
    });
  }

  const SECCIONES = {
    identidad: pintarIdentidad,
    instrucciones: pintarInstrucciones,
    conocimiento: pintarConocimiento,
    tipos: pintarTipos,
    reglas: pintarReglas,
    cadena: pintarCadena,
  };

  function pintarSeccion(id, mensaje) {
    const cuerpo = document.querySelector("#" + id + " .cuerpo");
    bases[id] = estado.vigente.numero;
    const leer = SECCIONES[id](cuerpo, estado.vigente.contenido);
    const nota = el("p", { class: "mensaje", role: "status" });
    const boton = el("button", { type: "button", class: "primario" }, "Guardar y publicar");
    boton.addEventListener("click", () => guardar(id, leer, boton, nota));
    cuerpo.append(el("div", { class: "pie" }, boton, nota));
    if (mensaje) decir(nota, mensaje[0], mensaje[1]);
  }

  async function guardar(id, leer, boton, nota) {
    let secciones;
    try {
      secciones = leer();
    } catch (error) {
      decir(nota, error.message, "error");
      return;
    }
    boton.disabled = true;
    decir(nota, "Publicando…");
    const antes = estado.vigente.numero;
    try {
      const r = await api("PUT", "/configuracion", { base: bases[id], secciones });
      await recargar();
      pintarSeccion(id, r.numero === antes ? ["No había cambios que publicar.", "ok"] : ["Publicado: versión " + r.numero + ".", "ok"]);
      pintarCabecera();
      pintarHistorial();
      agregarAvisos(r.avisos);
    } catch (error) {
      decir(nota, explicar(error), "error");
      boton.disabled = false;
    }
  }

  // ── Plataformas (FR-040, FR-041, FR-043, FR-045) ────────────────────────

  const MOTIVOS = {
    credencial_invalida: "La llave no es válida o no tiene permiso.",
    sin_saldo: "La cuenta de la plataforma no tiene saldo.",
    modelo_inexistente: "Ese modelo no existe en esta plataforma.",
    no_usa_herramientas: "El modelo respondió, pero no usa herramientas: el agente no podría trabajar con él.",
    sin_respuesta: "La plataforma no respondió a tiempo.",
    rechazo: "La plataforma rechazó la prueba.",
  };

  function resumenDePlataforma(p) {
    const partes = ["Llave " + p.llave];
    partes.push(p.ultimoExito ? "último éxito: " + fecha(p.ultimoExito) : "sin éxitos registrados");
    if (p.ultimoErrorEn) partes.push("último error: " + fecha(p.ultimoErrorEn) + " — " + (p.ultimoError || ""));
    return partes.join(" · ");
  }

  function tarjetaDePlataforma(p) {
    const nota = el("p", { class: "mensaje", role: "status" });
    const resumen = el("p", { class: "tenue" }, resumenDePlataforma(p));
    const nombre = texto(p.nombre, { maxlength: 80 });
    const baseUrl = texto(p.baseUrl, { maxlength: 500 });
    const llave = el("input", { type: "password", autocomplete: "off", placeholder: "Se conserva " + p.llave });
    const modelo = texto("", { maxlength: 200, placeholder: "proveedor/modelo" });
    const extras = area("{}", 2, { class: "mono" });

    async function guardarCambios() {
      try {
        await api("PUT", "/plataformas/" + p.id, {
          nombre: nombre.value,
          baseUrl: baseUrl.value,
          ...(llave.value ? { llave: llave.value } : {}),
        });
        await recargar();
        pintarPlataformas();
      } catch (error) {
        decir(nota, explicar(error), "error");
      }
    }

    async function borrar() {
      if (!window.confirm("¿Borrar la plataforma «" + p.nombre + "»? Su llave y sus certificaciones se pierden.")) return;
      try {
        await api("DELETE", "/plataformas/" + p.id);
        await recargar();
        pintarPlataformas();
        pintarSeccion("cadena");
      } catch (error) {
        decir(nota, explicar(error), "error");
      }
    }

    async function probar(evento) {
      const extrasJson = objetoJson(extras.value);
      if (!modelo.value.trim() || !extrasJson) {
        decir(nota, "Escribe el modelo y unos extras en JSON (o {}).", "error");
        return;
      }
      evento.target.disabled = true;
      decir(nota, "Probando…");
      try {
        const r = await api("POST", "/plataformas/" + p.id + "/probar", { modelo: modelo.value.trim(), extras: extrasJson });
        if (r.ok) decir(nota, "Conectado en " + r.ms + " ms: respondió «" + r.modeloRespondio + "».", "ok");
        else decir(nota, (MOTIVOS[r.motivo] || r.motivo) + (r.detalle ? " " + r.detalle : ""), "error");
        const fresca = (await api("GET", "/plataformas")).find((x) => x.id === p.id);
        if (fresca) resumen.textContent = resumenDePlataforma(fresca);
      } catch (error) {
        decir(nota, explicar(error), "error");
      } finally {
        evento.target.disabled = false;
      }
    }

    return el(
      "article",
      { class: "tarjeta" },
      el("h3", {}, p.nombre),
      resumen,
      el("div", { class: "rejilla" }, campo("Nombre", nombre), campo("Dirección", baseUrl), campo("Llave nueva", llave, "Vacía, se conserva la guardada.")),
      el(
        "div",
        { class: "acciones" },
        el("button", { type: "button", onclick: guardarCambios }, "Guardar cambios"),
        el("button", { type: "button", class: "quitar", onclick: borrar }, "Borrar")
      ),
      el("div", { class: "rejilla" }, campo("Modelo a probar", modelo), campo("Extras (JSON)", extras)),
      el("div", { class: "acciones" }, el("button", { type: "button", onclick: probar }, "Probar conexión")),
      nota
    );
  }

  function formularioDeAlta() {
    const nota = el("p", { class: "mensaje", role: "status" });
    const nombre = texto("", { maxlength: 80, placeholder: "OpenRouter" });
    const baseUrl = texto("", { maxlength: 500, placeholder: "https://openrouter.ai/api/v1" });
    const llave = el("input", { type: "password", autocomplete: "off" });
    async function alta(evento) {
      evento.target.disabled = true;
      try {
        await api("POST", "/plataformas", { nombre: nombre.value, baseUrl: baseUrl.value, llave: llave.value });
        await recargar();
        pintarPlataformas();
        pintarSeccion("cadena"); // la cadena elige entre las plataformas registradas
      } catch (error) {
        decir(nota, explicar(error), "error");
        evento.target.disabled = false;
      }
    }
    return el(
      "article",
      { class: "tarjeta" },
      el("h3", {}, "Agregar plataforma"),
      el("div", { class: "rejilla" }, campo("Nombre", nombre), campo("Dirección (compatible con OpenAI)", baseUrl), campo("Llave", llave)),
      el("div", { class: "acciones" }, el("button", { type: "button", class: "primario", onclick: alta }, "Agregar")),
      nota
    );
  }

  function pintarPlataformas() {
    const lista = (estado.plataformas || []).map(tarjetaDePlataforma);
    $("#plataformas .lista-plataformas").replaceChildren(
      ...(lista.length ? lista : [el("p", { class: "tenue" }, "Todavía no hay plataformas.")]),
      formularioDeAlta()
    );
  }

  // ── Historial (FR-070, FR-071) ──────────────────────────────────────────

  async function pintarHistorial() {
    const cuerpo = $("#historial .cuerpo");
    const nota = el("p", { class: "mensaje", role: "status" });
    let versiones;
    try {
      versiones = await api("GET", "/versiones");
    } catch (error) {
      cuerpo.replaceChildren(el("p", { class: "mensaje error" }, explicar(error)));
      return;
    }
    async function restaurar(numero, boton) {
      boton.disabled = true;
      decir(nota, "Restaurando…");
      try {
        const r = await api("POST", "/versiones/" + numero + "/restaurar");
        await recargar();
        pintarTodo();
        agregarAvisos(["Versión " + numero + " restaurada como versión " + r.numero + ".", ...(r.avisos || [])]);
      } catch (error) {
        decir(nota, explicar(error), "error");
        boton.disabled = false;
      }
    }
    const filas = versiones.map((v) => {
      const accion =
        v.numero === estado.vigente.numero
          ? el("span", { class: "etiqueta ok" }, "vigente")
          : el("button", { type: "button", onclick: (e) => restaurar(v.numero, e.target) }, "Restaurar");
      return el(
        "tr",
        {},
        el("td", {}, String(v.numero)),
        el("td", {}, fecha(v.fecha)),
        el("td", {}, v.autor),
        el("td", {}, v.resumen),
        el("td", {}, accion)
      );
    });
    cuerpo.replaceChildren(
      el(
        "div",
        { class: "tabla" },
        el(
          "table",
          {},
          el("thead", {}, el("tr", {}, ...["Versión", "Fecha", "Autor", "Qué cambió", ""].map((t) => el("th", {}, t)))),
          el("tbody", {}, ...filas)
        )
      ),
      nota
    );
  }

  // ── Arranque ────────────────────────────────────────────────────────────

  function pintarTodo() {
    pintarCabecera();
    pintarAvisos(estado.avisos);
    for (const id of Object.keys(SECCIONES)) pintarSeccion(id);
    pintarPlataformas();
    pintarHistorial();
  }

  async function iniciar() {
    try {
      await recargar();
    } catch (error) {
      $("#vigente").textContent = "";
      $("#avisos").replaceChildren(el("p", { class: "peligro" }, explicar(error)));
      return;
    }
    pintarTodo();
  }

  iniciar();
})();
