"""Pruebas del backend FastAPI sin PostgreSQL, embeddings ni Gemini.

Sustituyen las referencias que ``src.api`` usa de verdad: ``conexion_empresa``
en el módulo de la API y ``src.rag.responder``, importado dentro de las
funciones de consulta y aislamiento. Las comprobaciones de
``src.demo_aislamiento`` se ejecutan sobre resultados fabricados, para no
debilitar el contrato de las diez verificaciones.

El cliente de prueba usa ``127.0.0.1``. No se desactiva el middleware local.
Starlette 1.10 resuelve ``TestClient`` con ``httpx2``; el paquete ``httpx`` no
está instalado y estas pruebas no lo importan.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

_RAIZ = Path(__file__).resolve().parents[1]
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from src.api import app  # noqa: E402
from src.config import BASE_DATOS_ESPERADA, ids_empresa, rol_de_empresa  # noqa: E402
from src.database import ErrorConexion  # noqa: E402
from src.demo_aislamiento import PREGUNTA  # noqa: E402
from src.ingest import EMPRESAS_ESPERADAS  # noqa: E402
from src.rag import SIN_INFORMACION, FuenteCitada, RespuestaRag  # noqa: E402
from src.retrieval import ResultadoBusqueda  # noqa: E402

EMPRESAS = tuple(ids_empresa())
SECRETO = "POSTGRES_PASSWORD=no-mostrar ya29.falso Bearer secreto-http"
MARCADOR_AJENO = "SECRETO-AJENO-NO-MOSTRAR"
PREGUNTA_CONSULTA = "¿Cuántos días de vacaciones tengo?"
AVISO_DEMO = (
    "El selector de empresa es una función de demostración local. "
    "No autentica al usuario ni sustituye a una identidad de servidor."
)
AVISO_SALUD_ERROR = (
    "No se pudo comprobar PostgreSQL con los roles de empresa. "
    "Gemini no se ha comprobado."
)
RESULTADO_VERIFICADO = "AISLAMIENTO MULTIEMPRESA VERIFICADO"
RESULTADO_NO_VERIFICADO = "AISLAMIENTO NO VERIFICADO"
COMPROBACIONES_HORNOS = (
    "respuesta exacta de ausencia de información",
    "fragmentos propios y ninguno ajeno",
    "sin documento de guardería de otra empresa",
    "sin fuentes citadas",
    "empresa y nombre coherentes",
)
COMPROBACIONES_CLINICA = (
    "ayuda de guardería fundamentada",
    "documento propio de guardería recuperado",
    "fragmentos propios y ninguno ajeno",
    "fuentes propias y citadas",
    "empresa y nombre coherentes",
)

CASOS_INVALIDOS = [
    pytest.param({"empresa_id": 0, "pregunta": "Hola"}, id="empresa-0"),
    pytest.param({"empresa_id": 4, "pregunta": "Hola"}, id="empresa-inexistente"),
    pytest.param({"empresa_id": 99, "pregunta": "Hola"}, id="empresa-99"),
    pytest.param({"empresa_id": True, "pregunta": "Hola"}, id="booleano-true"),
    pytest.param({"empresa_id": False, "pregunta": "Hola"}, id="booleano-false"),
    pytest.param({"empresa_id": "1", "pregunta": "Hola"}, id="empresa-texto"),
    pytest.param({"empresa_id": 1.5, "pregunta": "Hola"}, id="empresa-float"),
    pytest.param({"empresa_id": 2.0, "pregunta": "Hola"}, id="float-entero"),
    pytest.param({"empresa_id": None, "pregunta": "Hola"}, id="empresa-nula"),
    pytest.param({"empresa_id": 1, "pregunta": ""}, id="pregunta-vacia"),
    pytest.param({"empresa_id": 1, "pregunta": "   "}, id="pregunta-espacios"),
    pytest.param({"empresa_id": 1, "pregunta": "\n\t "}, id="pregunta-blancos"),
    pytest.param({"empresa_id": 1, "pregunta": "a" * 241}, id="pregunta-larga"),
    pytest.param({"empresa_id": 1, "pregunta": "hola\x00"}, id="pregunta-nula"),
    pytest.param(
        {"empresa_id": 1, "pregunta": "Hola", "token": "ya29.no-mostrar"},
        id="campo-extra",
    ),
    pytest.param({"empresa_id": 1}, id="sin-pregunta"),
    pytest.param({"pregunta": "Hola"}, id="sin-empresa"),
    pytest.param({"empresa_id": [1], "pregunta": "Hola"}, id="empresa-lista"),
]

RUTAS_PUBLICAS = [
    pytest.param("/", "text/html", "Enterprise RAG", id="inicio"),
    pytest.param("/styles.css", "text/css", ".cabecera", id="css"),
    pytest.param("/app.js", "javascript", "/api/consulta", id="js"),
    pytest.param("/docs", "text/html", "swagger", id="docs"),
    pytest.param("/openapi.json", "application/json", "/api/health", id="openapi"),
]


class _CursorFalso:
    """Cursor mínimo para la comprobación de salud."""

    def __init__(self, fila: tuple[str, str], consultas: list[str]) -> None:
        self._fila = fila
        self._consultas = consultas

    def __enter__(self) -> "_CursorFalso":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, sql: str, _params: object = None) -> None:
        self._consultas.append(sql)

    def fetchone(self) -> tuple[str, str]:
        return self._fila


class _ConexionFalsa:
    """Conexión de empresa simulada. No abre sockets ni lee credenciales."""

    def __init__(self, fila: tuple[str, str], consultas: list[str]) -> None:
        self._fila = fila
        self._consultas = consultas
        self.revertida = False

    def rollback(self) -> None:
        self.revertida = True

    def cursor(self) -> _CursorFalso:
        return _CursorFalso(self._fila, self._consultas)


@pytest.fixture(autouse=True)
def sin_servicios_externos(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Impide PostgreSQL, embeddings y una llamada RAG no simulada."""
    estado: dict[str, Any] = {"admin": 0, "embeddings": 0, "rag": []}

    def prohibir_admin(*_args: object, **_kwargs: object) -> None:
        estado["admin"] += 1
        raise AssertionError("Se intentó una conexión administrativa.")

    def prohibir_embeddings(*_args: object, **_kwargs: object) -> None:
        estado["embeddings"] += 1
        raise AssertionError("Se intentó generar embeddings reales.")

    def responder_no_previsto(*args: object, **kwargs: object) -> None:
        estado["rag"].append((args, kwargs))
        raise AssertionError("responder() no estaba previsto en esta prueba.")

    monkeypatch.setattr("src.database._abrir", prohibir_admin)
    monkeypatch.setattr("src.database.conectar_admin", prohibir_admin)
    monkeypatch.setattr("src.database.conexion_admin", prohibir_admin)
    monkeypatch.setattr("src.embeddings.generate_embedding", prohibir_embeddings)
    monkeypatch.setattr("src.embeddings.generate_embeddings", prohibir_embeddings)
    monkeypatch.setattr("src.rag.responder", responder_no_previsto)
    return estado


