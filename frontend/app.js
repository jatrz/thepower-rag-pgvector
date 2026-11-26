"use strict";

const MAX_CARACTERES = 240;
const IDS_ADMITIDOS = new Set([1, 2, 3]);
const CLAVE_TEMA = "enterprise-rag-theme";
const PREGUNTA_AISLAMIENTO = "¿La empresa me ayuda a pagar la guardería de mi hijo?";
const RESULTADO_VERIFICADO = "AISLAMIENTO MULTIEMPRESA VERIFICADO";

const nodos = {};
let empresas = [];
let controladorConsulta = null;
let controladorAislamiento = null;
let respuestaCopiable = "";

document.addEventListener("DOMContentLoaded", iniciar);

function iniciar() {
  const ids = [
    "estado-postgresql", "estado-gemini", "aviso-salud", "region-estado",
    "boton-tema", "boton-actualizar-salud", "metric-empresas", "metric-empresas-nota",
    "aviso-empresas", "selector-empresas", "formulario-consulta", "pregunta",
    "contador-valor", "boton-consultar", "boton-cancelar-consulta", "boton-copiar",
    "boton-limpiar", "consulta-vacia", "consulta-cargando", "consulta-cronometro",
    "consulta-error", "consulta-error-texto", "consulta-resultado", "resultado-empresa",
    "resultado-duracion", "resultado-propios", "resultado-ajenos", "resultado-respuesta",
    "resultado-documentos", "resultado-fuentes", "resultado-similitudes", "resultado-fragmentos",
    "boton-aislamiento", "boton-cancelar-aislamiento", "boton-imprimir",
    "aislamiento-pendiente", "aislamiento-cargando", "aislamiento-cronometro",
    "aislamiento-error", "aislamiento-error-texto", "aislamiento-resultado",
    "aislamiento-banner", "aislamiento-pregunta-real", "aislamiento-coherencia",
    "aislamiento-tarjetas", "aislamiento-superadas", "aislamiento-duracion",
    "aislamiento-global"
  ];
  for (const id of ids) {
    nodos[id] = document.getElementById(id);
    if (!nodos[id]) {
      document.body.textContent = "La interfaz está incompleta y no puede iniciarse.";
      return;
    }
  }

  sincronizarTema();
  nodos["boton-tema"].addEventListener("click", alternarTema);
  nodos["boton-actualizar-salud"].addEventListener("click", () => cargarSalud());
  nodos.pregunta.addEventListener("input", actualizarContador);
  nodos["formulario-consulta"].addEventListener("submit", enviarConsulta);
  nodos["boton-cancelar-consulta"].addEventListener("click", () => cancelar("consulta"));
  nodos["boton-limpiar"].addEventListener("click", limpiarConsulta);
  nodos["boton-copiar"].addEventListener("click", copiarRespuesta);
  nodos["boton-aislamiento"].addEventListener("click", ejecutarAislamiento);
  nodos["boton-cancelar-aislamiento"].addEventListener("click", () => cancelar("aislamiento"));
  nodos["boton-imprimir"].addEventListener("click", () => window.print());
  document.querySelectorAll("[data-pregunta]").forEach((boton) => {
    boton.addEventListener("click", () => sugerir(boton.dataset.pregunta || ""));
  });

  actualizarContador();
  cargarInicio();
}

function sincronizarTema() {
  const claro = document.documentElement.dataset.theme === "light";
  nodos["boton-tema"].textContent = claro ? "Modo oscuro" : "Modo claro";
  nodos["boton-tema"].setAttribute("aria-pressed", claro ? "false" : "true");
}

function alternarTema() {
  const siguiente = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = siguiente;
  try {
    localStorage.setItem(CLAVE_TEMA, siguiente);
  } catch (error) {
    anunciar("No se pudo guardar la preferencia visual.");
  }
  sincronizarTema();
}

async function cargarInicio() {
  await Promise.all([cargarSalud(), cargarEmpresas()]);
}

