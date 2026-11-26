"""Pruebas de aislamiento RLS y de privilegios de los tres roles de empresa.

Usan la base de datos local ya preparada y solo las conexiones de empresa.
No abren el rol administrador, no leen .env y no confirman ninguna escritura.
Las sentencias de modificación se revierten siempre, incluso si alguna fuera
aceptada por error.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

_RAIZ = Path(__file__).resolve().parents[1]
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from src.config import (  # noqa: E402
    BASE_DATOS_ESPERADA,
    ConfiguracionError,
    ROLES_POR_EMPRESA,
    ids_empresa,
    rol_de_empresa,
)
from src.database import conectar_empresa, conexion_empresa  # noqa: E402

EMPRESAS = tuple(ids_empresa())
FRAGMENTOS_PROPIOS = 12
PRIVILEGIOS_DENEGADOS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "TRIGGER")
ROLES_AJENOS = (
    (1, "rag_empresa_2"),
    (1, "rag_empresa_3"),
    (1, "rag_admin"),
    (2, "rag_empresa_1"),
    (2, "rag_empresa_3"),
    (2, "rag_admin"),
    (3, "rag_empresa_1"),
    (3, "rag_empresa_2"),
    (3, "rag_admin"),
)
CONSULTAS_AJENAS = tuple(
    (solicitante, ajena)
    for solicitante in EMPRESAS
    for ajena in EMPRESAS
    if solicitante != ajena
)
IDENTIFICADORES_DESCONOCIDOS = (
    0,
    4,
    -1,
    99,
    True,
    False,
    None,
    "1",
    1.0,
    2.5,
)
# Sentencias que no seleccionan filas. TRUNCATE no admite ese filtro; si el
# privilegio fallara, la transacción se revierte antes de salir del contexto.
SENTENCIAS_ESCRITURA = {
    "INSERT": (
        "INSERT INTO fragmentos (empresa_id, documento, texto, embedding) "
        "SELECT empresa_id, documento, texto, embedding "
        "FROM fragmentos WHERE false"
    ),
    "UPDATE": "UPDATE fragmentos SET texto = texto WHERE false",
    "DELETE": "DELETE FROM fragmentos WHERE false",
    "TRUNCATE": "TRUNCATE TABLE fragmentos",
}


def test_constantes_de_aislamiento() -> None:
    """Fija el contrato de tres empresas antes de consultar PostgreSQL."""
    assert EMPRESAS == (1, 2, 3)
    assert ROLES_POR_EMPRESA[1] == "rag_empresa_1"
    assert ROLES_POR_EMPRESA[2] == "rag_empresa_2"
    assert ROLES_POR_EMPRESA[3] == "rag_empresa_3"


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_usuario_efectivo_coincide_con_el_rol(empresa_id: int) -> None:
    """current_user debe ser el rol RLS de esa empresa, nunca el administrador."""
    usuario, base_datos = _fila(
        empresa_id,
        "SELECT current_user, current_database()",
    )
    assert usuario == rol_de_empresa(empresa_id)
    assert usuario != "rag_admin"
    assert base_datos == BASE_DATOS_ESPERADA


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_ve_exactamente_sus_doce_fragmentos(empresa_id: int) -> None:
    """Sin WHERE, RLS debe dejar solo los 12 fragmentos de la empresa conectada."""
    total, ajenos, empresas_visibles = _fila(
        empresa_id,
        """
        SELECT
            COUNT(*)::int,
            COUNT(*) FILTER (WHERE empresa_id <> %s)::int,
            COUNT(DISTINCT empresa_id)::int
        FROM fragmentos
        """,
        (empresa_id,),
    )
    assert total == FRAGMENTOS_PROPIOS
    assert ajenos == 0
    assert empresas_visibles == 1


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_no_ve_documentos_ajenos_sin_where(empresa_id: int) -> None:
    """Los prefijos documentales de otras empresas no aparecen ni sin filtro."""
    for ajena in EMPRESAS:
        if ajena == empresa_id:
            continue
        prefijo = {1: "ha-", 2: "cs-", 3: "rn-"}[ajena]
        visibles = _fila(
            empresa_id,
            "SELECT COUNT(*)::int FROM fragmentos WHERE documento LIKE %s",
            (f"{prefijo}%",),
        )[0]
        assert visibles == 0


@pytest.mark.parametrize(("solicitante", "ajena"), CONSULTAS_AJENAS)
def test_consulta_explicita_a_otra_empresa_devuelve_cero(
    solicitante: int,
    ajena: int,
) -> None:
    """Un WHERE hacia otra empresa no abre una excepción de la política RLS."""
    visibles = _fila(
        solicitante,
        "SELECT COUNT(*)::int FROM fragmentos WHERE empresa_id = %s",
        (ajena,),
    )[0]
    assert visibles == 0


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_rls_esta_habilitado_y_forzado(empresa_id: int) -> None:
    """Cada rol debe observar RLS activo y forzado en fragmentos."""
    habilitado, forzado = _fila(
        empresa_id,
        """
        SELECT relrowsecurity, relforcerowsecurity
        FROM pg_class
        WHERE oid = 'public.fragmentos'::regclass
        """,
    )
    assert habilitado is True
    assert forzado is True


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_rol_no_es_superusuario_ni_omite_rls(empresa_id: int) -> None:
    """El rol efectivo no puede saltarse RLS ni heredar privilegios ajenos."""
    superusuario, omite_rls, hereda, crea_roles, crea_bases = _fila(
        empresa_id,
        """
        SELECT rolsuper, rolbypassrls, rolinherit, rolcreaterole, rolcreatedb
        FROM pg_roles
        WHERE rolname = current_user
        """,
    )
    assert superusuario is False
    assert omite_rls is False
    assert hereda is False
    assert crea_roles is False
    assert crea_bases is False


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_rol_no_tiene_membresias(empresa_id: int) -> None:
    """Sin membresías no puede heredar ni adoptar otro rol."""
    membresias = _fila(
        empresa_id,
        """
        SELECT COUNT(*)::int
        FROM pg_auth_members AS membresia
        JOIN pg_roles AS miembro ON miembro.oid = membresia.member
        WHERE miembro.rolname = current_user
        """,
    )[0]
    assert membresias == 0


@pytest.mark.parametrize(("empresa_id", "rol_ajeno"), ROLES_AJENOS)
def test_no_puede_adoptar_otro_rol(empresa_id: int, rol_ajeno: str) -> None:
    """SET ROLE hacia otra empresa o el administrador debe ser denegado."""
    assert rol_ajeno in {"rag_admin", "rag_empresa_1", "rag_empresa_2", "rag_empresa_3"}
    with conexion_empresa(empresa_id) as conexion:
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conexion.cursor() as cursor:
                    cursor.execute(
                        psycopg.sql.SQL("SET ROLE {}").format(
                            psycopg.sql.Identifier(rol_ajeno)
                        )
                    )
        finally:
            # Revierte también un SET ROLE que hubiera sido aceptado por error.
            conexion.rollback()


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_select_habilitado_y_es_el_unico_privilegio(empresa_id: int) -> None:
    """El rol solo puede leer fragmentos; no conserva concesiones amplias."""
    puede_leer = _fila(
        empresa_id,
        "SELECT has_table_privilege(current_user, 'public.fragmentos', 'SELECT')",
    )[0]
    privilegios = _filas(
        empresa_id,
        """
        SELECT privilege_type
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'fragmentos'
          AND grantee = current_user
        """,
    )
    assert puede_leer is True
    assert {fila[0] for fila in privilegios} == {"SELECT"}


@pytest.mark.parametrize("empresa_id", EMPRESAS)
@pytest.mark.parametrize("privilegio", PRIVILEGIOS_DENEGADOS)
def test_privilegio_de_escritura_denegado(empresa_id: int, privilegio: str) -> None:
    """INSERT, UPDATE, DELETE, TRUNCATE y TRIGGER no están concedidos."""
    concedido = _fila(
        empresa_id,
        "SELECT has_table_privilege(current_user, 'public.fragmentos', %s)",
        (privilegio,),
    )[0]
    assert concedido is False


@pytest.mark.parametrize("empresa_id", EMPRESAS)
@pytest.mark.parametrize("privilegio", tuple(SENTENCIAS_ESCRITURA))
def test_sentencia_de_escritura_es_rechazada(empresa_id: int, privilegio: str) -> None:
    """La ejecución se rechaza y la transacción se revierte sin confirmar datos."""
    with conexion_empresa(empresa_id) as conexion:
        denegada = False
        try:
            with conexion.cursor() as cursor:
                cursor.execute(SENTENCIAS_ESCRITURA[privilegio])
        except psycopg.errors.InsufficientPrivilege:
            denegada = True
        finally:
            conexion.rollback()
        assert denegada, f"{privilegio} no fue denegado para la empresa {empresa_id}"


@pytest.mark.parametrize("empresa_id", IDENTIFICADORES_DESCONOCIDOS)
def test_rechaza_identificador_desconocido(
    monkeypatch: pytest.MonkeyPatch,
    empresa_id: object,
) -> None:
    """Un identificador ajeno al mapa no abre ninguna conexión."""

    def _conectar_prohibido(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "Un identificador desconocido no debe intentar abrir una conexión."
        )

    monkeypatch.setattr("src.database.psycopg.connect", _conectar_prohibido)
    with pytest.raises(ConfiguracionError):
        rol_de_empresa(empresa_id)  # type: ignore[arg-type]
    with pytest.raises(ConfiguracionError):
        conectar_empresa(empresa_id)  # type: ignore[arg-type]


def _fila(empresa_id: int, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    filas = _filas(empresa_id, sql, params)
    assert len(filas) == 1
    return filas[0]


def _filas(
    empresa_id: int,
    sql: str,
    params: tuple[object, ...] = (),
) -> list[tuple[object, ...]]:
    """Ejecuta una consulta de solo lectura con el rol exclusivo de la empresa."""
    with conexion_empresa(empresa_id) as conexion:
        conexion.rollback()
        with conexion.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(sql, params)
            return list(cursor.fetchall())