@pytest.fixture
def cliente() -> Any:
    """Cliente loopback. El middleware sigue activo."""
    with TestClient(app, client=("127.0.0.1", 50000)) as instancia:
        yield instancia


def test_salud_comprueba_las_tres_empresas_sin_administrador(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    sin_servicios_externos: dict[str, Any],
) -> None:
    """La salud usa solo roles de empresa y no afirma que Gemini esté comprobado."""
    consultas: list[str] = []
    empresas: list[int] = []

    @contextmanager
    def conexion_falsa(empresa_id: int) -> Any:
        empresas.append(empresa_id)
        fila = (rol_de_empresa(empresa_id), BASE_DATOS_ESPERADA)
        conexion = _ConexionFalsa(fila, consultas)
        yield conexion
        assert conexion.revertida is True

    monkeypatch.setattr("src.api.conexion_empresa", conexion_falsa)
    respuesta = cliente.get("/api/health")
    cuerpo = respuesta.json()

    assert respuesta.status_code == 200
    assert cuerpo["estado"] == "ok"
    assert cuerpo["gemini_comprobado"] is False
    assert cuerpo["postgresql"] == {
        "conectado": True,
        "modo": "roles_de_empresa",
        "administrador": False,
        "empresas_comprobadas": list(EMPRESAS),
    }
    assert empresas == list(EMPRESAS)
    assert any("READ ONLY" in sql for sql in consultas)
    assert any("current_user" in sql and "current_database" in sql for sql in consultas)
    assert sin_servicios_externos["admin"] == 0
    assert sin_servicios_externos["rag"] == []
    assert "Gemini" in cuerpo["tecnologias"]
    _assert_sin_secretos(respuesta.text)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ErrorConexion(SECRETO), id="error-conexion"),
        pytest.param(RuntimeError(SECRETO), id="error-interno"),
    ],
)
def test_salud_oculta_errores_de_conexion(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Un fallo de PostgreSQL no devuelve credenciales, trazas ni estado de Gemini."""

    def conexion_rota(_empresa_id: int) -> None:
        raise error

    monkeypatch.setattr("src.api.conexion_empresa", conexion_rota)
    respuesta = cliente.get("/api/health")
    cuerpo = respuesta.json()

    assert respuesta.status_code == 503
    assert cuerpo["estado"] == "no_disponible"
    assert cuerpo["gemini_comprobado"] is False
    assert cuerpo["postgresql"]["conectado"] is False
    assert cuerpo["postgresql"]["administrador"] is False
    assert cuerpo["postgresql"]["modo"] == "roles_de_empresa"
    assert cuerpo["postgresql"]["empresas_comprobadas"] == []
    assert cuerpo["aviso"] == AVISO_SALUD_ERROR
    assert SECRETO not in respuesta.text
    assert "Traceback" not in respuesta.text
    assert "rag_admin" not in respuesta.text


def test_salud_rechaza_un_rol_administrativo(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    sin_servicios_externos: dict[str, Any],
) -> None:
    """Aunque la fila diga rag_admin, la salud no lo acepta ni lo revela."""

    @contextmanager
    def conexion_admin_disfrazada(_empresa_id: int) -> Any:
        yield _ConexionFalsa(("rag_admin", BASE_DATOS_ESPERADA), [])

    monkeypatch.setattr("src.api.conexion_empresa", conexion_admin_disfrazada)
    respuesta = cliente.get("/api/health")

    assert respuesta.status_code == 503
    assert respuesta.json()["aviso"] == AVISO_SALUD_ERROR
    assert "rag_admin" not in respuesta.text
    assert sin_servicios_externos["admin"] == 0


def test_empresas_devuelve_solo_identidades_publicas(cliente: TestClient) -> None:
    """El listado no incluye roles, contraseñas ni una falsa autenticación."""
    respuesta = cliente.get("/api/empresas")
    cuerpo = respuesta.json()

    assert respuesta.status_code == 200
    assert cuerpo["aviso"] == AVISO_DEMO
    assert cuerpo["empresas"] == [
        {"id": empresa_id, "nombre": EMPRESAS_ESPERADAS[empresa_id]}
        for empresa_id in EMPRESAS
    ]
    assert [empresa["nombre"] for empresa in cuerpo["empresas"]] == [
        "Hornos Alba",
        "Clínica Sonrisa",
        "Ruta Norte",
    ]
    assert all(set(empresa) == {"id", "nombre"} for empresa in cuerpo["empresas"])
    _assert_sin_secretos(respuesta.text)


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_consulta_valida_solo_serializa_la_empresa_solicitada(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    sin_servicios_externos: dict[str, Any],
    empresa_id: int,
) -> None:
    """Una consulta válida no mezcla fragmentos ni fuentes de otra empresa."""
    llamadas: list[tuple[int, str]] = []

    def responder(empresa: int, pregunta: str) -> RespuestaRag:
        llamadas.append((empresa, pregunta))
        return _respuesta_propia(empresa, pregunta)

    monkeypatch.setattr("src.rag.responder", responder)
    respuesta = cliente.post(
        "/api/consulta",
        json={"empresa_id": empresa_id, "pregunta": PREGUNTA_CONSULTA},
    )
    cuerpo = respuesta.json()

    assert respuesta.status_code == 200
    assert llamadas == [(empresa_id, PREGUNTA_CONSULTA)]
    assert cuerpo["empresa_id"] == empresa_id
    assert cuerpo["nombre_empresa"] == EMPRESAS_ESPERADAS[empresa_id]
    assert cuerpo["pregunta"] == PREGUNTA_CONSULTA
    assert cuerpo["respuesta"] == f"Respuesta de {EMPRESAS_ESPERADAS[empresa_id]}"
    assert cuerpo["fragmentos_propios"] == 1
    assert cuerpo["fragmentos_ajenos"] == 0
    assert cuerpo["documentos_recuperados"] == [f"doc-{empresa_id}"]
    assert cuerpo["similitudes"] == [0.812346]
    assert cuerpo["duracion_s"] == 1.235
    assert cuerpo["fragmentos"] == [
        {
            "empresa_id": empresa_id,
            "documento_id": f"doc-{empresa_id}",
            "titulo": "Documento propio",
            "numero": 1,
            "similitud": 0.812346,
            "texto": "Texto propio de la empresa.",
        }
    ]
    assert cuerpo["fuentes"] == [
        {
            "empresa_id": empresa_id,
            "documento_id": f"doc-{empresa_id}",
            "titulo": "Documento propio",
            "numero": 1,
        }
    ]
    assert all(fragmento["empresa_id"] == empresa_id for fragmento in cuerpo["fragmentos"])
    assert all(fuente["empresa_id"] == empresa_id for fuente in cuerpo["fuentes"])
    assert sin_servicios_externos["admin"] == 0
    assert sin_servicios_externos["embeddings"] == 0
    _assert_sin_secretos(respuesta.text)


def test_consulta_envia_la_pregunta_recortada(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Los espacios extremos no llegan al RAG ni se conservan en la respuesta."""
    llamadas: list[str] = []

    def responder(empresa_id: int, pregunta: str) -> RespuestaRag:
        llamadas.append(pregunta)
        return _respuesta_propia(empresa_id, pregunta)

    monkeypatch.setattr("src.rag.responder", responder)
    respuesta = cliente.post(
        "/api/consulta",
        json={"empresa_id": 1, "pregunta": f"  {PREGUNTA_CONSULTA}  "},
    )

    assert respuesta.status_code == 200
    assert llamadas == [PREGUNTA_CONSULTA]
    assert respuesta.json()["pregunta"] == PREGUNTA_CONSULTA


def test_consulta_acepta_el_limite_de_240_caracteres(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El límite del contrato es inclusivo en 240 y no llama a embeddings."""
    pregunta = "a" * 240
    monkeypatch.setattr(
        "src.rag.responder",
        lambda empresa_id, recibida: _respuesta_propia(empresa_id, recibida),
    )
    respuesta = cliente.post("/api/consulta", json={"empresa_id": 3, "pregunta": pregunta})

    assert respuesta.status_code == 200
    assert respuesta.json()["pregunta"] == pregunta
    assert respuesta.json()["empresa_id"] == 3


@pytest.mark.parametrize("cuerpo", CASOS_INVALIDOS)
def test_consulta_rechaza_entrada_invalida_sin_llamar_al_rag(
    cliente: TestClient,
    sin_servicios_externos: dict[str, Any],
    cuerpo: dict[str, Any],
) -> None:
    """La validación estricta ocurre antes de responder()."""
    respuesta = cliente.post("/api/consulta", json=cuerpo)
    texto = respuesta.text

    assert respuesta.status_code == 422
    assert respuesta.json()["detalle"] == "Los datos de entrada no son válidos."
    assert sin_servicios_externos["rag"] == []
    assert "Traceback" not in texto
    assert "ya29.no-mostrar" not in texto
    assert "a" * 241 not in texto
    _assert_sin_secretos(texto)


@pytest.mark.parametrize(
    "contenido",
    [pytest.param(b"{", id="json-incompleto"), pytest.param(b"", id="cuerpo-vacio")],
)
def test_consulta_rechaza_json_malformado(
    cliente: TestClient,
    sin_servicios_externos: dict[str, Any],
    contenido: bytes,
) -> None:
    """Una petición ilegible no llega al RAG ni muestra una traza."""
    respuesta = cliente.post(
        "/api/consulta",
        content=contenido,
        headers={"content-type": "application/json"},
    )

    assert respuesta.status_code == 422
    assert sin_servicios_externos["rag"] == []
    assert "Traceback" not in respuesta.text
    _assert_sin_secretos(respuesta.text)


@pytest.mark.parametrize(
    ("error", "estado", "detalle"),
    [
        pytest.param(
            ValueError(SECRETO),
            422,
            "La pregunta supera la longitud admitida por el modelo.",
            id="longitud-modelo",
        ),
        pytest.param(
            ErrorConexion(SECRETO),
            503,
            "No se pudo consultar la empresa solicitada.",
            id="conexion-empresa",
        ),
        pytest.param(
            RuntimeError(SECRETO),
            503,
            "Error interno. No se muestran trazas ni configuración.",
            id="excepcion-generica",
        ),
    ],
)
def test_consulta_oculta_excepciones(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    estado: int,
    detalle: str,
) -> None:
    """El detalle público no reexpone el mensaje interno."""

    def responder(_empresa_id: int, _pregunta: str) -> RespuestaRag:
        raise error

    monkeypatch.setattr("src.rag.responder", responder)
    respuesta = cliente.post(
        "/api/consulta",
        json={"empresa_id": 2, "pregunta": PREGUNTA_CONSULTA},
    )

    assert respuesta.status_code == estado
    assert respuesta.json() == {"detalle": detalle}
    assert SECRETO not in respuesta.text
    assert "Traceback" not in respuesta.text


@pytest.mark.parametrize("ajeno", ["fragmento", "fuente"])
def test_consulta_bloquea_contenido_de_otra_empresa(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    ajeno: str,
) -> None:
    """Si aparece un elemento ajeno, no se serializa ningún contenido."""

    def responder(empresa_id: int, pregunta: str) -> RespuestaRag:
        fragmentos = [_fragmento(empresa_id)]
        fuentes = [_fuente(empresa_id)]
        if ajeno == "fragmento":
            fragmentos.append(_fragmento(2 if empresa_id != 2 else 1, texto=MARCADOR_AJENO))
        else:
            fuentes.append(_fuente(3, titulo=MARCADOR_AJENO))
        return _respuesta_propia(empresa_id, pregunta, tuple(fragmentos), tuple(fuentes))

    monkeypatch.setattr("src.rag.responder", responder)
    respuesta = cliente.post(
        "/api/consulta",
        json={"empresa_id": 1, "pregunta": PREGUNTA_CONSULTA},
    )

    assert respuesta.status_code == 500
    assert "otra empresa" in respuesta.json()["detalle"]
    assert "fragmentos" not in respuesta.json()
    assert MARCADOR_AJENO not in respuesta.text
    assert "Texto propio" not in respuesta.text


def test_aislamiento_verificado_usa_la_pregunta_fija(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    sin_servicios_externos: dict[str, Any],
) -> None:
    """La prueba ignora el cuerpo y consulta solo a Hornos Alba y Clínica Sonrisa."""
    llamadas: list[tuple[int, str]] = []
    monkeypatch.setattr("src.rag.responder", lambda empresa_id, pregunta: _responder_aislamiento(llamadas, empresa_id, pregunta))
    respuesta = cliente.post("/api/aislamiento", json={"empresa_id": 3, "pregunta": "otra pregunta"})
    cuerpo = respuesta.json()
    comprobaciones = [item for empresa in cuerpo["empresas"] for item in empresa["comprobaciones"]]

    assert respuesta.status_code == 200
    assert llamadas == [(1, PREGUNTA), (2, PREGUNTA)]
    assert cuerpo["pregunta"] == PREGUNTA
    assert cuerpo["aislamiento_verificado"] is True
    assert cuerpo["resultado_global"] == RESULTADO_VERIFICADO
    assert [empresa["empresa_id"] for empresa in cuerpo["empresas"]] == [1, 2]
    assert cuerpo["empresas"][0]["nombre_empresa"] == "Hornos Alba"
    assert cuerpo["empresas"][0]["respuesta"] == SIN_INFORMACION
    assert cuerpo["empresas"][0]["fuentes"] == []
    assert "cs-guarderia" not in cuerpo["empresas"][0]["documentos_recuperados"]
    assert cuerpo["empresas"][1]["nombre_empresa"] == "Clínica Sonrisa"
    assert "ciento cincuenta euros mensuales" in cuerpo["empresas"][1]["respuesta"]
    assert cuerpo["empresas"][1]["documentos_recuperados"] == ["cs-guarderia"]
    assert all(empresa["fragmentos_ajenos"] == 0 for empresa in cuerpo["empresas"])
    assert all(empresa["correcta"] is True for empresa in cuerpo["empresas"])
    assert [item["nombre"] for item in cuerpo["empresas"][0]["comprobaciones"]] == list(COMPROBACIONES_HORNOS)
    assert [item["nombre"] for item in cuerpo["empresas"][1]["comprobaciones"]] == list(COMPROBACIONES_CLINICA)
    assert len(comprobaciones) == 10
    assert all(item["correcta"] is True for item in comprobaciones)
    assert isinstance(cuerpo["duracion_s"], (int, float))
    assert sin_servicios_externos["admin"] == 0
    _assert_sin_secretos(respuesta.text)


def test_aislamiento_negativo_si_falla_una_comprobacion(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una sola comprobación fallida impide el resultado global positivo."""

    def responder(empresa_id: int, pregunta: str) -> RespuestaRag:
        if empresa_id == 1:
            return _respuesta_propia(
                1,
                pregunta,
                (_fragmento(1, "ha-vacaciones"),),
                (_fuente(1, "ha-vacaciones"),),
                SIN_INFORMACION,
            )
        return _respuesta_clinica(pregunta)

    monkeypatch.setattr("src.rag.responder", responder)
    respuesta = cliente.post("/api/aislamiento")
    cuerpo = respuesta.json()

    assert respuesta.status_code == 200
    assert cuerpo["aislamiento_verificado"] is False
    assert cuerpo["resultado_global"] == RESULTADO_NO_VERIFICADO
    assert RESULTADO_VERIFICADO not in respuesta.text
    assert cuerpo["empresas"][0]["correcta"] is False
    assert cuerpo["empresas"][0]["comprobaciones"][3]["correcta"] is False
    assert all(empresa["fragmentos_ajenos"] == 0 for empresa in cuerpo["empresas"])
    assert sum(item["correcta"] for empresa in cuerpo["empresas"] for item in empresa["comprobaciones"]) == 9


def test_aislamiento_bloquea_fragmento_o_fuente_ajena(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El bloqueo ocurre antes de devolver el resultado de cualquiera de las dos empresas."""

    def responder(empresa_id: int, pregunta: str) -> RespuestaRag:
        if empresa_id == 1:
            return _respuesta_hornos(pregunta)
        return _respuesta_propia(
            2,
            pregunta,
            (_fragmento(1, "ha-vacaciones", texto=MARCADOR_AJENO),),
            (),
            "La ayuda de guardería es de ciento cincuenta euros mensuales.",
        )

    monkeypatch.setattr("src.rag.responder", responder)
    respuesta = cliente.post("/api/aislamiento")

    assert respuesta.status_code == 500
    assert respuesta.json()["detalle"] == "Se detectó un fragmento de otra empresa. No se devuelve contenido."
    assert "empresas" not in respuesta.json()
    assert MARCADOR_AJENO not in respuesta.text
    assert SIN_INFORMACION not in respuesta.text


def test_aislamiento_oculta_errores_de_generacion(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un fallo de la segunda empresa no revela el mensaje ni consulta al administrador."""

    def responder(empresa_id: int, pregunta: str) -> RespuestaRag:
        if empresa_id == 1:
            return _respuesta_hornos(pregunta)
        raise RuntimeError(SECRETO)

    monkeypatch.setattr("src.rag.responder", responder)
    respuesta = cliente.post("/api/aislamiento")

    assert respuesta.status_code == 503
    assert respuesta.json() == {"detalle": "No se pudo completar la demostración de aislamiento."}
    assert SECRETO not in respuesta.text
    assert "Traceback" not in respuesta.text


@pytest.mark.parametrize(("ruta", "tipo", "marca"), RUTAS_PUBLICAS)
def test_frontend_y_documentacion_disponibles(
    cliente: TestClient,
    ruta: str,
    tipo: str,
    marca: str,
) -> None:
    """El montaje estático no tapa la documentación ni los archivos del frontend."""
    respuesta = cliente.get(ruta)

    assert respuesta.status_code == 200
    assert tipo in respuesta.headers["content-type"]
    assert marca in respuesta.text
    _assert_sin_secretos(respuesta.text)


def test_openapi_describe_los_cuatro_endpoints(cliente: TestClient) -> None:
    """El contrato publicado coincide con las rutas implementadas."""
    cuerpo = cliente.get("/openapi.json").json()

    assert set(cuerpo["paths"]) >= {"/api/health", "/api/empresas", "/api/consulta", "/api/aislamiento"}
    assert "get" in cuerpo["paths"]["/api/health"]
    assert "get" in cuerpo["paths"]["/api/empresas"]
    assert "post" in cuerpo["paths"]["/api/consulta"]
    assert "post" in cuerpo["paths"]["/api/aislamiento"]


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_middleware_acepta_clientes_locales(host: str) -> None:
    """Loopback y localhost pasan el filtro sin desactivarlo."""
    with TestClient(app, client=(host, 50000)) as local:
        respuesta = local.get("/api/empresas")

    assert respuesta.status_code == 200
    assert respuesta.json()["empresas"][0]["nombre"] == "Hornos Alba"


@pytest.mark.parametrize("host", ["203.0.113.8", "10.0.0.8", "8.8.8.8"])
def test_middleware_rechaza_clientes_no_locales(
    host: str,
    sin_servicios_externos: dict[str, Any],
) -> None:
    """Un cliente remoto no llega a la API ni al frontend."""
    with TestClient(app, client=(host, 50000)) as remoto:
        empresas = remoto.get("/api/empresas")
        consulta = remoto.post("/api/consulta", json={"empresa_id": 1, "pregunta": "Hola"})
        inicio = remoto.get("/")

    for respuesta in (empresas, consulta, inicio):
        assert respuesta.status_code == 403
        assert respuesta.json() == {"detalle": "Esta demostración solo acepta conexiones locales."}
        assert "Enterprise RAG" not in respuesta.text
    assert sin_servicios_externos["rag"] == []


def test_no_hay_cors_permisivo(cliente: TestClient) -> None:
    """Una origen externo no queda autorizado."""
    from fastapi.middleware.cors import CORSMiddleware

    assert all(getattr(capa, "cls", None) is not CORSMiddleware for capa in app.user_middleware)
    cabeceras = {
        "Origin": "https://example.test",
        "Access-Control-Request-Method": "POST",
    }
    lectura = cliente.get("/api/empresas", headers=cabeceras)
    preflight = cliente.options("/api/consulta", headers=cabeceras)

    for respuesta in (lectura, preflight):
        nombres = {clave.lower() for clave in respuesta.headers}
        assert "access-control-allow-origin" not in nombres
        assert "access-control-allow-credentials" not in nombres
    assert lectura.status_code == 200


def test_ruta_api_inexistente_no_devuelve_el_frontend(
    cliente: TestClient,
    sin_servicios_externos: dict[str, Any],
) -> None:
    """Una ruta API ausente no se disfraza de página ni llama al RAG."""
    respuesta = cliente.get("/api/ruta-inexistente")

    assert respuesta.status_code == 404
    assert "Enterprise RAG" not in respuesta.text
    assert "text/html" not in respuesta.headers["content-type"]
    assert "Traceback" not in respuesta.text
    assert sin_servicios_externos["rag"] == []
    _assert_sin_secretos(respuesta.text)


def _respuesta_propia(
    empresa_id: int,
    pregunta: str,
    fragmentos: tuple[ResultadoBusqueda, ...] | None = None,
    fuentes: tuple[FuenteCitada, ...] | None = None,
    respuesta: str | None = None,
) -> RespuestaRag:
    propios = fragmentos if fragmentos is not None else (_fragmento(empresa_id),)
    citadas = fuentes if fuentes is not None else (_fuente(empresa_id),)
    return RespuestaRag(
        empresa_id=empresa_id,
        nombre_empresa=EMPRESAS_ESPERADAS[empresa_id],
        pregunta=pregunta,
        respuesta=respuesta or f"Respuesta de {EMPRESAS_ESPERADAS[empresa_id]}",
        fragmentos=propios,
        fuentes=citadas,
        duracion_s=1.23456,
    )


def _fragmento(
    empresa_id: int,
    documento_id: str | None = None,
    texto: str = "Texto propio de la empresa.",
) -> ResultadoBusqueda:
    return ResultadoBusqueda(
        empresa_id=empresa_id,
        documento_id=documento_id or f"doc-{empresa_id}",
        titulo="Documento propio",
        numero=1,
        texto=texto,
        similitud=0.812345678,
    )


def _fuente(
    empresa_id: int,
    documento_id: str | None = None,
    titulo: str = "Documento propio",
    numero: int = 1,
) -> FuenteCitada:
    return FuenteCitada(
        empresa_id=empresa_id,
        documento_id=documento_id or f"doc-{empresa_id}",
        titulo=titulo,
        numero=numero,
    )


def _respuesta_hornos(pregunta: str) -> RespuestaRag:
    return _respuesta_propia(
        1,
        pregunta,
        (_fragmento(1, "ha-vacaciones", "Veintitrés días laborables."),),
        (),
        SIN_INFORMACION,
    )


def _respuesta_clinica(pregunta: str) -> RespuestaRag:
    return _respuesta_propia(
        2,
        pregunta,
        (
            ResultadoBusqueda(
                empresa_id=2,
                documento_id="cs-guarderia",
                titulo="Ayuda para la guardería",
                numero=2,
                texto="Ayuda propia de Clínica Sonrisa.",
                similitud=0.91,
            ),
        ),
        (_fuente(2, "cs-guarderia", "Ayuda para la guardería", 2),),
        "La ayuda de guardería es de ciento cincuenta euros mensuales.",
    )


def _responder_aislamiento(
    llamadas: list[tuple[int, str]],
    empresa_id: int,
    pregunta: str,
) -> RespuestaRag:
    llamadas.append((empresa_id, pregunta))
    if empresa_id == 1:
        return _respuesta_hornos(pregunta)
    if empresa_id == 2:
        return _respuesta_clinica(pregunta)
    raise AssertionError(f"Empresa no prevista en el aislamiento: {empresa_id}")


def _assert_sin_secretos(texto: str) -> None:
    assert SECRETO not in texto
    assert "POSTGRES_PASSWORD" not in texto
    assert "ya29." not in texto
    assert "Bearer " not in texto
    assert "contrasena" not in texto.lower()
    assert "Traceback" not in texto