async function cargarSalud() {
  nodos["estado-postgresql"].className = "pastilla pendiente";
  nodos["estado-postgresql"].textContent = "Comprobando PostgreSQL…";
  nodos["estado-gemini"].textContent = "Gemini: no comprobado por /api/health";
  try {
    const datos = await solicitar("/api/health");
    pintarSalud(datos);
  } catch (error) {
    if (esObjeto(error.datos) && esObjeto(error.datos.postgresql)) {
      pintarSalud(error.datos);
      return;
    }
    nodos["estado-postgresql"].className = "pastilla error";
    nodos["estado-postgresql"].textContent = "PostgreSQL no disponible";
    nodos["aviso-salud"].textContent = mensajeError(error);
  }
}

function pintarSalud(datos) {
  if (!esObjeto(datos) || !esObjeto(datos.postgresql)) {
    throw new Error("invalida");
  }
  const postgres = datos.postgresql;
  const conectado = datos.estado === "ok" && postgres.conectado === true && postgres.administrador === false;
  nodos["estado-postgresql"].className = conectado ? "pastilla ok" : "pastilla error";
  const comprobadas = Array.isArray(postgres.empresas_comprobadas) ? postgres.empresas_comprobadas.length : 0;
  nodos["estado-postgresql"].textContent = conectado
    ? `PostgreSQL conectado · ${comprobadas} roles de empresa`
    : "PostgreSQL no disponible";
  nodos["estado-gemini"].textContent = datos.gemini_comprobado === true
    ? "Gemini: el endpoint indica comprobado"
    : "Gemini: no comprobado por /api/health";
  nodos["aviso-salud"].textContent = typeof datos.aviso === "string" ? datos.aviso : "";
}

async function cargarEmpresas() {
  try {
    const datos = await solicitar("/api/empresas");
    if (!esObjeto(datos) || !Array.isArray(datos.empresas)) {
      throw new Error("invalida");
    }
    empresas = datos.empresas.filter((empresa) => esEmpresa(empresa));
    pintarEmpresas(typeof datos.aviso === "string" ? datos.aviso : "");
    nodos["metric-empresas"].textContent = String(datos.empresas.length);
    nodos["metric-empresas-nota"].textContent = "Obtenido ahora de GET /api/empresas. No incluye documentos ni fragmentos.";
  } catch (error) {
    empresas = [];
    nodos["selector-empresas"].replaceChildren();
    nodos["selector-empresas"].append(crear("p", "estado-suave", mensajeError(error)));
    nodos["metric-empresas"].textContent = "—";
    nodos["boton-consultar"].disabled = true;
  }
}

function pintarEmpresas(aviso) {
  const contenedor = nodos["selector-empresas"];
  contenedor.replaceChildren();
  if (aviso) {
    nodos["aviso-empresas"].textContent = aviso;
  }
  if (empresas.length === 0) {
    contenedor.append(crear("p", "estado-suave", "No hay empresas disponibles."));
    nodos["boton-consultar"].disabled = true;
    return;
  }
  empresas.forEach((empresa, indice) => {
    const id = `empresa-${empresa.id}`;
    const etiqueta = crear("label", "opcion-empresa");
    etiqueta.htmlFor = id;
    const entrada = document.createElement("input");
    entrada.type = "radio";
    entrada.name = "empresa_id";
    entrada.id = id;
    entrada.value = String(empresa.id);
    entrada.checked = indice === 0;
    entrada.disabled = !IDS_ADMITIDOS.has(empresa.id);
    const texto = crear("span");
    texto.append(
      crear("strong", "", `${empresa.nombre}`),
      document.createTextNode(` · identificador ${empresa.id}`)
    );
    if (entrada.disabled) {
      texto.append(document.createTextNode(" · identificador no admitido por la API"));
    }
    etiqueta.append(entrada, texto);
    contenedor.append(etiqueta);
  });
  nodos["boton-consultar"].disabled = ocupado() || !empresaSeleccionada();
}

function sugerir(pregunta) {
  nodos.pregunta.value = pregunta;
  actualizarContador();
  nodos.pregunta.focus();
}

function actualizarContador() {
  nodos["contador-valor"].textContent = String(nodos.pregunta.value.length);
}

