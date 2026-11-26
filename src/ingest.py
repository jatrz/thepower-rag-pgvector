"""Ingesta idempotente del conjunto de datos multiempresa.

Reutiliza la fragmentación y los embeddings locales. Escribe solo con la
conexión administrativa y no implementa búsqueda ni Gemini.

La columna ``documento`` no tiene un campo aparte para el título. Se conserva
el origen con un formato reversible y sin ambigüedad:

    {documento_id}\\x1f{numero}\\x1f{titulo}

La idempotencia no borra la tabla. Dentro de una única transacción se
sustituyen exclusivamente los fragmentos cuyo ``empresa_id`` y
``documento_id`` pertenecen a este conjunto. Una segunda ejecución deja el
mismo número de fragmentos, sin duplicados. Las empresas ya registradas con
el mismo id y nombre se conservan; un id o un nombre en conflicto aborta la
transacción y no se sobrescribe.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import numpy as np
import psycopg
from pgvector import Vector

from src.chunking import Fragmento, cargar_empresas, fragmentar_empresas
from src.config import BASE_DATOS_ESPERADA, USUARIO_ADMIN, ids_empresa
from src.database import conexion_admin
from src.embeddings import EMBEDDING_DIM, generate_embeddings

# Contrato del conjunto ficticio. Debe coincidir con los JSON y con los roles.
EMPRESAS_ESPERADAS = MappingProxyType(
    {
        1: "Hornos Alba",
        2: "Clínica Sonrisa",
        3: "Ruta Norte",
    }
)

# Separador que no aparece en los identificadores ni en los títulos validados.
SEPARADOR_DOCUMENTO = "\x1f"


class IngestaError(RuntimeError):
    """El conjunto de datos o la verificación impiden confirmar la ingesta."""


@dataclass(frozen=True, slots=True)
class FilaFragmento:
    """Fragmento ya validado y listo para insertarse, sin acceso a la base."""

    empresa_id: int
    documento_id: str
    documento: str
    texto: str
    embedding: Vector


@dataclass(frozen=True, slots=True)
class ConjuntoPreparado:
    """Resultado en memoria, calculado antes de abrir la transacción."""

    documentos: int
    filas: tuple[FilaFragmento, ...]
    pares: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True)
class ResumenIngesta:
    """Cifras verificadas dentro de la transacción, antes de confirmarla."""

    empresas: int
    documentos: int
    embeddings: int
    dimensiones: int
    fragmentos_por_empresa: tuple[tuple[int, str, int], ...]
    total_fragmentos: int


@dataclass(slots=True)
class EstadoTransaccion:
    """Estado observable por main sin imprimir datos de conexión."""

    resultado: str = "no iniciada"


def ingerir(estado: EstadoTransaccion | None = None) -> ResumenIngesta:
    """Prepara el conjunto y lo persiste en una sola transacción administrativa.

    Los embeddings se generan antes de conectar. Así la transacción no permanece
    abierta durante la inferencia y un vector inválido no modifica la base.
    """
    if estado is None:
        estado = EstadoTransaccion()
    preparado = preparar_conjunto()
    with conexion_admin() as conexion:
        estado.resultado = "abierta"
        resumen = _persistir(conexion, preparado)
    estado.resultado = "confirmada"
    return resumen


def preparar_conjunto() -> ConjuntoPreparado:
    """Valida documentos, fragmentos y embeddings sin modificar PostgreSQL."""
    empresas = cargar_empresas()
    fragmentos = fragmentar_empresas(empresas)
    _validar_conjunto(empresas, fragmentos)
    textos = [fragmento.texto for fragmento in fragmentos]
    vectores = generate_embeddings(textos)
    _validar_embeddings(vectores, len(fragmentos))
    filas = tuple(
        FilaFragmento(
            empresa_id=fragmento.empresa_id,
            documento_id=fragmento.documento_id,
            documento=_valor_documento(fragmento),
            texto=fragmento.texto,
            embedding=Vector(np.ascontiguousarray(vector, dtype=np.float32)),
        )
        for fragmento, vector in zip(fragmentos, vectores, strict=True)
    )
    pares = tuple(
        sorted({(fila.empresa_id, fila.documento_id) for fila in filas})
    )
    documentos = sum(len(empresa["documentos"]) for empresa in empresas)
    if len(pares) != documentos or len(filas) != len(fragmentos):
        raise IngestaError(
            "Los documentos preparados no coinciden con los fragmentos validados."
        )
    return ConjuntoPreparado(
        documentos=documentos,
        filas=filas,
        pares=pares,
    )


def main() -> None:
    """Ejecuta la ingesta y muestra el resumen solo si la transacción se confirma."""
    inicio = time.perf_counter()
    estado = EstadoTransaccion()
    try:
        resumen = ingerir(estado)
    except Exception as exc:
        if estado.resultado == "abierta":
            estado.resultado = "revertida"
        duracion = time.perf_counter() - inicio
        print(f"Resultado de la transacción: {estado.resultado}")
        print(f"Tiempo total de ejecución: {duracion:.3f} s")
        print(f"ERROR: {_mensaje_sin_conexion(exc)}", file=sys.stderr)
        raise SystemExit(1) from None

    duracion = time.perf_counter() - inicio
    _mostrar_resumen(resumen, estado.resultado, duracion)


def _validar_conjunto(empresas: list[dict[str, Any]], fragmentos: list[Fragmento]) -> None:
    """Comprueba el contrato de empresas, documentos y fragmentos."""
    if tuple(ids_empresa()) != tuple(EMPRESAS_ESPERADAS):
        raise IngestaError(
            "Los identificadores de empresa de la configuración no coinciden "
            "con el conjunto de datos."
        )
    if not empresas or not fragmentos:
        raise IngestaError("El conjunto de datos no contiene fragmentos.")

    observadas = {
        int(empresa["empresa_id"]): str(empresa["nombre"]) for empresa in empresas
    }
    if observadas != dict(EMPRESAS_ESPERADAS):
        raise IngestaError(
            "Las empresas de data/ no coinciden con Hornos Alba, "
            "Clínica Sonrisa y Ruta Norte."
        )

    documentos: set[tuple[int, str]] = set()
    for empresa in empresas:
        empresa_id = int(empresa["empresa_id"])
        vistos: set[str] = set()
        for documento in empresa["documentos"]:
            documento_id = str(documento["id"])
            titulo = str(documento["titulo"])
            _validar_origen(documento_id, titulo)
            if documento_id in vistos:
                raise IngestaError(f"Documento duplicado: {documento_id}.")
            vistos.add(documento_id)
            documentos.add((empresa_id, documento_id))
        if not vistos:
            raise IngestaError(f"La empresa {empresa_id} no tiene documentos.")

    numeros: dict[tuple[int, str], list[int]] = {}
    for fragmento in fragmentos:
        if fragmento.nombre_empresa != EMPRESAS_ESPERADAS.get(fragmento.empresa_id):
            raise IngestaError(
                f"Fragmento de una empresa no esperada: {fragmento.empresa_id}."
            )
        origen = (fragmento.empresa_id, fragmento.documento_id)
        if origen not in documentos:
            raise IngestaError(
                "Hay un fragmento cuyo documento no pertenece al conjunto cargado."
            )
        if not fragmento.texto.strip():
            raise IngestaError(
                f"El documento {fragmento.documento_id} tiene un fragmento vacío."
            )
        if fragmento.numero < 1:
            raise IngestaError(
                f"Número de fragmento no válido en {fragmento.documento_id}."
            )
        numeros.setdefault(origen, []).append(fragmento.numero)

    if set(numeros) != documentos:
        raise IngestaError("Algún documento del conjunto no produjo fragmentos.")
    for (empresa_id, documento_id), serie in numeros.items():
        if sorted(serie) != list(range(1, len(serie) + 1)):
            raise IngestaError(
                f"Los fragmentos de la empresa {empresa_id}, documento "
                f"{documento_id}, no forman una secuencia continua."
            )


def _validar_origen(documento_id: str, titulo: str) -> None:
    """Impide que el separador vuelva ambiguo el valor de la columna documento."""
    if not documento_id or not titulo:
        raise IngestaError("El identificador o el título del documento está vacío.")
    if SEPARADOR_DOCUMENTO in documento_id or SEPARADOR_DOCUMENTO in titulo:
        raise IngestaError(
            f"El documento {documento_id!r} contiene el separador reservado."
        )
    if "\n" in documento_id or "\r" in documento_id:
        raise IngestaError(
            f"El identificador de documento {documento_id!r} ocupa más de una línea."
        )


def _validar_embeddings(vectores: np.ndarray, cantidad: int) -> None:
    """Rechaza un lote que no pueda almacenarse en VECTOR(384)."""
    if vectores.shape != (cantidad, EMBEDDING_DIM):
        raise IngestaError(
            f"Se esperaban embeddings ({cantidad}, {EMBEDDING_DIM}); "
            f"se obtuvieron {vectores.shape}."
        )
    if vectores.dtype != np.float32:
        raise IngestaError(f"Los embeddings deben ser float32, no {vectores.dtype}.")
    if not np.isfinite(vectores).all():
        raise IngestaError("Hay embeddings con valores no finitos.")
    normas = np.linalg.norm(vectores, axis=1)
    if not np.allclose(normas, 1.0, rtol=0.0, atol=1e-5):
        raise IngestaError("Hay embeddings que no están normalizados.")


def _valor_documento(fragmento: Fragmento) -> str:
    """Codifica id, número y título en la única columna de origen disponible."""
    _validar_origen(fragmento.documento_id, fragmento.titulo)
    return (
        f"{fragmento.documento_id}{SEPARADOR_DOCUMENTO}"
        f"{fragmento.numero}{SEPARADOR_DOCUMENTO}"
        f"{fragmento.titulo}"
    )


def _persistir(
    conexion: psycopg.Connection[tuple[Any, ...]],
    preparado: ConjuntoPreparado,
) -> ResumenIngesta:
    """Escribe y verifica. No confirma: el context manager lo hace si no hay error."""
    with conexion.cursor() as cursor:
        _exigir_rol_administrativo(cursor)
        _registrar_empresas(cursor)
        ajenos_antes = _contar_fragmentos_ajenos(cursor, preparado.pares)
        _sustituir_fragmentos_del_conjunto(cursor, preparado)
        resumen = _verificar_antes_de_confirmar(cursor, preparado, ajenos_antes)
    return resumen


def _exigir_rol_administrativo(cursor: psycopg.Cursor[tuple[Any, ...]]) -> None:
    """Impide ingerir con un rol de empresa o sin capacidad real de escritura."""
    cursor.execute(
        """
        SELECT current_user,
               current_database(),
               (
                   SELECT rolsuper OR rolbypassrls
                   FROM pg_roles
                   WHERE rolname = current_user
               )
        """
    )
    fila = cursor.fetchone()
    if fila is None:
        raise IngestaError("No se pudo identificar el rol de la conexión.")
    usuario, base_datos, puede_escribir = fila
    if usuario != USUARIO_ADMIN or base_datos != BASE_DATOS_ESPERADA:
        raise IngestaError(
            "La ingesta solo puede ejecutarse con el rol administrativo "
            "sobre la base de datos prevista."
        )
    if puede_escribir is not True:
        raise IngestaError(
            "El rol administrativo no puede escribir con RLS forzado. "
            "No se utiliza un rol de empresa como alternativa."
        )


def _registrar_empresas(cursor: psycopg.Cursor[tuple[Any, ...]]) -> None:
    """Inserta las empresas que faltan y rechaza cualquier conflicto id/nombre."""
    ids = list(EMPRESAS_ESPERADAS)
    nombres = list(EMPRESAS_ESPERADAS.values())
    cursor.execute(
        """
        SELECT id, nombre
        FROM empresas
        WHERE id = ANY(%s) OR nombre = ANY(%s)
        FOR UPDATE
        """,
        (ids, nombres),
    )
    existentes = cursor.fetchall()
    por_id = {int(empresa_id): str(nombre) for empresa_id, nombre in existentes}
    por_nombre = {str(nombre): int(empresa_id) for empresa_id, nombre in existentes}
    if len(por_id) != len(existentes) or len(por_nombre) != len(existentes):
        raise IngestaError(
            "La tabla empresas contiene identificadores o nombres duplicados "
            "en el ámbito del conjunto. No se sobrescribe."
        )

    pendientes: list[tuple[int, str]] = []
    for empresa_id, nombre in EMPRESAS_ESPERADAS.items():
        nombre_existente = por_id.get(empresa_id)
        if nombre_existente is not None and nombre_existente != nombre:
            raise IngestaError(
                f"Conflicto: el id {empresa_id} ya pertenece a otro nombre. "
                "No se sobrescribe."
            )
        id_del_nombre = por_nombre.get(nombre)
        if id_del_nombre is not None and id_del_nombre != empresa_id:
            raise IngestaError(
                f"Conflicto: el nombre {nombre!r} ya pertenece a otro id. "
                "No se sobrescribe."
            )
        if nombre_existente is None:
            pendientes.append((empresa_id, nombre))

    if pendientes:
        cursor.executemany(
            "INSERT INTO empresas (id, nombre) VALUES (%s, %s)",
            pendientes,
        )
    # setval no se revierte en PostgreSQL. Adelantar la secuencia es seguro;
    # dejarla por detrás de los id explícitos provocaría un conflicto posterior.
    cursor.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('empresas', 'id'),
            (SELECT MAX(id) FROM empresas)
        )
        """
    )
    if cursor.fetchone() is None:
        raise IngestaError("No se pudo alinear la secuencia de empresas.")

    cursor.execute(
        """
        SELECT id, nombre
        FROM empresas
        WHERE id = ANY(%s)
        ORDER BY id
        """,
        (ids,),
    )
    registradas = [(int(empresa_id), str(nombre)) for empresa_id, nombre in cursor.fetchall()]
    if registradas != list(EMPRESAS_ESPERADAS.items()):
        raise IngestaError(
            "Las empresas resultantes no coinciden con el conjunto. "
            "La transacción no se confirmará."
        )


