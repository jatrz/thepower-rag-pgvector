"""Conexiones PostgreSQL para administración e empresas.

La conexión administrativa queda reservada a la futura ingesta. Cada empresa
se conecta solo con su rol PostgreSQL. Si esa conexión falla, no se reintenta
con las credenciales de rag_admin: hacerlo anularía el aislamiento de RLS.

Importante para producción: empresa_id debe proceder de una identidad
autenticada por el servidor. No debe tomarse de la pregunta del usuario ni de
una respuesta del LLM, porque cualquiera de esas fuentes puede ser manipulada
para solicitar datos de otra empresa.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.adapt import PyFormat

from src.config import (
    ConfiguracionAdmin,
    ConfiguracionEmpresa,
    ConfiguracionError,
    configuracion_admin,
    configuracion_empresa,
    ids_empresa,
    rol_de_empresa,
)

APLICACION = "thepower-rag-pgvector"


class ErrorConexion(RuntimeError):
    """Fallo de conexión o de registro del tipo vector, sin secretos."""


class ErrorAislamiento(RuntimeError):
    """El usuario efectivo no coincide con el rol exigido por la política RLS."""


def conectar_admin() -> psycopg.Connection[tuple[Any, ...]]:
    """Abre una conexión administrativa reservada para la futura ingesta.

    No debe usarse para responder consultas de una empresa. El llamador es
    responsable de cerrarla, o puede usar :func:`conexion_admin`.
    """
    config = configuracion_admin()
    return _abrir(
        host=config.host,
        puerto=config.puerto,
        base_datos=config.base_datos,
        usuario=config.usuario,
        contrasena=config.contrasena,
        aplicacion=f"{APLICACION}-admin",
    )


def conectar_empresa(empresa_id: int) -> psycopg.Connection[tuple[Any, ...]]:
    """Abre una conexión con el rol exclusivo de la empresa indicada.

    ``empresa_id`` debe proceder de una identidad autenticada por el servidor,
    nunca de la pregunta del usuario ni del modelo de lenguaje. Solo se emplean
    las credenciales de ese rol. Un error de autenticación o de red no provoca
    una conexión alternativa con el administrador.
    """
    config = configuracion_empresa(empresa_id)
    return _abrir(
        host=config.host,
        puerto=config.puerto,
        base_datos=config.base_datos,
        usuario=config.usuario,
        contrasena=config.contrasena,
        aplicacion=f"{APLICACION}-empresa-{config.empresa_id}",
    )


@contextmanager
def conexion_admin() -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    """Context manager administrativo: confirma, revierte y cierra la conexión."""
    conexion = conectar_admin()
    try:
        yield conexion
    except Exception:
        _revertir(conexion)
        raise
    else:
        _confirmar(conexion)
    finally:
        _cerrar(conexion)


@contextmanager
def conexion_empresa(
    empresa_id: int,
) -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    """Context manager de empresa: confirma, revierte y cierra la conexión.

    La transacción terminada con éxito solo puede contener lecturas autorizadas
    por RLS. Este módulo no inserta, actualiza ni elimina registros.
    """
    conexion = conectar_empresa(empresa_id)
    try:
        yield conexion
    except Exception:
        _revertir(conexion)
        raise
    else:
        _confirmar(conexion)
    finally:
        _cerrar(conexion)


def vector_registrado(conexion: psycopg.Connection[Any]) -> bool:
    """Comprueba que pgvector está registrado en esta conexión y en el catálogo.

    La comprobación es de solo lectura: adapta un vector literal y lo recupera,
    sin escribir en ninguna tabla.
    """
    # %s usa PyFormat.AUTO, que es el formato con el que register_vector
    # deja disponible el adaptador de Vector.
    try:
        conexion.adapters.get_dumper(Vector, PyFormat.AUTO)
    except Exception:
        return False

    origen = Vector([0.25, -0.5])
    with conexion.cursor() as cursor:
        cursor.execute(
            "SELECT typname FROM pg_type WHERE typname = %s",
            ("vector",),
        )
        tipo = cursor.fetchone()
        # El parámetro usa el adaptador registrado; no se concatena SQL.
        cursor.execute("SELECT %s", (origen,))
        devuelto = cursor.fetchone()
    valor = devuelto[0] if devuelto is not None else None
    if tipo is None or tipo[0] != "vector" or not isinstance(valor, Vector):
        return False
    if valor.dimensions() != origen.dimensions():
        return False
    return all(
        abs(recibido - esperado) < 1e-6
        for recibido, esperado in zip(
            valor.to_list(), origen.to_list(), strict=True
        )
    )


def main() -> None:
    """Prueba independiente de los cuatro roles, sin modificar datos."""
    print("Prueba de conexiones y aislamiento de roles")
    print(f"Empresas admitidas: {', '.join(str(valor) for valor in ids_empresa())}")
    for empresa_id in ids_empresa():
        print(
            f"Correspondencia: empresa_id={empresa_id} -> "
            f"rol={rol_de_empresa(empresa_id)}"
        )

    admin = configuracion_admin()
    _probar_admin(admin)
    for empresa_id in ids_empresa():
        _probar_empresa(configuracion_empresa(empresa_id))
    print("Resultado: las cuatro conexiones usan el rol esperado.")


def _probar_admin(config: ConfiguracionAdmin) -> None:
    with conexion_admin() as conexion:
        _verificar_y_mostrar(
            conexion,
            usuario_esperado=config.usuario,
            etiqueta="Administración",
            detalle="reservada para la futura ingesta; no sustituye a una empresa",
        )


def _probar_empresa(config: ConfiguracionEmpresa) -> None:
    # Nueva conexión, independiente de la administrativa y de las otras empresas.
    with conexion_empresa(config.empresa_id) as conexion:
        _verificar_y_mostrar(
            conexion,
            usuario_esperado=config.rol_esperado,
            etiqueta=f"Empresa {config.empresa_id} ({config.nombre})",
            detalle=f"rol exclusivo {config.rol_esperado}",
        )


def _verificar_y_mostrar(
    conexion: psycopg.Connection[tuple[Any, ...]],
    usuario_esperado: str,
    etiqueta: str,
    detalle: str,
) -> None:
    with conexion.cursor() as cursor:
        cursor.execute("SELECT current_user, current_database()")
        fila = cursor.fetchone()
    if fila is None:
        raise ErrorAislamiento(
            f"{etiqueta}: PostgreSQL no devolvió el usuario efectivo."
        )
    usuario, base_datos = fila
    if usuario != usuario_esperado:
        raise ErrorAislamiento(
            f"{etiqueta}: usuario efectivo {usuario!r} distinto del rol "
            f"esperado {usuario_esperado!r}."
        )
    if not vector_registrado(conexion):
        raise ErrorConexion(
            f"{etiqueta}: el tipo vector no quedó registrado en la conexión."
        )
    estado = "cerrada" if conexion.closed else "abierta"
    print(
        f"{etiqueta}: usuario={usuario}; base_datos={base_datos}; "
        f"estado={estado}; {detalle}; tipo vector registrado=sí"
    )


def _abrir(
    *,
    host: str,
    puerto: int,
    base_datos: str,
    usuario: str,
    contrasena: str,
    aplicacion: str,
) -> psycopg.Connection[tuple[Any, ...]]:
    try:
        # Parámetros separados, nunca una cadena DSN que pudiera registrarse.
        conexion = psycopg.connect(
            host=host,
            port=puerto,
            dbname=base_datos,
            user=usuario,
            password=contrasena,
            application_name=aplicacion,
            connect_timeout=10,
        )
    except Exception as exc:
        raise ErrorConexion(
            _ocultar_secreto(
                f"No se pudo conectar como {usuario!r} a {base_datos!r} "
                f"en {host}:{puerto}. {_detalle_sin_secreto(exc, contrasena)}",
                contrasena,
            )
        ) from None
    try:
        register_vector(conexion)
    except Exception as exc:
        _cerrar(conexion)
        raise ErrorConexion(
            _ocultar_secreto(
                f"No se pudo registrar el tipo vector para {usuario!r}. "
                f"{_detalle_sin_secreto(exc, contrasena)}",
                contrasena,
            )
        ) from None
    return conexion


def _detalle_sin_secreto(exc: BaseException, secreto: str) -> str:
    return _ocultar_secreto(f"{type(exc).__name__}: {exc}", secreto)


def _ocultar_secreto(mensaje: str, secreto: str) -> str:
    if secreto:
        return mensaje.replace(secreto, "***")
    return mensaje


def _confirmar(conexion: psycopg.Connection[Any]) -> None:
    if not conexion.closed:
        conexion.commit()


def _revertir(conexion: psycopg.Connection[Any]) -> None:
    if conexion.closed:
        return
    try:
        conexion.rollback()
    except Exception:
        # La conexión ya no admite revertir; el cierre posterior libera la sesión.
        return


def _cerrar(conexion: psycopg.Connection[Any]) -> None:
    if not conexion.closed:
        conexion.close()


if __name__ == "__main__":
    try:
        main()
    except (ConfiguracionError, ErrorConexion, ErrorAislamiento) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(
            f"ERROR: fallo inesperado ({type(exc).__name__}). "
            "No se muestran detalles para evitar revelar secretos.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