async function enviarConsulta(evento) {
  evento.preventDefault();
  if (controladorConsulta || controladorAislamiento) {
    return;
  }
  const pregunta = nodos.pregunta.value.trim();
  const empresa = empresaSeleccionada();
  const errorLocal = validarConsulta(empresa, pregunta);
  if (errorLocal) {
    mostrarErrorConsulta(errorLocal);
    return;
  }
  controladorConsulta = new AbortController();
  ocupar(true);
  mostrarEstadoConsulta("cargando");
  const detener = cronometro(nodos["consulta-cronometro"]);
  anunciar("Consulta en curso.");
  try {
    const datos = await solicitar("/api/consulta", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ empresa_id: empresa.id, pregunta })
    }, controladorConsulta.signal);
    pintarConsulta(datos, empresa.id);
    anunciar("Consulta completada.");
  } catch (error) {
    if (error.name === "AbortError") {
      mostrarErrorConsulta("Consulta cancelada. No se muestra una respuesta.");
      anunciar("Consulta cancelada.");
    } else {
      mostrarErrorConsulta(mensajeError(error));
      anunciar("La consulta no pudo completarse.");
    }
  } finally {
    detener();
    controladorConsulta = null;
    ocupar(false);
  }
}

function validarConsulta(empresa, pregunta) {
  if (!empresa || !IDS_ADMITIDOS.has(empresa.id)) {
    return "Selecciona una de las empresas admitidas por la API.";
  }
  if (!pregunta || pregunta.includes("\u0000") || pregunta.length > MAX_CARACTERES) {
    return "La pregunta debe tener entre 1 y 240 caracteres y no puede incluir caracteres nulos.";
  }
  return "";
}

function pintarConsulta(datos, empresaId) {
  if (!consultaValida(datos, empresaId)) {
    mostrarErrorConsulta("La respuesta no coincide con el contrato o incluye datos de otra empresa. No se muestra el contenido.");
    return;
  }
  mostrarEstadoConsulta("resultado");
  nodos["resultado-empresa"].textContent = `${datos.nombre_empresa} · identificador ${datos.empresa_id}`;
  nodos["resultado-duracion"].textContent = formatearSegundos(datos.duracion_s);
  nodos["resultado-propios"].textContent = String(datos.fragmentos_propios);
  nodos["resultado-ajenos"].textContent = String(datos.fragmentos_ajenos);
  nodos["resultado-ajenos"].className = datos.fragmentos_ajenos === 0 ? "ajeno-ok" : "ajeno-alerta";
  nodos["resultado-respuesta"].textContent = datos.respuesta;
  nodos["resultado-similitudes"].textContent = textoSimilitudes(datos.similitudes);
  respuestaCopiable = datos.respuesta;
  llenarLista(nodos["resultado-documentos"], datos.documentos_recuperados, "Ningún documento recuperado.");
  pintarFuentes(nodos["resultado-fuentes"], datos.fuentes);
  pintarFragmentos(datos.fragmentos);
  nodos["boton-copiar"].disabled = false;
  nodos["boton-limpiar"].disabled = false;
}

function consultaValida(datos, empresaId) {
  if (!esObjeto(datos) || datos.empresa_id !== empresaId) {
    return false;
  }
  if (typeof datos.nombre_empresa !== "string" || typeof datos.pregunta !== "string" || typeof datos.respuesta !== "string") {
    return false;
  }
  if (!Array.isArray(datos.documentos_recuperados) || !Array.isArray(datos.fragmentos) || !Array.isArray(datos.fuentes)) {
    return false;
  }
  if (datos.fragmentos_ajenos !== 0 || datos.fragmentos_propios !== datos.fragmentos.length) {
    return false;
  }
  return datos.fragmentos.every((fragmento) => fragmentoSeguro(fragmento, empresaId))
    && datos.fuentes.every((fuente) => fuenteSegura(fuente, empresaId))
    && datos.documentos_recuperados.every((documento) => typeof documento === "string");
}

