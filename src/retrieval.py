"""Búsqueda vectorial semántica limitada a una empresa.

La consulta usa la distancia coseno de pgvector y el mismo modelo local de la
ingesta. No llama a Gemini ni a ningún servicio externo de embeddings.

Seguridad: ``empresa_id`` debe proceder, en producción, de una identidad
autenticada por el servidor. No se deduce de la pregunta ni de un modelo de
lenguaje. La selección manual de las cinco búsquedas de ``main`` es solo
demostrativa. Este módulo no abre la conexión administrativa, no desactiva RLS
y mantiene además el filtro explícito por ``empresa_id``.
"""

from __future__ import annotations

import math
import sys
from decimal import Decimal
from dataclasses import dataclass
from typing import Any

import numpy as np
import psycopg
from pgvector import Vector

from src.config import ConfiguracionError, rol_de_empresa
from src.database import ErrorConexion, conexion_empresa
from src.embeddings import EMBEDDING_DIM, generate_embedding
from src.ingest import EMPRESAS_ESPERADAS, SEPARADOR_DOCUMENTO

# La estructura está fijada por el contrato de búsqueda. Los cuatro valores
# llegan como parámetros; no se concatena la pregunta ni el identificador.
CONSULTA_BUSQUEDA = """
SELECT
    empresa_id,
    documento,
    texto,
    1 - (embedding <=> %s) AS similitud
FROM fragmentos
WHERE empresa_id = %s
ORDER BY embedding <=> %s, id
LIMIT %s
"""

TOP_K_PREDETERMINADO = 3

# Preguntas de la prueba. El identificador no se extrae del texto.
BUSQUEDAS_DEMOSTRACION = (
    (1, "¿Cuántos días de vacaciones tengo?"),
    (2, "¿Cuántos días de vacaciones tengo?"),
    (3, "¿Cuántos días de vacaciones tengo?"),
    (1, "¿La empresa me ayuda a pagar la guardería de mi hijo?"),
    (2, "¿La empresa me ayuda a pagar la guardería de mi hijo?"),
)


class RecuperacionError(RuntimeError):
    """La búsqueda no puede devolver resultados sin romper el aislamiento."""


@dataclass(frozen=True, slots=True)
class ResultadoBusqueda:
    """Fragmento recuperado, ya interpretado y atribuido a una empresa."""

    empresa_id: int
    documento_id: str
    titulo: str
    numero: int
    texto: str
    similitud: float


def buscar(
    empresa_id: int,
    pregunta: str,
    top_k: int = TOP_K_PREDETERMINADO,
) -> list[ResultadoBusqueda]:
    """Recupera los fragmentos más próximos de una sola empresa.

    Args:
        empresa_id: Empresa cuyas políticas RLS y credenciales se usan.
            En producción debe llegar de una identidad autenticada por el
            servidor, nunca de la pregunta ni del LLM.
        pregunta: Texto de la consulta. No se usa para elegir la empresa.
        top_k: Número máximo de fragmentos. El predeterminado es 3.

    Returns:
        Fragmentos de esa empresa, de mayor a menor similitud coseno.
        No se aplica ningún umbral de similitud.

    Raises:
        RecuperacionError: Si los parámetros no son válidos, el formato de
            ``documento`` no se puede interpretar o algún fragmento no
            pertenece a la empresa solicitante. En ese último caso no se
            devuelve ningún resultado.
        ConfiguracionError: Si ``empresa_id`` no tiene un rol conocido.
        ErrorConexion: Si falla la conexión de esa empresa. No se reintenta
            con el rol administrativo.
    """
    pregunta_valida = _validar_parametros(empresa_id, pregunta, top_k)
    rol_esperado = rol_de_empresa(empresa_id)
    vector = _embedding_de_pregunta(pregunta_valida)
    with conexion_empresa(empresa_id) as conexion:
        # register_vector ya ejecutó una consulta al abrir la conexión.
        # Se cierra esa transacción de catálogo, sin confirmar datos de negocio,
        # para poder marcar como solo lectura la transacción de búsqueda.
        conexion.rollback()
        with conexion.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            _exigir_rol_de_empresa(cursor, rol_esperado)
            cursor.execute(
                CONSULTA_BUSQUEDA,
                (vector, empresa_id, vector, top_k),
            )
            filas = cursor.fetchall()
        # Dentro de la transacción: una discrepancia la revierte y no hay retorno.
        return _resultados_de_la_empresa(empresa_id, filas)


