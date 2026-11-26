"""API local de demostración del RAG multiempresa.

El selector de empresa no autentica a nadie. En producción, empresa_id debe
proceder de una identidad autenticada por el servidor, nunca de un formulario
ni de la pregunta. Esta aplicación solo debe escucharse en 127.0.0.1.

No usa la conexión administrativa, no abre CORS y no monta un frontend que
todavía no existe. Las operaciones síncronas del RAG se ejecutan fuera del
event loop.
"""

from __future__ import annotations

import time
from typing import Literal

from starlette.exceptions import HTTPException as StarletteHTTPException

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.config import (
    BASE_DATOS_ESPERADA,
    ConfiguracionError,
    ids_empresa,
    rol_de_empresa,
)
from src.database import ErrorConexion, conexion_empresa
from src.ingest import EMPRESAS_ESPERADAS

HOST_LOCAL = "127.0.0.1"
PUERTO_LOCAL = 8000
# Por debajo de la ventana de 128 tokens del modelo de embeddings.
MAX_CARACTERES_PREGUNTA = 240
_CLIENTES_LOCALES = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
RESULTADO_VERIFICADO = "AISLAMIENTO MULTIEMPRESA VERIFICADO"
RESULTADO_NO_VERIFICADO = "AISLAMIENTO NO VERIFICADO"


class ErrorAPI(Exception):
    """Error controlado que puede mostrarse sin trazas ni secretos."""

    def __init__(self, estado: int, detalle: str) -> None:
        self.estado = estado
        self.detalle = detalle
        super().__init__(detalle)


class EmpresaResumen(BaseModel):
    """Identidad pública de una empresa. No incluye roles ni credenciales."""

    model_config = ConfigDict(extra="forbid")

    id: int
    nombre: str


class EmpresasRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    empresas: list[EmpresaResumen]
    aviso: str