function fragmentoSeguro(fragmento, empresaId) {
  return esObjeto(fragmento)
    && fragmento.empresa_id === empresaId
    && typeof fragmento.documento_id === "string"
    && typeof fragmento.titulo === "string"
    && Number.isInteger(fragmento.numero)
    && typeof fragmento.similitud === "number"
    && typeof fragmento.texto === "string";
}

function fuenteSegura(fuente, empresaId) {
  return esObjeto(fuente)
    && fuente.empresa_id === empresaId
    && typeof fuente.documento_id === "string"
    && typeof fuente.titulo === "string"
    && Number.isInteger(fuente.numero);
}

function pintarFuentes(lista, fuentes) {
  lista.replaceChildren();
  if (fuentes.length === 0) {
    lista.append(crear("li", "", "Ninguna fuente citada."));
    return;
  }
  fuentes.forEach((fuente) => {
    const item = crear("li");
    item.append(
      crear("strong", "", fuente.titulo),
      document.createTextNode(` · ${fuente.documento_id} · fragmento ${fuente.numero}`)
    );
    lista.append(item);
  });
}

function pintarFragmentos(fragmentos) {
  const contenedor = nodos["resultado-fragmentos"];
  contenedor.replaceChildren();
  if (fragmentos.length === 0) {
    contenedor.append(crear("p", "estado-suave", "No se recuperaron fragmentos."));
    return;
  }
  fragmentos.forEach((fragmento) => {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `${fragmento.titulo} · fragmento ${fragmento.numero}`;
    const cuerpo = crear("div", "fragmento-cuerpo");
    const meta = crear("p", "fuente-meta", `Documento recuperado: ${fragmento.documento_id}. Empresa ${fragmento.empresa_id}.`);
    cuerpo.append(meta, barraSimilitud(fragmento.similitud), crear("p", "fragmento-texto", fragmento.texto));
    details.append(summary, cuerpo);
    contenedor.append(details);
  });
}

function barraSimilitud(valor) {
  const contenedor = crear("div", "similitud");
  const texto = Number.isFinite(valor) ? valor.toFixed(6) : "no disponible";
  const ancho = Number.isFinite(valor) ? Math.max(0, Math.min(1, valor)) * 100 : 0;
  contenedor.append(
    crear("p", "", `Similitud coseno: ${texto}`),
    crear("p", "similitud-nota", "No es una probabilidad ni la confianza del modelo.")
  );
  const barra = crear("div", "barra");
  barra.setAttribute("role", "img");
  barra.setAttribute("aria-label", `Similitud coseno ${texto}. No representa probabilidad ni confianza del modelo.`);
  const relleno = crear("span", "barra-relleno");
  relleno.style.width = `${ancho}%`;
  barra.append(relleno);
  contenedor.append(barra);
  return contenedor;
}

function limpiarConsulta() {
  if (controladorConsulta) {
    return;
  }
  respuestaCopiable = "";
  nodos["resultado-respuesta"].textContent = "";
  nodos["resultado-documentos"].replaceChildren();
  nodos["resultado-fuentes"].replaceChildren();
  nodos["resultado-similitudes"].textContent = "";
  nodos["resultado-fragmentos"].replaceChildren();
  nodos["boton-copiar"].disabled = true;
  nodos["boton-limpiar"].disabled = true;
  mostrarEstadoConsulta("vacia");
  anunciar("Resultados de la consulta limpiados.");
}

async function copiarRespuesta() {
  if (!respuestaCopiable) {
    return;
  }
  try {
    await navigator.clipboard.writeText(respuestaCopiable);
    anunciar("Respuesta copiada.");
  } catch (error) {
    anunciar("No se pudo copiar la respuesta.");
  }
}

async function ejecutarAislamiento() {
  if (controladorConsulta || controladorAislamiento) {
    return;
  }
  controladorAislamiento = new AbortController();
  ocupar(true);
  mostrarEstadoAislamiento("cargando");
  const detener = cronometro(nodos["aislamiento-cronometro"]);
  anunciar("Prueba de aislamiento en curso.");
  try {
    const datos = await solicitar("/api/aislamiento", { method: "POST" }, controladorAislamiento.signal);
    pintarAislamiento(datos);
    anunciar("Prueba de aislamiento completada.");
  } catch (error) {
    if (error.name === "AbortError") {
      mostrarErrorAislamiento("Prueba cancelada. No se muestran resultados.");
      anunciar("Prueba de aislamiento cancelada.");
    } else {
      mostrarErrorAislamiento(mensajeError(error));
      anunciar("La prueba de aislamiento no pudo completarse.");
    }
  } finally {
    detener();
    controladorAislamiento = null;
    ocupar(false);
  }
}

