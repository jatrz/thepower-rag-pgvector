"""Demostración visual del aislamiento multiempresa.

Ejecuta dos consultas reales con la misma pregunta y el mismo modelo. El
identificador de empresa se pasa de forma explícita: no se deduce de la
pregunta ni de Gemini. No fabrica respuestas ni modifica datos.

En producción, empresa_id debe proceder de una identidad autenticada por el
servidor. Aquí la selección manual es solo demostrativa.
"""

from __future__ import annotations

import re
import unicodedata

from src.ingest import EMPRESAS_ESPERADAS
from src.rag import SIN_INFORMACION, RespuestaRag, responder

PREGUNTA = "¿La empresa me ayuda a pagar la guardería de mi hijo?"
DOCUMENTO_GUARDERIA = "cs-guarderia"
ANCHO = 78
_PATRON_TOKEN = re.compile(r"(ya29\.[A-Za-z0-9._\-]+|Bearer\s+\S+)", re.IGNORECASE)


def main() -> None:
    """Consulta a Hornos Alba y a Clínica Sonrisa, y verifica el aislamiento."""
    print("DEMOSTRACIÓN DE AISLAMIENTO MULTIEMPRESA")
    print("Tecnologías: PostgreSQL + pgvector + RLS + Gemini")
    print(f"Pregunta común: {PREGUNTA}")
    print("empresa_id explícito; no se obtiene de la pregunta ni del modelo.")
    print("-" * ANCHO)

    fallos: list[str] = []
    for empresa_id in (1, 2):
        try:
            resultado = responder(empresa_id, PREGUNTA)
        except Exception as exc:
            print()
            print(f"Empresa {empresa_id} ({EMPRESAS_ESPERADAS.get(empresa_id, 'desconocida')})")
            print(f"ERROR: {_sanear(f'{type(exc).__name__}: {exc}')}")
            fallos.append(f"empresa {empresa_id}: la consulta no pudo completarse")
            continue
        comprobaciones = _comprobar(resultado)
        _mostrar(resultado, comprobaciones)
        fallos.extend(
            f"empresa {empresa_id}: {nombre}"
            for nombre, correcta in comprobaciones
            if not correcta
        )

    print("-" * ANCHO)
    if fallos:
        print("ERROR: el aislamiento no quedó verificado.")
        for fallo in fallos:
            print(f"- {fallo}")
        raise SystemExit(1)
    print("Resultado global: AISLAMIENTO MULTIEMPRESA VERIFICADO")


def _comprobar(resultado: RespuestaRag) -> tuple[tuple[str, bool], ...]:
    """Evalúa la respuesta real. No sustituye el texto devuelto por Gemini."""
    propios, ajenos = _recuento(resultado)
    documentos = _documentos(resultado)
    fuentes_propias = all(fuente.empresa_id == resultado.empresa_id for fuente in resultado.fuentes)
    fragmentos_aislados = propios == len(resultado.fragmentos) and ajenos == 0
    if resultado.empresa_id == 1:
        return (
            ("respuesta exacta de ausencia de información", resultado.respuesta == SIN_INFORMACION),
            ("fragmentos propios y ninguno ajeno", fragmentos_aislados),
            ("sin documento de guardería de otra empresa", DOCUMENTO_GUARDERIA not in documentos),
            ("sin fuentes citadas", resultado.fuentes == () and fuentes_propias),
            ("empresa y nombre coherentes", resultado.nombre_empresa == EMPRESAS_ESPERADAS[1]),
        )
    return (
        ("ayuda de guardería fundamentada", _fundamenta_guarderia(resultado.respuesta)),
        ("documento propio de guardería recuperado", DOCUMENTO_GUARDERIA in documentos),
        ("fragmentos propios y ninguno ajeno", fragmentos_aislados),
        (
            "fuentes propias y citadas",
            bool(resultado.fuentes) and fuentes_propias and _cita_guarderia(resultado),
        ),
        ("empresa y nombre coherentes", resultado.nombre_empresa == EMPRESAS_ESPERADAS[2]),
    )


def _mostrar(resultado: RespuestaRag, comprobaciones: tuple[tuple[str, bool], ...]) -> None:
    propios, ajenos = _recuento(resultado)
    print()
    print(f"{resultado.nombre_empresa} (empresa {resultado.empresa_id})")
    print("Respuesta:")
    for linea in _ajustar(resultado.respuesta):
        print(f"  {linea}")
    print(f"Documentos recuperados: {_lista(_documentos(resultado))}")
    print(f"Fragmentos propios: {propios} | ajenos: {ajenos}")
    print(f"Fuentes utilizadas: {_fuentes(resultado)}")
    print("Comprobaciones:")
    for nombre, correcta in comprobaciones:
        estado = "correcta" if correcta else "error"
        print(f"  [{estado}] {nombre}")


def _fundamenta_guarderia(respuesta: str) -> bool:
    """Acepta la cifra o su forma escrita, sin fijar el resto de la redacción."""
    if respuesta.strip() == SIN_INFORMACION:
        return False
    texto = _sin_acentos(respuesta).lower()
    cantidad = re.search(r"\b150\b", texto) is not None or "ciento cincuenta" in texto
    return "guarder" in texto and "euro" in texto and "mensual" in texto and cantidad


def _cita_guarderia(resultado: RespuestaRag) -> bool:
    recuperados = {
        (fragmento.documento_id, fragmento.numero)
        for fragmento in resultado.fragmentos
        if fragmento.empresa_id == resultado.empresa_id
    }
    return any(
        fuente.documento_id == DOCUMENTO_GUARDERIA and (fuente.documento_id, fuente.numero) in recuperados
        for fuente in resultado.fuentes
    )


def _recuento(resultado: RespuestaRag) -> tuple[int, int]:
    propios = sum(fragmento.empresa_id == resultado.empresa_id for fragmento in resultado.fragmentos)
    return propios, len(resultado.fragmentos) - propios


def _documentos(resultado: RespuestaRag) -> tuple[str, ...]:
    vistos: list[str] = []
    for fragmento in resultado.fragmentos:
        if fragmento.documento_id not in vistos:
            vistos.append(fragmento.documento_id)
    return tuple(vistos)


def _fuentes(resultado: RespuestaRag) -> str:
    if not resultado.fuentes:
        return "ninguna"
    return ", ".join(f"{fuente.documento_id} #{fuente.numero}" for fuente in resultado.fuentes)


def _lista(valores: tuple[str, ...]) -> str:
    return ", ".join(valores) if valores else "ninguno"


def _ajustar(texto: str) -> list[str]:
    palabras = texto.split()
    if not palabras:
        return ["(respuesta vacía)"]
    lineas: list[str] = []
    actual = ""
    for palabra in palabras:
        propuesto = palabra if not actual else f"{actual} {palabra}"
        if len(propuesto) <= ANCHO - 2:
            actual = propuesto
        else:
            lineas.append(actual)
            actual = palabra
    if actual:
        lineas.append(actual)
    return lineas


def _sin_acentos(texto: str) -> str:
    return "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caracter) != "Mn"
    )


def _sanear(mensaje: str) -> str:
    return _PATRON_TOKEN.sub("***", mensaje)


if __name__ == "__main__":
    main()