def _sustituir_fragmentos_del_conjunto(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    preparado: ConjuntoPreparado,
) -> None:
    """Borra y vuelve a insertar solo los documentos de este conjunto."""
    if not preparado.pares or not preparado.filas:
        raise IngestaError("No hay documentos concretos que sustituir.")
    marcadores, parametros = _marcadores_pares(preparado.pares)
    condicion = f"""
        FROM fragmentos AS f
        JOIN (VALUES {marcadores}) AS esperado(empresa_id, documento_id)
          ON f.empresa_id = esperado.empresa_id
         AND split_part(f.documento, %s, 1) = esperado.documento_id
    """
    argumentos = [*parametros, SEPARADOR_DOCUMENTO]
    cursor.execute(f"SELECT f.id {condicion} FOR UPDATE OF f", argumentos)
    cursor.fetchall()
    cursor.execute(
        f"""
        DELETE FROM fragmentos AS f
        USING (VALUES {marcadores}) AS esperado(empresa_id, documento_id)
        WHERE f.empresa_id = esperado.empresa_id
          AND split_part(f.documento, %s, 1) = esperado.documento_id
        """,
        argumentos,
    )
    cursor.executemany(
        """
        INSERT INTO fragmentos (empresa_id, documento, texto, embedding)
        VALUES (%s, %s, %s, %s)
        """,
        [
            (fila.empresa_id, fila.documento, fila.texto, fila.embedding)
            for fila in preparado.filas
        ],
    )