function pintarAislamiento(datos) {
  if (!aislamientoValido(datos)) {
    mostrarErrorAislamiento("La respuesta de aislamiento no es válida o incluye datos de otra empresa. No se muestra el contenido.");
    return;
  }
  const coherente = aislamientoCoherente(datos);
  mostrarEstadoAislamiento("resultado");
  nodos["aislamiento-pregunta-real"].textContent = datos.pregunta;
  nodos["aislamiento-coherencia"].hidden = datos.pregunta === PREGUNTA_AISLAMIENTO;
  nodos["aislamiento-coherencia"].textContent = datos.pregunta === PREGUNTA_AISLAMIENTO
    ? ""
    : "La pregunta devuelta por el servidor no coincide con la pregunta prevista.";

  const banner = nodos["aislamiento-banner"];
  if (coherente) {
    banner.className = "banner ok";
    banner.textContent = datos.resultado_global;
  } else {
    banner.className = "banner error";
    banner.textContent = datos.aislamiento_verificado === true
      ? "El backend marcó el aislamiento como verificado, pero las comprobaciones no son coherentes."
      : "Aislamiento no verificado. Alguna comprobación ha fallado.";
  }

  const tarjetas = nodos["aislamiento-tarjetas"];
  tarjetas.replaceChildren();
  datos.empresas.forEach((empresa) => tarjetas.append(tarjetaEmpresa(empresa)));

  const comprobaciones = datos.empresas.flatMap((empresa) => empresa.comprobaciones);
  const superadas = comprobaciones.filter((item) => item.correcta === true).length;
  nodos["aislamiento-superadas"].textContent = `${superadas} de ${comprobaciones.length}`;
  nodos["aislamiento-duracion"].textContent = formatearSegundos(datos.duracion_s);
  nodos["aislamiento-global"].textContent = datos.resultado_global;
  nodos["aislamiento-global"].className = coherente ? "correcta" : "incorrecta";
  nodos["boton-imprimir"].disabled = false;
}

function aislamientoValido(datos) {
  if (!esObjeto(datos) || typeof datos.pregunta !== "string" || typeof datos.resultado_global !== "string") {
    return false;
  }
  if (typeof datos.aislamiento_verificado !== "boolean" || typeof datos.duracion_s !== "number") {
    return false;
  }
  if (!Array.isArray(datos.empresas) || datos.empresas.length !== 2) {
    return false;
  }
  return datos.empresas.every((empresa) => {
    if (!esObjeto(empresa) || !IDS_ADMITIDOS.has(empresa.empresa_id)) {
      return false;
    }
    if (typeof empresa.nombre_empresa !== "string" || typeof empresa.respuesta !== "string") {
      return false;
    }
    if (typeof empresa.correcta !== "boolean" || typeof empresa.duracion_s !== "number") {
      return false;
    }
    if (!Number.isInteger(empresa.fragmentos_ajenos) || empresa.fragmentos_ajenos < 0 || !Number.isInteger(empresa.fragmentos_propios)) {
      return false;
    }
    if (!Array.isArray(empresa.documentos_recuperados) || !Array.isArray(empresa.fuentes) || !Array.isArray(empresa.comprobaciones)) {
      return false;
    }
    return empresa.documentos_recuperados.every((documento) => typeof documento === "string")
      && empresa.fuentes.every((fuente) => fuenteSegura(fuente, empresa.empresa_id))
      && empresa.comprobaciones.every((item) => esObjeto(item) && typeof item.nombre === "string" && typeof item.correcta === "boolean");
  });
}

