"""Configuración de conexiones, separada entre administración y empresas.

Las variables se cargan en tiempo de ejecución con python-dotenv desde el
archivo .env de la raíz del proyecto. Este módulo no imprime ese archivo ni
incluye contraseñas en representaciones, excepciones o diagnósticos.

Una variable ya presente en el proceso no se sustituye, pero tampoco elude las
comprobaciones: base, puerto y host se validan en cada solicitud, antes de
construir la configuración y, por tanto, antes de cualquier conexión.

La correspondencia empresa-rol es fija porque las políticas RLS de PostgreSQL
están definidas para esos roles y no para un identificador recibido en caliente.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from dotenv import load_dotenv

# Raíz del proyecto: src/config.py -> thepower-rag-pgvector/.
RAIZ_PROYECTO = Path(__file__).resolve().parent.parent
RUTA_ENV = RAIZ_PROYECTO / ".env"

USUARIO_ADMIN = "rag_admin"
BASE_DATOS_ESPERADA = "rag_chatbot"
PUERTO_ESPERADO = 5434
HOST_ESPERADO = "127.0.0.1"
# Infraestructura de la versión anterior. Se rechaza aunque el resto coincida.
BASE_DATOS_PROHIBIDA = "rag_multitenant"
PUERTO_PROHIBIDO = 5433
_NOMBRES_SECRETOS = (
    "POSTGRES_PASSWORD",
    "RAG_EMPRESA_1_PASSWORD",
    "RAG_EMPRESA_2_PASSWORD",
    "RAG_EMPRESA_3_PASSWORD",
)

# Correspondencia explícita. No se deduce el rol concatenando un identificador
# recibido desde fuera sin comprobar que pertenece a este mapa.
ROLES_POR_EMPRESA = MappingProxyType(
    {
        1: "rag_empresa_1",
        2: "rag_empresa_2",
        3: "rag_empresa_3",
    }
)

_entorno_cargado = False


class ConfiguracionError(ValueError):
    """Variable ausente, identificador desconocido o destino incompatible."""


@dataclass(frozen=True, slots=True)
class ConfiguracionAdmin:
    """Credenciales exclusivas del rol administrador, reservadas a la ingesta."""

    host: str
    puerto: int
    base_datos: str
    usuario: str
    contrasena: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "ConfiguracionAdmin("
            f"host={self.host!r}, puerto={self.puerto}, "
            f"base_datos={self.base_datos!r}, usuario={self.usuario!r}, "
            "contrasena='***')"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __post_init__(self) -> None:
        _validar_destino(
            "host", self.host, "puerto", self.puerto, "base_datos", self.base_datos
        )
        if self.usuario != USUARIO_ADMIN:
            _rechazar(
                "El usuario administrativo debe ser "
                f"{USUARIO_ADMIN!r}, no {self.usuario!r}."
            )
        if not isinstance(self.contrasena, str) or self.contrasena.strip() == "":
            _rechazar("La contraseña administrativa está vacía.")


@dataclass(frozen=True, slots=True)
class ConfiguracionEmpresa:
    """Credenciales del rol PostgreSQL de una sola empresa."""

    empresa_id: int
    nombre: str
    host: str
    puerto: int
    base_datos: str
    usuario: str
    rol_esperado: str
    contrasena: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "ConfiguracionEmpresa("
            f"empresa_id={self.empresa_id}, nombre={self.nombre!r}, "
            f"host={self.host!r}, puerto={self.puerto}, "
            f"base_datos={self.base_datos!r}, usuario={self.usuario!r}, "
            f"rol_esperado={self.rol_esperado!r}, contrasena='***')"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __post_init__(self) -> None:
        _validar_destino(
            "host", self.host, "puerto", self.puerto, "base_datos", self.base_datos
        )
        if self.usuario == USUARIO_ADMIN:
            _rechazar(
                "La configuración de empresa no puede usar el rol administrador."
            )
        rol = ROLES_POR_EMPRESA.get(self.empresa_id)
        if rol is None or self.rol_esperado != rol or self.usuario != rol:
            _rechazar(
                "La configuración de empresa no coincide con el rol RLS previsto."
            )
        if not isinstance(self.contrasena, str) or self.contrasena.strip() == "":
            _rechazar("La contraseña de empresa está vacía.")


def cargar_entorno() -> None:
    """Carga .env una sola vez, sin sustituir variables ya definidas en el proceso.

    Si el archivo no existe, no se considera un error por sí mismo: la validación
    posterior exige cada variable cuando se solicita su configuración. Nunca se
    lee ni se muestra el contenido del archivo desde este módulo. El indicador de
    carga no recuerda una configuración ya aceptada: cada llamada vuelve a leer
    y validar el entorno efectivo del proceso.
    """
    global _entorno_cargado
    if _entorno_cargado:
        return
    load_dotenv(dotenv_path=RUTA_ENV, override=False, encoding="utf-8")
    _entorno_cargado = True


def ids_empresa() -> tuple[int, ...]:
    """Devuelve los identificadores de empresa admitidos, en orden estable."""
    return tuple(sorted(ROLES_POR_EMPRESA))


def rol_de_empresa(empresa_id: int) -> str:
    """Resuelve el rol PostgreSQL de una empresa o rechaza el identificador."""
    _validar_empresa_id(empresa_id)
    return ROLES_POR_EMPRESA[empresa_id]


def configuracion_admin() -> ConfiguracionAdmin:
    """Valida y devuelve la configuración administrativa.

    Usa exclusivamente POSTGRES_*. No consulta las variables de las empresas.
    Rechaza la base, el puerto o el host anteriores antes de devolver datos
    utilizables por una conexión.
    """
    usuario = _exigir_texto("POSTGRES_USER")
    if usuario != USUARIO_ADMIN:
        _rechazar(
            f"POSTGRES_USER debe ser {USUARIO_ADMIN!r} para respetar el rol "
            f"administrativo previsto, no {usuario!r}."
        )
    base_datos = _exigir_texto("POSTGRES_DB")
    host = _exigir_texto("POSTGRES_HOST")
    puerto = _exigir_puerto("POSTGRES_PORT")
    _validar_destino(
        "POSTGRES_HOST", host, "POSTGRES_PORT", puerto, "POSTGRES_DB", base_datos
    )
    return ConfiguracionAdmin(
        host=host,
        puerto=puerto,
        base_datos=base_datos,
        usuario=usuario,
        contrasena=_exigir_secreto("POSTGRES_PASSWORD"),
    )


def configuracion_empresa(empresa_id: int) -> ConfiguracionEmpresa:
    """Valida y devuelve la configuración de una empresa conocida.

    Usa APP_DB_* y RAG_EMPRESA_N_*. No usa POSTGRES_USER ni POSTGRES_PASSWORD,
    de modo que un fallo de la empresa no puede resolverse con el administrador.
    Rechaza la base, el puerto o el host anteriores antes de devolver datos
    utilizables por una conexión.
    """
    rol_esperado = rol_de_empresa(empresa_id)
    prefijo = f"RAG_EMPRESA_{empresa_id}"
    usuario = _exigir_texto(f"{prefijo}_USER")
    if usuario != rol_esperado:
        _rechazar(
            f"{prefijo}_USER debe ser {rol_esperado!r} para coincidir con la "
            f"política RLS de la empresa {empresa_id}, no {usuario!r}."
        )
    identificador = _exigir_texto(f"{prefijo}_ID")
    if identificador != str(empresa_id):
        _rechazar(
            f"{prefijo}_ID debe ser {empresa_id}, no {identificador!r}."
        )
    base_datos = _exigir_texto("APP_DB_NAME")
    host = _exigir_texto("APP_DB_HOST")
    puerto = _exigir_puerto("APP_DB_PORT")
    _validar_destino(
        "APP_DB_HOST", host, "APP_DB_PORT", puerto, "APP_DB_NAME", base_datos
    )
    return ConfiguracionEmpresa(
        empresa_id=empresa_id,
        nombre=_exigir_texto(f"{prefijo}_NAME"),
        host=host,
        puerto=puerto,
        base_datos=base_datos,
        usuario=usuario,
        rol_esperado=rol_esperado,
        contrasena=_exigir_secreto(f"{prefijo}_PASSWORD"),
    )


def _validar_empresa_id(empresa_id: object) -> None:
    if type(empresa_id) is not int or empresa_id not in ROLES_POR_EMPRESA:
        admitidos = ", ".join(str(valor) for valor in ids_empresa())
        raise ConfiguracionError(
            "Identificador de empresa desconocido. "
            f"Valores admitidos: {admitidos}."
        )


def _validar_destino(
    variable_host: str,
    host: str,
    variable_puerto: str,
    puerto: int,
    variable_base: str,
    base_datos: str,
) -> None:
    """Rechaza cualquier destino distinto de la instancia local nueva."""
    _validar_host(variable_host, host)
    _validar_puerto_destino(variable_puerto, puerto)
    _validar_base_datos(variable_base, base_datos)


def _validar_host(variable: str, host: str) -> None:
    if type(host) is not str or host != HOST_ESPERADO:
        _rechazar(
            f"{variable} debe ser {HOST_ESPERADO!r} para permanecer en local, "
            f"no {host!r}."
        )


def _validar_puerto_destino(variable: str, puerto: int) -> None:
    if type(puerto) is not int:
        _rechazar(f"{variable} debe ser el puerto entero {PUERTO_ESPERADO}.")
    if puerto == PUERTO_PROHIBIDO:
        _rechazar(
            f"{variable} no puede ser {PUERTO_PROHIBIDO}: pertenece a la "
            f"infraestructura anterior. Debe ser {PUERTO_ESPERADO}."
        )
    if puerto != PUERTO_ESPERADO:
        _rechazar(f"{variable} debe ser {PUERTO_ESPERADO}, no {puerto}.")


def _validar_base_datos(variable: str, base_datos: str) -> None:
    if type(base_datos) is not str:
        _rechazar(f"{variable} debe ser {BASE_DATOS_ESPERADA!r}.")
    if base_datos == BASE_DATOS_PROHIBIDA:
        _rechazar(
            f"{variable} no puede ser {BASE_DATOS_PROHIBIDA!r}: pertenece a la "
            f"infraestructura anterior. Debe ser {BASE_DATOS_ESPERADA!r}."
        )
    if base_datos != BASE_DATOS_ESPERADA:
        _rechazar(
            f"{variable} debe ser {BASE_DATOS_ESPERADA!r}, no {base_datos!r}."
        )


def _rechazar(mensaje: str) -> None:
    raise ConfiguracionError(_ocultar_secretos(mensaje))


def _ocultar_secretos(mensaje: str) -> str:
    publicos = {
        BASE_DATOS_ESPERADA,
        BASE_DATOS_PROHIBIDA,
        str(PUERTO_ESPERADO),
        str(PUERTO_PROHIBIDO),
        HOST_ESPERADO,
        USUARIO_ADMIN,
        *ROLES_POR_EMPRESA.values(),
    }
    for nombre in _NOMBRES_SECRETOS:
        secreto = os.environ.get(nombre)
        if secreto and secreto not in publicos and secreto in mensaje:
            mensaje = mensaje.replace(secreto, "***")
    return mensaje


def _exigir_texto(nombre: str) -> str:
    valor = _leer(nombre)
    texto = valor.strip().strip('"').strip("'")
    if not texto:
        raise ConfiguracionError(
            f"La variable de entorno {nombre} está vacía."
        )
    return texto


def _exigir_secreto(nombre: str) -> str:
    valor = _leer(nombre)
    if valor.strip() == "":
        raise ConfiguracionError(
            f"La variable de entorno {nombre} está vacía."
        )
    # Se conserva el valor exacto: una contraseña puede incluir espacios.
    return valor


def _exigir_puerto(nombre: str) -> int:
    texto = _exigir_texto(nombre)
    if not texto.isascii() or not texto.isdigit():
        raise ConfiguracionError(f"{nombre} debe ser un puerto entero.")
    puerto = int(texto)
    if not 1 <= puerto <= 65535:
        raise ConfiguracionError(f"{nombre} está fuera del rango 1-65535.")
    return puerto


def _leer(nombre: str) -> str:
    cargar_entorno()
    valor = os.environ.get(nombre)
    if valor is None:
        raise ConfiguracionError(
            f"Falta la variable de entorno obligatoria {nombre}."
        )
    return valor