def _verificar_antes_de_confirmar(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    preparado: ConjuntoPreparado,
    ajenos_antes: int,
) -> ResumenIngesta:
    """Comprueba recuentos, dimensiones y que lo ajeno al conjunto sigue igual."""
    if _contar_fragmentos_ajenos(cursor, preparado.pares) != ajenos_antes:
        raise IngestaError(
            "La ingesta afectó a fragmentos de empresas o documentos ajenos. "
            "La transacción no se confirmará."
        )

    marcadores, parametros = _marcadores_pares(preparado.pares)
    cursor.execute(
        f"""
        SELECT f.empresa_id, f.documento, f.texto, vector_dims(f.embedding)
        FROM fragmentos AS f
        JOIN (VALUES {marcadores}) AS esperado(empresa_id, documento_id)
          ON f.empresa_id = esperado.empresa_id
         AND split_part(f.documento, %s, 1) = esperado.documento_id
        """,
        [*parametros, SEPARADOR_DOCUMENTO],
    )
    almacenados = cursor.fetchall()
    esperados = Counter(
        (fila.empresa_id, fila.documento, fila.texto) for fila in preparado.filas
    )
    observados = Counter(
        (int(empresa_id), str(documento), str(texto))
        for empresa_id, documento, texto, _dimension in almacenados
    )
    if observados != esperados:
        raise IngestaError(
            "Los fragmentos almacenados no coinciden con el conjunto preparado. "
            "La transacción no se confirmará."
        )
    if any(dimension != EMBEDDING_DIM for *_resto, dimension in almacenados):
        raise IngestaError(
            f"Hay embeddings almacenados que no tienen {EMBEDDING_DIM} dimensiones."
        )

    por_empresa = {empresa_id: 0 for empresa_id in EMPRESAS_ESPERADAS}
    for (empresa_id, _documento, _texto), cantidad in observados.items():
        if empresa_id not in por_empresa:
            raise IngestaError("Se almacenó un fragmento de una empresa ajena.")
        por_empresa[empresa_id] += cantidad
    if any(cantidad < 1 for cantidad in por_empresa.values()):
        raise IngestaError(
            "Alguna empresa del conjunto no tiene fragmentos almacenados."
        )

    total = sum(por_empresa.values())
    if total != len(preparado.filas) or len(preparado.pares) != preparado.documentos:
        raise IngestaError(
            "El número de fragmentos verificados no coincide con los embeddings."
        )
    return ResumenIngesta(
        empresas=len(EMPRESAS_ESPERADAS),
        documentos=preparado.documentos,
        embeddings=len(preparado.filas),
        dimensiones=EMBEDDING_DIM,
        fragmentos_por_empresa=tuple(
            (empresa_id, EMPRESAS_ESPERADAS[empresa_id], por_empresa[empresa_id])
            for empresa_id in EMPRESAS_ESPERADAS
        ),
        total_fragmentos=total,
    )