function aislamientoCoherente(datos) {
  if (datos.aislamiento_verificado !== true || datos.resultado_global !== RESULTADO_VERIFICADO) {
    return false;
  }
  return datos.empresas.every((empresa) => {
    return empresa.correcta === true
      && empresa.fragmentos_ajenos === 0
      && empresa.comprobaciones.length > 0
      && empresa.comprobaciones.every((item) => item.correcta === true);
  });
}

function tarjetaEmpresa(empresa) {
  const tarjeta = crear("article", "tarjeta-empresa");
  tarjeta.append(
    crear("p", "tarjeta-meta", `Empresa ${empresa.empresa_id}`),
    crear("h3", "", empresa.nombre_empresa),
    crear("p", empresa.correcta ? "correcta" : "incorrecta", empresa.correcta ? "Comprobación de empresa correcta" : "Comprobación de empresa incorrecta"),
    crear("h4", "", "Respuesta"),
    crear("p", "respuesta-empresa", empresa.respuesta)
  );
  const datos = crear("div", "dato-linea");
  datos.append(
    crear("p", "", `Fragmentos propios: ${empresa.fragmentos_propios}`),
    crear("p", empresa.fragmentos_ajenos === 0 ? "ajeno-ok" : "ajeno-alerta", `Fragmentos ajenos: ${empresa.fragmentos_ajenos}`),
    crear("p", "", `Tiempo individual: ${formatearSegundos(empresa.duracion_s)}`)
  );
  tarjeta.append(datos, crear("h4", "", "Documentos recuperados"));
  const documentos = crear("ul", "lista");
  llenarLista(documentos, empresa.documentos_recuperados, "Ningún documento recuperado.");
  tarjeta.append(documentos, crear("h4", "", "Fuentes citadas"));
  const fuentes = crear("ul", "lista");
  pintarFuentes(fuentes, empresa.fuentes);
  tarjeta.append(fuentes, crear("h4", "", "Comprobaciones"));
  const lista = crear("ul", "comprobaciones");
  empresa.comprobaciones.forEach((item) => {
    const fila = document.createElement("li");
    fila.append(crear("span", "", item.nombre), crear("span", item.correcta ? "correcta" : "incorrecta", item.correcta ? "Correcta" : "Incorrecta"));
    lista.append(fila);
  });
  tarjeta.append(lista);
  return tarjeta;
}

function llenarLista(lista, valores, vacio) {
  lista.replaceChildren();
  if (valores.length === 0) {
    lista.append(crear("li", "", vacio));
    return;
  }
  valores.forEach((valor) => lista.append(crear("li", "", valor)));
}

function mostrarEstadoConsulta(estado) {
  nodos["consulta-vacia"].hidden = estado !== "vacia";
  nodos["consulta-cargando"].hidden = estado !== "cargando";
  nodos["consulta-error"].hidden = estado !== "error";
  nodos["consulta-resultado"].hidden = estado !== "resultado";
}

function mostrarErrorConsulta(texto) {
  respuestaCopiable = "";
  nodos["boton-copiar"].disabled = true;
  nodos["boton-limpiar"].disabled = false;
  nodos["consulta-error-texto"].textContent = texto;
  mostrarEstadoConsulta("error");
}

function mostrarEstadoAislamiento(estado) {
  nodos["aislamiento-pendiente"].hidden = estado !== "pendiente";
  nodos["aislamiento-cargando"].hidden = estado !== "cargando";
  nodos["aislamiento-error"].hidden = estado !== "error";
  nodos["aislamiento-resultado"].hidden = estado !== "resultado";
}

function mostrarErrorAislamiento(texto) {
  nodos["boton-imprimir"].disabled = false;
  nodos["aislamiento-error-texto"].textContent = texto;
  mostrarEstadoAislamiento("error");
}

function textoSimilitudes(similitudes) {
  if (!Array.isArray(similitudes) || similitudes.some((valor) => typeof valor !== "number")) {
    return "La respuesta no incluye una lista numérica de similitudes. Cada barra usa la similitud coseno del fragmento.";
  }
  if (similitudes.length === 0) {
    return "No hay similitudes coseno en la respuesta.";
  }
  return `Similitudes coseno devueltas: ${similitudes.map((valor) => valor.toFixed(6)).join(", ")}. No son probabilidades.`;
}