class SaludPostgreSQL(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conectado: bool
    modo: Literal["roles_de_empresa"]
    administrador: Literal[False]
    empresas_comprobadas: list[int]


class SaludRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estado: Literal["ok", "no_disponible"]
    postgresql: SaludPostgreSQL
    tecnologias: list[str]
    gemini_comprobado: Literal[False]
    aviso: str


class ConsultaEntrada(BaseModel):
    """Petición estricta. Los campos extra y los tipos coercibles se rechazan."""

    model_config = ConfigDict(extra="forbid", strict=True)

    empresa_id: Literal[1, 2, 3]
    pregunta: str = Field(min_length=1, max_length=MAX_CARACTERES_PREGUNTA)

    @field_validator("empresa_id", mode="before")
    @classmethod
    def _empresa_id_estricto(cls, valor: object) -> int:
        """Rechaza el tipo original antes de que Pydantic convierta bool o float."""
        if type(valor) is not int or valor not in (1, 2, 3):
            raise ValueError("empresa_id debe ser un entero válido: 1, 2 o 3.")
        return valor

    @field_validator("empresa_id")
    @classmethod
    def _empresa_conocida(cls, empresa_id: int) -> int:
        if empresa_id not in ids_empresa():
            raise ValueError("Identificador de empresa no admitido.")
        return empresa_id

    @field_validator("pregunta")
    @classmethod
    def _pregunta_util(cls, pregunta: str) -> str:
        limpia = pregunta.strip()
        if not limpia:
            raise ValueError("La pregunta no puede estar vacía.")
        if len(limpia) > MAX_CARACTERES_PREGUNTA:
            raise ValueError("La pregunta es excesivamente larga.")
        if "\x00" in limpia:
            raise ValueError("La pregunta contiene caracteres no admitidos.")
        return limpia


class FragmentoConsulta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    empresa_id: int
    documento_id: str
    titulo: str
    numero: int
    similitud: float
    texto: str


class FuenteConsulta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    empresa_id: int
    documento_id: str
    titulo: str
    numero: int


class ConsultaRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    empresa_id: int
    nombre_empresa: str
    pregunta: str
    respuesta: str
    documentos_recuperados: list[str]
    fragmentos_propios: int
    fragmentos_ajenos: int
    similitudes: list[float]
    fragmentos: list[FragmentoConsulta]
    fuentes: list[FuenteConsulta]
    duracion_s: float


class ComprobacionAislamiento(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str
    correcta: bool


class ResultadoAislamientoEmpresa(BaseModel):
    model_config = ConfigDict(extra="forbid")

    empresa_id: int
    nombre_empresa: str
    respuesta: str
    documentos_recuperados: list[str]
    fragmentos_propios: int
    fragmentos_ajenos: int
    fuentes: list[FuenteConsulta]
    comprobaciones: list[ComprobacionAislamiento]
    duracion_s: float
    correcta: bool


class AislamientoRespuesta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pregunta: str
    empresas: list[ResultadoAislamientoEmpresa]
    duracion_s: float
    aislamiento_verificado: bool
    resultado_global: str


def crear_aplicacion() -> FastAPI:
    """Construye la API sin montar directorios de frontend inexistentes."""
    aplicacion = FastAPI(
        title="thepower-rag-pgvector",
        version="1.0.0",
        description=(
            "Demostración local del aislamiento multiempresa. "
            "Elegir una empresa en la interfaz no constituye autenticación. "
            "El servidor debe permanecer en 127.0.0.1."
        ),
    )
    aplicacion.middleware("http")(_solo_clientes_locales)
    aplicacion.add_exception_handler(ErrorAPI, _gestionar_error_api)
    aplicacion.add_exception_handler(RequestValidationError, _gestionar_validacion)
    aplicacion.add_exception_handler(Exception, _gestionar_error_inesperado)
    aplicacion.add_api_route("/api/health", salud, methods=["GET"], response_model=SaludRespuesta)
    aplicacion.add_api_route(
        "/api/empresas",
        listar_empresas,
        methods=["GET"],
        response_model=EmpresasRespuesta,
    )
    aplicacion.add_api_route(
        "/api/consulta",
        consultar,
        methods=["POST"],
        response_model=ConsultaRespuesta,
    )
    aplicacion.add_api_route(
        "/api/aislamiento",
        demostrar_aislamiento,
        methods=["POST"],
        response_model=AislamientoRespuesta,
    )
    # El HTML se añadirá en frontend/ y se montará aquí. Ahora el directorio
    # no existe y no debe crearse ni montarse.
    _montar_frontend_si_existe(aplicacion)
    return aplicacion


async def salud() -> SaludRespuesta | JSONResponse:
    """Comprueba PostgreSQL con roles de empresa. No llama a Gemini."""
    try:
        comprobadas = await run_in_threadpool(_comprobar_postgresql)
    except Exception:
        cuerpo = SaludRespuesta(
            estado="no_disponible",
            postgresql=SaludPostgreSQL(
                conectado=False,
                modo="roles_de_empresa",
                administrador=False,
                empresas_comprobadas=[],
            ),
            tecnologias=["PostgreSQL", "pgvector", "RLS", "Gemini"],
            gemini_comprobado=False,
            aviso=(
                "No se pudo comprobar PostgreSQL con los roles de empresa. "
                "Gemini no se ha comprobado."
            ),
        )
        return JSONResponse(status_code=503, content=cuerpo.model_dump())
    return SaludRespuesta(
        estado="ok",
        postgresql=SaludPostgreSQL(
            conectado=True,
            modo="roles_de_empresa",
            administrador=False,
            empresas_comprobadas=comprobadas,
        ),
        tecnologias=["PostgreSQL", "pgvector", "RLS", "Gemini"],
        gemini_comprobado=False,
        aviso="Gemini figura como tecnología del sistema, pero este endpoint no lo comprueba.",
    )


async def listar_empresas() -> EmpresasRespuesta:
    """Devuelve solo identificadores y nombres públicos."""
    return EmpresasRespuesta(
        empresas=[
            EmpresaResumen(id=empresa_id, nombre=EMPRESAS_ESPERADAS[empresa_id])
            for empresa_id in ids_empresa()
        ],
        aviso=(
            "El selector de empresa es una función de demostración local. "
            "No autentica al usuario ni sustituye a una identidad de servidor."
        ),
    )


async def consultar(entrada: ConsultaEntrada) -> ConsultaRespuesta:
    """Ejecuta responder() fuera del event loop y oculta cualquier fragmento ajeno."""
    try:
        resultado = await run_in_threadpool(
            _consultar,
            entrada.empresa_id,
            entrada.pregunta,
        )
    except ErrorAPI:
        raise
    except Exception as exc:
        raise ErrorAPI(503, _detalle_seguro(exc)) from None
    return _respuesta_consulta(resultado)


async def demostrar_aislamiento() -> AislamientoRespuesta:
    """Repite la demostración real de guardería sin lanzar procesos externos."""
    inicio = time.perf_counter()
    try:
        empresas = await run_in_threadpool(_ejecutar_aislamiento)
    except ErrorAPI:
        raise
    except Exception as exc:
        raise ErrorAPI(503, _detalle_seguro(exc)) from None
    verificado = all(empresa.correcta for empresa in empresas) and len(empresas) == 2
    return AislamientoRespuesta(
        pregunta=_pregunta_aislamiento(),
        empresas=empresas,
        duracion_s=round(time.perf_counter() - inicio, 3),
        aislamiento_verificado=verificado,
        resultado_global=RESULTADO_VERIFICADO if verificado else RESULTADO_NO_VERIFICADO,
    )


def main() -> None:
    """Arranca Uvicorn solo en el bucle local. No debe usarse con 0.0.0.0."""
    import uvicorn

    uvicorn.run(app, host=HOST_LOCAL, port=PUERTO_LOCAL, reload=False, access_log=False)


def _comprobar_postgresql() -> list[int]:
    """Abre una conexión de solo lectura por empresa. Nunca usa rag_admin."""
    comprobadas: list[int] = []
    for empresa_id in ids_empresa():
        rol_esperado = rol_de_empresa(empresa_id)
        try:
            with conexion_empresa(empresa_id) as conexion:
                conexion.rollback()
                with conexion.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute("SELECT current_user, current_database()")
                    fila = cursor.fetchone()
        except ErrorConexion as exc:
            raise ErrorAPI(503, "No se pudo conectar con PostgreSQL mediante un rol de empresa.") from exc
        if fila is None or fila[0] != rol_esperado or fila[1] != BASE_DATOS_ESPERADA:
            raise ErrorAPI(503, "La comprobación de PostgreSQL no superó el aislamiento de roles.")
        comprobadas.append(empresa_id)
    if comprobadas != list(ids_empresa()):
        raise ErrorAPI(503, "No se comprobaron todas las empresas.")
    return comprobadas


def _consultar(empresa_id: int, pregunta: str) -> object:
    from src.rag import responder

    try:
        return responder(empresa_id, pregunta)
    except ValueError as exc:
        raise ErrorAPI(422, "La pregunta supera la longitud admitida por el modelo.") from exc
    except (ConfiguracionError, ErrorConexion) as exc:
        raise ErrorAPI(503, "No se pudo consultar la empresa solicitada.") from exc
    except Exception as exc:
        raise ErrorAPI(503, _detalle_seguro(exc)) from exc


def _ejecutar_aislamiento() -> list[ResultadoAislamientoEmpresa]:
    from src.demo_aislamiento import PREGUNTA, _comprobar, _documentos, _recuento
    from src.rag import responder

    resultados: list[ResultadoAislamientoEmpresa] = []
    for empresa_id in (1, 2):
        try:
            resultado = responder(empresa_id, PREGUNTA)
        except Exception as exc:
            raise ErrorAPI(503, "No se pudo completar la demostración de aislamiento.") from exc
        _rechazar_si_hay_ajenos(resultado)
        propios, ajenos = _recuento(resultado)
        comprobaciones = [
            ComprobacionAislamiento(nombre=nombre, correcta=correcta)
            for nombre, correcta in _comprobar(resultado)
        ]
        resultados.append(
            ResultadoAislamientoEmpresa(
                empresa_id=resultado.empresa_id,
                nombre_empresa=resultado.nombre_empresa,
                respuesta=resultado.respuesta,
                documentos_recuperados=list(_documentos(resultado)),
                fragmentos_propios=propios,
                fragmentos_ajenos=ajenos,
                fuentes=_fuentes(resultado),
                comprobaciones=comprobaciones,
                duracion_s=round(resultado.duracion_s, 3),
                correcta=all(item.correcta for item in comprobaciones) and ajenos == 0,
            )
        )
    return resultados


def _respuesta_consulta(resultado: object) -> ConsultaRespuesta:
    _rechazar_si_hay_ajenos(resultado)
    from src.demo_aislamiento import _documentos, _recuento

    propios, ajenos = _recuento(resultado)
    fragmentos = _fragmentos_propios(resultado)
    return ConsultaRespuesta(
        empresa_id=resultado.empresa_id,
        nombre_empresa=resultado.nombre_empresa,
        pregunta=resultado.pregunta,
        respuesta=resultado.respuesta,
        documentos_recuperados=list(_documentos(resultado)),
        fragmentos_propios=propios,
        fragmentos_ajenos=ajenos,
        similitudes=[fragmento.similitud for fragmento in fragmentos],
        fragmentos=fragmentos,
        fuentes=_fuentes(resultado),
        duracion_s=round(resultado.duracion_s, 3),
    )


def _fragmentos_propios(resultado: object) -> list[FragmentoConsulta]:
    return [
        FragmentoConsulta(
            empresa_id=fragmento.empresa_id,
            documento_id=fragmento.documento_id,
            titulo=fragmento.titulo,
            numero=fragmento.numero,
            similitud=round(float(fragmento.similitud), 6),
            texto=fragmento.texto,
        )
        for fragmento in resultado.fragmentos
        if fragmento.empresa_id == resultado.empresa_id
    ]


def _fuentes(resultado: object) -> list[FuenteConsulta]:
    return [
        FuenteConsulta(
            empresa_id=fuente.empresa_id,
            documento_id=fuente.documento_id,
            titulo=fuente.titulo,
            numero=fuente.numero,
        )
        for fuente in resultado.fuentes
        if fuente.empresa_id == resultado.empresa_id
    ]


def _rechazar_si_hay_ajenos(resultado: object) -> None:
    """Impide serializar contenido de otra empresa, aunque la recuperación falle."""
    if any(fragmento.empresa_id != resultado.empresa_id for fragmento in resultado.fragmentos):
        raise ErrorAPI(500, "Se detectó un fragmento de otra empresa. No se devuelve contenido.")
    if any(fuente.empresa_id != resultado.empresa_id for fuente in resultado.fuentes):
        raise ErrorAPI(500, "Se detectó una fuente de otra empresa. No se devuelve contenido.")


def _pregunta_aislamiento() -> str:
    from src.demo_aislamiento import PREGUNTA

    return PREGUNTA


def _montar_frontend_si_existe(aplicacion: FastAPI) -> None:
    """Deja preparada la ruta estática, pero no crea ni monta un directorio ausente."""
    from src.config import RAIZ_PROYECTO

    directorio = RAIZ_PROYECTO / "frontend"
    if not directorio.is_dir():
        return
    from fastapi.staticfiles import StaticFiles

    aplicacion.mount("/", StaticFiles(directory=directorio, html=True), name="frontend")


async def _solo_clientes_locales(request: Request, call_next: object) -> object:
    cliente = request.client.host if request.client is not None else ""
    if cliente not in _CLIENTES_LOCALES:
        return JSONResponse(
            status_code=403,
            content={"detalle": "Esta demostración solo acepta conexiones locales."},
        )
    return await call_next(request)


async def _gestionar_error_api(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ErrorAPI):
        return JSONResponse(status_code=500, content={"detalle": "Error interno."})
    return JSONResponse(status_code=exc.estado, content={"detalle": exc.detalle})


async def _gestionar_validacion(_request: Request, exc: Exception) -> JSONResponse:
    errores: list[dict[str, str]] = []
    if isinstance(exc, RequestValidationError):
        for error in exc.errors():
            campo = ".".join(str(parte) for parte in error.get("loc", ()) if parte != "body")
            errores.append({"campo": campo or "cuerpo", "mensaje": "Valor no admitido."})
    return JSONResponse(
        status_code=422,
        content={"detalle": "Los datos de entrada no son válidos.", "errores": errores},
    )


async def _gestionar_error_inesperado(_request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detalle": "Petición no admitida."})
    return JSONResponse(
        status_code=500,
        content={"detalle": "Error interno. No se muestran trazas ni configuración."},
    )


def _detalle_seguro(exc: Exception) -> str:
    if isinstance(exc, ErrorAPI):
        return exc.detalle
    if isinstance(exc, (ConfiguracionError, ErrorConexion)):
        return "No se pudo usar la configuración de la empresa."
    nombre = type(exc).__name__
    if nombre in {"RagError", "RecuperacionError", "ValueError"}:
        return "No se pudo completar la consulta con la empresa solicitada."
    return "Error interno. No se muestran trazas ni configuración."


# Una sola instancia, creada cuando ya existen middleware y manejadores.
app = crear_aplicacion()
aplicacion = app


if __name__ == "__main__":
    main()
