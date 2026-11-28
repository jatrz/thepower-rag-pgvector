"""Pruebas aisladas de la configuración de la instancia chatbot.

No leen `.env`, `.env.multitenant` ni `.env.chatbot`, no abren PostgreSQL y no
llaman a Gemini ni a Vertex AI. Todas las credenciales son ficticias.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parents[1]
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from src.config import (  # noqa: E402
    BASE_DATOS_ESPERADA,
    BASE_DATOS_PROHIBIDA,
    HOST_ESPERADO,
    PUERTO_ESPERADO,
    PUERTO_PROHIBIDO,
    ConfiguracionAdmin,
    ConfiguracionEmpresa,
    ConfiguracionError,
    configuracion_admin,
    configuracion_empresa,
)
from src.database import conectar_admin, conectar_empresa  # noqa: E402

CLAVE_ADMIN = "admin-ficticio-no-mostrar-ax91"
CLAVE_EMPRESA_1 = "empresa1-ficticio-no-mostrar-bx82"
CLAVE_EMPRESA_2 = "empresa2-ficticio-no-mostrar-cx73"
CLAVE_EMPRESA_3 = "empresa3-ficticio-no-mostrar-dx64"
SECRETOS = (CLAVE_ADMIN, CLAVE_EMPRESA_1, CLAVE_EMPRESA_2, CLAVE_EMPRESA_3)
VARIABLES_SENSIBLES = (
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "APP_DB_NAME",
    "APP_DB_HOST",
    "APP_DB_PORT",
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "RAG_EMPRESA_1_USER",
    "RAG_EMPRESA_1_PASSWORD",
    "RAG_EMPRESA_1_ID",
    "RAG_EMPRESA_1_NAME",
    "RAG_EMPRESA_2_USER",
    "RAG_EMPRESA_2_PASSWORD",
    "RAG_EMPRESA_2_ID",
    "RAG_EMPRESA_2_NAME",
    "RAG_EMPRESA_3_USER",
    "RAG_EMPRESA_3_PASSWORD",
    "RAG_EMPRESA_3_ID",
    "RAG_EMPRESA_3_NAME",
)


def _entorno_nuevo() -> dict[str, str]:
    return {
        "POSTGRES_USER": "rag_admin",
        "POSTGRES_PASSWORD": CLAVE_ADMIN,
        "POSTGRES_DB": "rag_chatbot",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5434",
        "APP_DB_NAME": "rag_chatbot",
        "APP_DB_HOST": "127.0.0.1",
        "APP_DB_PORT": "5434",
        "RAG_EMPRESA_1_USER": "rag_empresa_1",
        "RAG_EMPRESA_1_PASSWORD": CLAVE_EMPRESA_1,
        "RAG_EMPRESA_1_ID": "1",
        "RAG_EMPRESA_1_NAME": "Hornos Alba",
        "RAG_EMPRESA_2_USER": "rag_empresa_2",
        "RAG_EMPRESA_2_PASSWORD": CLAVE_EMPRESA_2,
        "RAG_EMPRESA_2_ID": "2",
        "RAG_EMPRESA_2_NAME": "Clínica Sonrisa",
        "RAG_EMPRESA_3_USER": "rag_empresa_3",
        "RAG_EMPRESA_3_PASSWORD": CLAVE_EMPRESA_3,
        "RAG_EMPRESA_3_ID": "3",
        "RAG_EMPRESA_3_NAME": "Ruta Norte",
    }


def _aplicar(monkeypatch: pytest.MonkeyPatch, **cambios: str) -> None:
    valores = _entorno_nuevo()
    valores.update(cambios)
    for nombre, valor in valores.items():
        monkeypatch.setenv(nombre, valor)


def _sin_secretos(texto: str) -> None:
    for secreto in SECRETOS:
        assert secreto not in texto


def _conexion_prohibida(*args: object, **kwargs: object) -> None:
    raise AssertionError("La prueba no debe abrir una conexión PostgreSQL.")


@pytest.fixture(autouse=True)
def entorno_aislado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla el proceso de los `.env` reales y de cualquier conexión."""
    import src.config as config

    monkeypatch.setattr(config, "_entorno_cargado", False)
    monkeypatch.setattr(
        config,
        "RUTA_ENV",
        config.RAIZ_PROYECTO / ".env.no-usar-en-pruebas",
    )

    def _no_leer_archivo(*args: object, **kwargs: object) -> bool:
        if kwargs.get("override"):
            raise AssertionError(
                "El archivo de entorno no debe sobrescribir el proceso."
            )
        return False

    monkeypatch.setattr(config, "load_dotenv", _no_leer_archivo)
    abrir = open

    def _abrir_vigilado(archivo: object, *args: object, **kwargs: object) -> object:
        nombre = os.path.basename(os.fspath(archivo))  # type: ignore[arg-type]
        if nombre == ".env" or nombre.startswith(".env."):
            raise AssertionError(f"No se debe abrir {nombre}.")
        return abrir(archivo, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("builtins.open", _abrir_vigilado)
    for nombre in VARIABLES_SENSIBLES:
        monkeypatch.delenv(nombre, raising=False)
    monkeypatch.setattr("psycopg.connect", _conexion_prohibida)
    monkeypatch.setattr("src.database.psycopg.connect", _conexion_prohibida)


def test_constantes_de_la_instancia_nueva() -> None:
    assert BASE_DATOS_ESPERADA == "rag_chatbot"
    assert PUERTO_ESPERADO == 5434
    assert HOST_ESPERADO == "127.0.0.1"
    assert BASE_DATOS_PROHIBIDA == "rag_multitenant"
    assert PUERTO_PROHIBIDO == 5433


def test_acepta_la_configuracion_nueva(monkeypatch: pytest.MonkeyPatch) -> None:
    _aplicar(monkeypatch, POSTGRES_DB="'rag_chatbot'", POSTGRES_PORT=" 5434 ")
    admin = configuracion_admin()
    assert admin.host == "127.0.0.1"
    assert admin.puerto == 5434
    assert admin.base_datos == "rag_chatbot"
    assert admin.usuario == "rag_admin"
    assert admin.contrasena == CLAVE_ADMIN

    for empresa_id, rol, clave in (
        (1, "rag_empresa_1", CLAVE_EMPRESA_1),
        (2, "rag_empresa_2", CLAVE_EMPRESA_2),
        (3, "rag_empresa_3", CLAVE_EMPRESA_3),
    ):
        empresa = configuracion_empresa(empresa_id)
        assert empresa.host == "127.0.0.1"
        assert empresa.puerto == 5434
        assert empresa.base_datos == "rag_chatbot"
        assert empresa.usuario == rol
        assert empresa.rol_esperado == rol
        assert empresa.contrasena == clave
        assert empresa.contrasena != CLAVE_ADMIN


def test_representacion_oculta_las_contrasenas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _aplicar(monkeypatch)
    objetos = (configuracion_admin(), configuracion_empresa(1))
    for objeto in objetos:
        for texto in (repr(objeto), str(objeto)):
            _sin_secretos(texto)
            assert "contrasena='***'" in texto


@pytest.mark.parametrize(
    "base",
    ["rag_multitenant", "RAG_CHATBOT", "postgres", "rag_chatbot_old"],
)
def test_rechaza_la_base_anterior_y_cualquier_otra(
    monkeypatch: pytest.MonkeyPatch,
    base: str,
) -> None:
    _aplicar(monkeypatch, POSTGRES_DB=base, APP_DB_NAME=base)
    with pytest.raises(ConfiguracionError) as admin:
        configuracion_admin()
    with pytest.raises(ConfiguracionError) as empresa:
        configuracion_empresa(1)
    assert "rag_chatbot" in str(admin.value)
    assert base in str(admin.value)
    _sin_secretos(str(admin.value))
    _sin_secretos(str(empresa.value))
    with pytest.raises(ConfiguracionError):
        conectar_admin()
    with pytest.raises(ConfiguracionError):
        conectar_empresa(2)


@pytest.mark.parametrize("puerto", ["5433", " 5433 ", "5432", "5435", "65536"])
def test_rechaza_el_puerto_anterior_y_cualquier_otro(
    monkeypatch: pytest.MonkeyPatch,
    puerto: str,
) -> None:
    _aplicar(monkeypatch, POSTGRES_PORT=puerto, APP_DB_PORT=puerto)
    with pytest.raises(ConfiguracionError) as admin:
        configuracion_admin()
    with pytest.raises(ConfiguracionError) as empresa:
        configuracion_empresa(3)
    _sin_secretos(str(admin.value))
    _sin_secretos(str(empresa.value))
    with pytest.raises(ConfiguracionError):
        conectar_admin()
    with pytest.raises(ConfiguracionError):
        conectar_empresa(3)


@pytest.mark.parametrize(
    ("cambios", "falla_admin", "falla_empresa"),
    [
        ({"POSTGRES_DB": "rag_multitenant"}, True, False),
        ({"APP_DB_NAME": "rag_multitenant"}, False, True),
        ({"POSTGRES_PORT": "5433"}, True, False),
        ({"APP_DB_PORT": "5433"}, False, True),
        ({"POSTGRES_HOST": "localhost"}, True, False),
        ({"APP_DB_HOST": "localhost"}, False, True),
        ({"POSTGRES_DB": "rag_chatbot", "POSTGRES_PORT": "5433"}, True, False),
        ({"APP_DB_NAME": "rag_multitenant", "APP_DB_PORT": "5434"}, False, True),
        (
            {"POSTGRES_DB": "rag_multitenant", "APP_DB_PORT": "5433"},
            True,
            True,
        ),
        (
            {"POSTGRES_HOST": "0.0.0.0", "APP_DB_HOST": "rag-pgvector-db"},
            True,
            True,
        ),
    ],
)
def test_rechaza_configuraciones_mixtas(
    monkeypatch: pytest.MonkeyPatch,
    cambios: dict[str, str],
    falla_admin: bool,
    falla_empresa: bool,
) -> None:
    _aplicar(monkeypatch, **cambios)
    if falla_admin:
        with pytest.raises(ConfiguracionError) as exc:
            configuracion_admin()
        _sin_secretos(str(exc.value))
        with pytest.raises(ConfiguracionError):
            conectar_admin()
    else:
        admin = configuracion_admin()
        assert (admin.host, admin.puerto, admin.base_datos) == (
            "127.0.0.1",
            5434,
            "rag_chatbot",
        )
    if falla_empresa:
        with pytest.raises(ConfiguracionError) as exc:
            configuracion_empresa(1)
        _sin_secretos(str(exc.value))
        with pytest.raises(ConfiguracionError):
            conectar_empresa(1)
    else:
        empresa = configuracion_empresa(1)
        assert (empresa.host, empresa.puerto, empresa.base_datos) == (
            "127.0.0.1",
            5434,
            "rag_chatbot",
        )


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "0.0.0.0",
        "::1",
        "rag-pgvector-db",
        "rag-chatbot-db",
        "10.0.0.8",
        "127.0.0.2",
        "127.0.0.1.local",
    ],
)
def test_rechaza_hosts_no_autorizados(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    _aplicar(monkeypatch, POSTGRES_HOST=host, APP_DB_HOST=host)
    with pytest.raises(ConfiguracionError) as admin:
        configuracion_admin()
    with pytest.raises(ConfiguracionError) as empresa:
        configuracion_empresa(1)
    assert "127.0.0.1" in str(admin.value)
    _sin_secretos(str(admin.value))
    _sin_secretos(str(empresa.value))
    with pytest.raises(ConfiguracionError):
        conectar_admin()
    with pytest.raises(ConfiguracionError):
        conectar_empresa(1)


def test_host_igual_a_la_clave_no_aparece_en_el_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _aplicar(monkeypatch, POSTGRES_HOST=CLAVE_ADMIN)
    with pytest.raises(ConfiguracionError) as exc:
        configuracion_admin()
    assert CLAVE_ADMIN not in str(exc.value)
    _sin_secretos(str(exc.value))
    with pytest.raises(ConfiguracionError):
        conectar_admin()


def test_variable_de_proceso_antigua_no_elude_la_validacion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.config as config

    monkeypatch.setattr(config, "_entorno_cargado", False)

    def _dotenv_nuevo(*args: object, **kwargs: object) -> bool:
        assert kwargs.get("override") is False
        os.environ.setdefault("POSTGRES_DB", "rag_chatbot")
        os.environ.setdefault("POSTGRES_PORT", "5434")
        return False

    monkeypatch.setattr(config, "load_dotenv", _dotenv_nuevo)
    _aplicar(monkeypatch, POSTGRES_DB="rag_multitenant", POSTGRES_PORT="5433")
    with pytest.raises(ConfiguracionError) as exc:
        configuracion_admin()
    texto = str(exc.value)
    # El proceso conserva 5433; el dotenv simulado no lo sustituye ni lo exime.
    assert "5433" in texto
    _sin_secretos(texto)
    with pytest.raises(ConfiguracionError):
        conectar_admin()


def test_un_valor_antiguo_inyectado_tambien_se_rechaza(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.config as config

    monkeypatch.setattr(config, "_entorno_cargado", False)

    def _dotenv_antiguo(*args: object, **kwargs: object) -> bool:
        os.environ.setdefault("POSTGRES_PORT", "5433")
        return False

    monkeypatch.setattr(config, "load_dotenv", _dotenv_antiguo)
    valores = _entorno_nuevo()
    del valores["POSTGRES_PORT"]
    for nombre, valor in valores.items():
        monkeypatch.setenv(nombre, valor)
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    with pytest.raises(ConfiguracionError) as exc:
        configuracion_admin()
    assert "5433" in str(exc.value)
    _sin_secretos(str(exc.value))
    with pytest.raises(ConfiguracionError):
        conectar_admin()


def test_los_alias_libpq_no_sustituyen_las_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _aplicar(monkeypatch)
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    monkeypatch.setenv("PGPORT", "5434")
    monkeypatch.setenv("PGDATABASE", "rag_chatbot")
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    with pytest.raises(ConfiguracionError) as exc:
        configuracion_admin()
    assert "POSTGRES_DB" in str(exc.value)
    _sin_secretos(str(exc.value))
    with pytest.raises(ConfiguracionError):
        conectar_admin()


def test_una_aceptacion_previa_no_autoriza_el_entorno_posterior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _aplicar(monkeypatch)
    aceptada = configuracion_admin()
    monkeypatch.setenv("APP_DB_NAME", "rag_multitenant")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    assert aceptada.puerto == 5434
    assert aceptada.base_datos == "rag_chatbot"
    with pytest.raises(ConfiguracionError):
        configuracion_admin()
    with pytest.raises(ConfiguracionError):
        conectar_admin()
    with pytest.raises(ConfiguracionError):
        conectar_empresa(1)


def test_empresa_invalida_no_recurre_al_administrador(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _aplicar(monkeypatch, APP_DB_NAME="rag_multitenant")

    def _prohibido() -> None:
        raise AssertionError("No debe usar la configuración administrativa.")

    monkeypatch.setattr("src.database.configuracion_admin", _prohibido)
    with pytest.raises(ConfiguracionError) as exc:
        conectar_empresa(1)
    _sin_secretos(str(exc.value))


@pytest.mark.parametrize(
    "cambios",
    [
        {"RAG_EMPRESA_1_USER": "rag_admin"},
        {"RAG_EMPRESA_1_USER": "rag_empresa_2"},
        {"RAG_EMPRESA_2_ID": "1"},
        {"RAG_EMPRESA_1_PASSWORD": "   "},
    ],
)
def test_conserva_la_correspondencia_estricta_de_roles(
    monkeypatch: pytest.MonkeyPatch,
    cambios: dict[str, str],
) -> None:
    _aplicar(monkeypatch, **cambios)
    empresa_id = 2 if "RAG_EMPRESA_2_ID" in cambios else 1
    with pytest.raises(ConfiguracionError) as exc:
        configuracion_empresa(empresa_id)
    _sin_secretos(str(exc.value))
    with pytest.raises(ConfiguracionError):
        conectar_empresa(empresa_id)


def test_identificador_desconocido_no_abre_conexion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _aplicar(monkeypatch)
    with pytest.raises(ConfiguracionError) as exc:
        conectar_empresa(4)  # type: ignore[arg-type]
    _sin_secretos(str(exc.value))


def test_construccion_directa_tambien_rechaza_el_destino_anterior() -> None:
    with pytest.raises(ConfiguracionError) as puerto:
        ConfiguracionAdmin(
            host="127.0.0.1",
            puerto=5433,
            base_datos="rag_chatbot",
            usuario="rag_admin",
            contrasena=CLAVE_ADMIN,
        )
    with pytest.raises(ConfiguracionError) as base:
        ConfiguracionEmpresa(
            empresa_id=1,
            nombre="Hornos Alba",
            host="127.0.0.1",
            puerto=5434,
            base_datos="rag_multitenant",
            usuario="rag_empresa_1",
            rol_esperado="rag_empresa_1",
            contrasena=CLAVE_EMPRESA_1,
        )
    with pytest.raises(ConfiguracionError) as admin:
        ConfiguracionEmpresa(
            empresa_id=1,
            nombre="Hornos Alba",
            host="127.0.0.1",
            puerto=5434,
            base_datos="rag_chatbot",
            usuario="rag_admin",
            rol_esperado="rag_empresa_1",
            contrasena=CLAVE_EMPRESA_1,
        )
    for exc in (puerto, base, admin):
        _sin_secretos(str(exc.value))