function ocupado() {
  return Boolean(controladorConsulta || controladorAislamiento);
}

function ocupar(activo) {
  nodos["boton-consultar"].disabled = activo || !empresaSeleccionada();
  nodos["boton-aislamiento"].disabled = activo;
  nodos["boton-cancelar-consulta"].hidden = !controladorConsulta;
  nodos["boton-cancelar-aislamiento"].hidden = !controladorAislamiento;
  document.querySelectorAll("[data-pregunta]").forEach((boton) => {
    boton.disabled = activo;
  });
}

function cancelar(tipo) {
  if (tipo === "consulta" && controladorConsulta) {
    controladorConsulta.abort();
  }
  if (tipo === "aislamiento" && controladorAislamiento) {
    controladorAislamiento.abort();
  }
}

function cronometro(nodo) {
  const inicio = performance.now();
  nodo.textContent = "0 s";
  const id = window.setInterval(() => {
    nodo.textContent = `${Math.floor((performance.now() - inicio) / 1000)} s`;
  }, 250);
  return () => window.clearInterval(id);
}

async function solicitar(url, opciones = {}, signal) {
  let respuesta;
  try {
    respuesta = await fetch(url, {
      method: opciones.method || "GET",
      headers: { Accept: "application/json", ...(opciones.headers || {}) },
      body: opciones.body,
      cache: "no-store",
      credentials: "same-origin",
      signal
    });
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw error;
    }
    const fallo = new Error("conexion");
    fallo.causa = "conexion";
    throw fallo;
  }
  const texto = await respuesta.text();
  let datos = null;
  if (texto) {
    try {
      datos = JSON.parse(texto);
    } catch (error) {
      datos = null;
    }
  }
  if (!respuesta.ok) {
    const fallo = new Error("http");
    fallo.estado = respuesta.status;
    fallo.detalle = esObjeto(datos) && typeof datos.detalle === "string" ? datos.detalle : "";
    fallo.errores = esObjeto(datos) && Array.isArray(datos.errores) ? datos.errores : [];
    fallo.datos = datos;
    throw fallo;
  }
  if (!esObjeto(datos)) {
    throw new Error("invalida");
  }
  return datos;
}

function mensajeError(error) {
  if (!error || error.message === "invalida") {
    return "La respuesta del servidor no es válida.";
  }
  if (error.causa === "conexion" || error.message === "conexion") {
    return "No se pudo conectar con el servidor local.";
  }
  if (typeof error.detalle === "string" && error.detalle && error.detalle.length <= 240) {
    const extra = error.errores
      .filter((item) => esObjeto(item) && typeof item.campo === "string")
      .map((item) => item.campo)
      .slice(0, 4)
      .join(", ");
    return extra ? `${error.detalle} Campos: ${extra}.` : error.detalle;
  }
  if (typeof error.estado === "number") {
    return `La petición fue rechazada con el estado ${error.estado}.`;
  }
  return "No se pudo completar la operación.";
}

function empresaSeleccionada() {
  const marcada = document.querySelector("input[name='empresa_id']:checked");
  if (!marcada) {
    return null;
  }
  const id = Number(marcada.value);
  return empresas.find((empresa) => empresa.id === id && IDS_ADMITIDOS.has(id)) || null;
}

function esEmpresa(empresa) {
  return esObjeto(empresa) && Number.isInteger(empresa.id) && typeof empresa.nombre === "string" && empresa.nombre.length > 0;
}

function esObjeto(valor) {
  return valor !== null && typeof valor === "object" && !Array.isArray(valor);
}

function formatearSegundos(valor) {
  return Number.isFinite(valor) ? `${valor.toFixed(3)} s` : "no disponible";
}

function crear(etiqueta, clase, texto) {
  const nodo = document.createElement(etiqueta);
  if (clase) {
    nodo.className = clase;
  }
  if (texto !== undefined) {
    nodo.textContent = texto;
  }
  return nodo;
}

function anunciar(texto) {
  nodos["region-estado"].textContent = texto;
}