def _contar_fragmentos_ajenos(
    cursor: psycopg.Cursor[tuple[Any, ...]],
    pares: tuple[tuple[int, str], ...],
) -> int:
    """Cuenta fragmentos que esta ingesta no está autorizada a sustituir."""
    marcadores, parametros = _marcadores_pares(pares)
    cursor.execute(
        f"""
        SELECT COUNT(*)
        FROM fragmentos AS f
        WHERE NOT EXISTS (
            SELECT 1
            FROM (VALUES {marcadores}) AS esperado(empresa_id, documento_id)
            WHERE f.empresa_id = esperado.empresa_id
              AND split_part(f.documento, %s, 1) = esperado.documento_id
        )
        """,
        [*parametros, SEPARADOR_DOCUMENTO],
    )
    fila = cursor.fetchone()
    if fila is None:
        raise IngestaError("No se pudo contar los fragmentos ajenos al conjunto.")
    return int(fila[0])


def _marcadores_pares(pares: tuple[tuple[int, str], ...]) -> tuple[str, list[Any]]:
    """Construye VALUES parametrizados. Los valores nunca se interpolan en SQL."""
    if not pares:
        raise IngestaError("La sustitución exige al menos un documento concreto.")
    marcadores = ", ".join(["(%s::integer, %s::text)"] * len(pares))
    parametros: list[Any] = []
    for empresa_id, documento_id in pares:
        parametros.extend((empresa_id, documento_id))
    return marcadores, parametros


def _mostrar_resumen(
    resumen: ResumenIngesta,
    transaccion: str,
    duracion: float,
) -> None:
    print(f"Número de empresas procesadas: {resumen.empresas}")
    print(f"Número de documentos procesados: {resumen.documentos}")
    print(f"Número de embeddings generados: {resumen.embeddings}")
    print(f"Dimensiones de los embeddings: {resumen.dimensiones}")
    print("Número de fragmentos almacenados por empresa:")
    for empresa_id, nombre, cantidad in resumen.fragmentos_por_empresa:
        print(f"  {empresa_id} ({nombre}): {cantidad}")
    print(
        "Número total de fragmentos almacenados del conjunto de datos: "
        f"{resumen.total_fragmentos}"
    )
    print(f"Resultado de la transacción: {transaccion}")
    print(f"Tiempo total de ejecución: {duracion:.3f} s")


def _mensaje_sin_conexion(exc: BaseException) -> str:
    """Resume el fallo sin representar la conexión ni sus credenciales."""
    if isinstance(exc, IngestaError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


if __name__ == "__main__":
    main()