def main() -> None:
    """Ejecuta las cinco búsquedas demostrativas sin modificar datos."""
    print(
        "Prueba de recuperación. empresa_id se pasa de forma explícita; "
        "no se obtiene de la pregunta."
    )
    try:
        for empresa_id, pregunta in BUSQUEDAS_DEMOSTRACION:
            resultados = buscar(empresa_id, pregunta)
            _afirmar_aislamiento(empresa_id, resultados)
            _mostrar_busqueda(empresa_id, pregunta, resultados)
    except (RecuperacionError, ConfiguracionError, ErrorConexion, AssertionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(
            f"ERROR: fallo inesperado ({type(exc).__name__}). "
            "No se muestran detalles para evitar revelar secretos.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def _validar_parametros(empresa_id: object, pregunta: object, top_k: object) -> str:
    """Rechaza la búsqueda antes de calcular embeddings o abrir conexiones."""
    if type(empresa_id) is not int:
        raise RecuperacionError(
            "empresa_id debe ser un entero conocido, no un valor deducido de la pregunta."
        )
    if not isinstance(pregunta, str) or not pregunta.strip():
        raise RecuperacionError("La pregunta debe ser una cadena no vacía.")
    if type(top_k) is not int or top_k < 1:
        raise RecuperacionError("top_k debe ser un entero mayor o igual que 1.")
    return pregunta.strip()


def _embedding_de_pregunta(pregunta: str) -> Vector:
    """Genera el vector local con el mismo contrato que la ingesta."""
    vector = generate_embedding(pregunta)
    if vector.shape != (EMBEDDING_DIM,) or vector.dtype != np.float32:
        raise RecuperacionError(
            f"La pregunta debe producir un embedding float32 de {EMBEDDING_DIM} dimensiones."
        )
    if not np.isfinite(vector).all():
        raise RecuperacionError("El embedding de la pregunta contiene valores no finitos.")
    return Vector(np.ascontiguousarray(vector, dtype=np.float32))


def _exigir_rol_de_empresa(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    rol_esperado: str,
) -> None:
    """Comprueba que la sesión no es el administrador ni otra empresa."""
    cursor.execute("SELECT current_user")
    fila = cursor.fetchone()
    if fila is None or fila[0] != rol_esperado:
        usuario = None if fila is None else fila[0]
        raise RecuperacionError(
            "La conexión efectiva no corresponde al rol de la empresa solicitante "
            f"({rol_esperado}); se obtuvo {usuario!r}. "
            "No se usa la conexión administrativa como alternativa."
        )


def _resultados_de_la_empresa(
    empresa_id: int,
    filas: list[tuple[object, ...]],
) -> list[ResultadoBusqueda]:
    """Interpreta el origen y aborta si cualquier fila es de otra empresa."""
    resultados: list[ResultadoBusqueda] = []
    for fila in filas:
        if len(fila) != 4:
            raise RecuperacionError(
                "La consulta devolvió una fila incompleta. No se devuelven resultados."
            )
        empresa_fila, documento, texto, similitud = fila
        if empresa_fila != empresa_id:
            raise RecuperacionError(
                "Se recuperó un fragmento de otra empresa "
                f"(solicitante={empresa_id}, propietario={empresa_fila}). "
                "No se devuelve ningún resultado."
            )
        documento_id, numero, titulo = _interpretar_documento(documento)
        if not isinstance(texto, str) or not texto.strip():
            raise RecuperacionError(
                "Un fragmento recuperado no tiene texto. No se devuelven resultados."
            )
        similitud_float = _similitud(similitud)
        resultados.append(
            ResultadoBusqueda(
                empresa_id=empresa_id,
                documento_id=documento_id,
                titulo=titulo,
                numero=numero,
                texto=texto,
                similitud=similitud_float,
            )
        )
    if any(resultado.empresa_id != empresa_id for resultado in resultados):
        raise RecuperacionError(
            "La verificación final encontró un fragmento de otra empresa. "
            "No se devuelve ningún resultado."
        )
    for anterior, siguiente in zip(resultados, resultados[1:], strict=False):
        if siguiente.similitud > anterior.similitud:
            raise RecuperacionError(
                "Los fragmentos no llegaron ordenados por similitud descendente."
            )
    return resultados


def _interpretar_documento(documento: object) -> tuple[str, int, str]:
    """Separa id, número y título según el formato de src/ingest.py."""
    if not isinstance(documento, str):
        raise RecuperacionError(
            "La columna documento no es texto. No se devuelven resultados."
        )
    partes = documento.split(SEPARADOR_DOCUMENTO)
    if len(partes) != 3:
        raise RecuperacionError(
            "La columna documento no sigue el formato id, número y título. "
            "No se devuelven resultados."
        )
    documento_id, numero_texto, titulo = partes
    if not documento_id or not titulo or not numero_texto.isascii() or not numero_texto.isdigit():
        raise RecuperacionError(
            "El origen almacenado en documento está incompleto. "
            "No se devuelven resultados."
        )
    numero = int(numero_texto)
    if numero < 1:
        raise RecuperacionError(
            "El número de fragmento almacenado no es válido. "
            "No se devuelven resultados."
        )
    return documento_id, numero, titulo


def _similitud(valor: object) -> float:
    """Convierte la similitud coseno sin aplicar un umbral de descarte."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float, Decimal)):
        raise RecuperacionError("La similitud devuelta por PostgreSQL no es numérica.")
    similitud = float(valor)
    if not math.isfinite(similitud):
        raise RecuperacionError("La similitud devuelta no es un valor finito.")
    return similitud


def _afirmar_aislamiento(empresa_id: int, resultados: list[ResultadoBusqueda]) -> None:
    """Falla la prueba si alguna fila no pertenece a la empresa solicitante."""
    assert all(resultado.empresa_id == empresa_id for resultado in resultados), (
        "Un fragmento recuperado pertenece a una empresa distinta de la solicitante."
    )
    assert all(resultado.numero >= 1 for resultado in resultados)
    assert all(resultado.documento_id and resultado.titulo and resultado.texto for resultado in resultados)


def _mostrar_busqueda(
    empresa_id: int,
    pregunta: str,
    resultados: list[ResultadoBusqueda],
) -> None:
    print()
    print(f"Empresa solicitante: {_etiqueta_empresa(empresa_id)}")
    print(f"Pregunta: {pregunta}")
    print(f"Número de fragmentos recuperados: {len(resultados)}")
    for indice, resultado in enumerate(resultados, start=1):
        print(f"Fragmento {indice}:")
        print(f"  Empresa propietaria: {_etiqueta_empresa(resultado.empresa_id)}")
        print(f"  Documento de origen: {resultado.documento_id} — {resultado.titulo}")
        print(f"  Número de fragmento: {resultado.numero}")
        print(f"  Similitud coseno: {resultado.similitud:.6f}")
        print("  Texto recuperado:")
        print(resultado.texto)


def _etiqueta_empresa(empresa_id: int) -> str:
    nombre = EMPRESAS_ESPERADAS.get(empresa_id)
    if nombre is None:
        return str(empresa_id)
    return f"{empresa_id} ({nombre})"


if __name__ == "__main__":
    main()
