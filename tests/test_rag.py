"""Pruebas del orquestador RAG con Gemini simulado.

La recuperación de las pruebas de contexto usa la base local. Las ramas que el
RLS real no puede provocar —fragmentos ajenos o ausencia total de fragmentos—
sustituyen únicamente ``buscar``. Ninguna prueba construye el cliente real de
Gemini ni abre la conexión administrativa.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_RAIZ = Path(__file__).resolve().parents[1]
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from src.config import ids_empresa  # noqa: E402
from src.database import conectar_empresa  # noqa: E402
from src.rag import SIN_INFORMACION, RagError, responder  # noqa: E402
from src.retrieval import BUSQUEDAS_DEMOSTRACION, ResultadoBusqueda  # noqa: E402

EMPRESAS = tuple(ids_empresa())
PREGUNTA = "¿Cuántos días de vacaciones tengo?"
RESPUESTA_FUNDAMENTADA = "La política consultada está en el contexto recuperado."


class _RespuestaModelo:
    """Imita el atributo ``text`` que lee el orquestador."""

    def __init__(self, text: str) -> None:
        self.text = text


def test_no_hay_llamadas_reales_previstas() -> None:
    """El contrato de las cinco consultas sigue siendo el de la demostración."""
    assert len(BUSQUEDAS_DEMOSTRACION) == 5
    assert SIN_INFORMACION == "No tengo esa información."


@pytest.mark.parametrize(("empresa_id", "pregunta"), BUSQUEDAS_DEMOSTRACION)
def test_el_contexto_enviado_pertenece_solo_a_la_empresa(
    monkeypatch: pytest.MonkeyPatch,
    empresa_id: int,
    pregunta: str,
) -> None:
    """Gemini recibe únicamente fragmentos ya filtrados de la empresa solicitante."""
    capturas: dict[str, Any] = {}
    conexiones = _vigilar_conexiones(monkeypatch)

    def generar(contents: str) -> str:
        cita = _primera_cita(contents)
        return _json(RESPUESTA_FUNDAMENTADA, [cita])

    _instalar_gemini(monkeypatch, capturas, generar)
    resultado = responder(empresa_id, pregunta)

    assert capturas["llamadas"] == 1
    assert capturas["kwargs"].get("vertexai") is True
    assert "api_key" not in capturas["kwargs"]
    assert "credentials" not in capturas["kwargs"]
    bloques = _bloques(capturas["contents"])
    assert len(bloques) == 3
    assert all(f"empresa_id: {empresa_id}" in bloque for bloque in bloques)
    prefijos_ajenos = {1: ("cs-", "rn-"), 2: ("ha-", "rn-"), 3: ("ha-", "cs-")}[empresa_id]
    for bloque in bloques:
        linea_documento = next(
            linea for linea in bloque.splitlines() if linea.startswith("documento_id: ")
        )
        assert not any(prefijo in linea_documento for prefijo in prefijos_ajenos)
        for ajena in EMPRESAS:
            if ajena != empresa_id:
                assert f"empresa_id: {ajena}" not in bloque
    assert all(fragmento.empresa_id == empresa_id for fragmento in resultado.fragmentos)
    assert {fragmento.empresa_id for fragmento in resultado.fragmentos} == {empresa_id}
    assert conexiones == [("empresa", empresa_id)]


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_respuesta_sin_informacion_es_exacta(
    monkeypatch: pytest.MonkeyPatch,
    empresa_id: int,
) -> None:
    """La frase fija no admite paráfrasis ni fuentes no utilizadas."""
    capturas: dict[str, Any] = {}
    _instalar_gemini(
        monkeypatch,
        capturas,
        lambda _contents: _json(
            SIN_INFORMACION,
            [{"documento_id": "documento-no-citado", "numero": 1}],
        ),
    )
    resultado = responder(empresa_id, PREGUNTA)
    assert resultado.respuesta == SIN_INFORMACION
    assert resultado.fuentes == ()
    assert capturas["llamadas"] == 1


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_sin_fragmentos_no_llama_a_gemini(
    monkeypatch: pytest.MonkeyPatch,
    empresa_id: int,
) -> None:
    """Sin contexto no hay generación ni conexión administrativa."""
    conexiones = _vigilar_conexiones(monkeypatch)
    monkeypatch.setattr("src.rag.buscar", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "src.rag.genai.Client",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Gemini no debía ser invocado")
        ),
    )
    resultado = responder(empresa_id, PREGUNTA)
    assert resultado.respuesta == SIN_INFORMACION
    assert resultado.fragmentos == ()
    assert resultado.fuentes == ()
    assert conexiones == []


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_respuesta_fundamentada_incluye_fuentes_validas(
    monkeypatch: pytest.MonkeyPatch,
    empresa_id: int,
) -> None:
    """Solo se presentan fuentes citadas y presentes en el contexto propio."""
    capturas: dict[str, Any] = {}

    def generar(contents: str) -> str:
        return _json(RESPUESTA_FUNDAMENTADA, [_primera_cita(contents)])

    _instalar_gemini(monkeypatch, capturas, generar)
    resultado = responder(empresa_id, PREGUNTA)
    claves_contexto = {
        (fragmento.documento_id, fragmento.numero) for fragmento in resultado.fragmentos
    }
    assert resultado.respuesta == RESPUESTA_FUNDAMENTADA
    assert resultado.fuentes
    assert all(fuente.empresa_id == empresa_id for fuente in resultado.fuentes)
    assert all(
        (fuente.documento_id, fuente.numero) in claves_contexto for fuente in resultado.fuentes
    )
    esperada = _primera_cita(capturas["contents"])
    citados = {(fuente.documento_id, fuente.numero) for fuente in resultado.fuentes}
    assert citados == {(esperada["documento_id"], esperada["numero"])}


@pytest.mark.parametrize("empresa_id", EMPRESAS)
@pytest.mark.parametrize("modo", ("documento_ajeno", "numero_inexistente"))
def test_rechaza_citas_ajenas_al_contexto(
    monkeypatch: pytest.MonkeyPatch,
    empresa_id: int,
    modo: str,
) -> None:
    """Una cita fuera de los fragmentos recuperados impide devolver la respuesta."""
    capturas: dict[str, Any] = {}
    documento_ajeno = "cs-guarderia" if empresa_id != 2 else "ha-vacaciones"

    def generar(contents: str) -> str:
        if modo == "numero_inexistente":
            cita = _primera_cita(contents)
            cita["numero"] = 99999
        else:
            cita = {"documento_id": documento_ajeno, "numero": 1}
        return _json("Respuesta que no debe entregarse.", [cita])

    _instalar_gemini(monkeypatch, capturas, generar)
    with pytest.raises(RagError, match="no pertenece al contexto"):
        responder(empresa_id, PREGUNTA)
    assert capturas["llamadas"] == 1


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_fragmento_ajeno_aborta_antes_de_llamar_a_gemini(
    monkeypatch: pytest.MonkeyPatch,
    empresa_id: int,
) -> None:
    """La comprobación previa evita la generación si el propietario no coincide."""
    conexiones = _vigilar_conexiones(monkeypatch)
    ajena = 1 if empresa_id != 1 else 2
    monkeypatch.setattr(
        "src.rag.buscar",
        lambda *args, **kwargs: [_fragmento_ajeno(ajena)],
    )
    monkeypatch.setattr(
        "src.rag.genai.Client",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Gemini no debía ser invocado")
        ),
    )
    with pytest.raises(RagError, match="No se llama a Gemini"):
        responder(empresa_id, PREGUNTA)
    assert conexiones == []


@pytest.mark.parametrize("empresa_id", EMPRESAS)
def test_error_de_generacion_no_abre_otra_conexion(
    monkeypatch: pytest.MonkeyPatch,
    empresa_id: int,
) -> None:
    """Un fallo de Gemini no reintenta con otra empresa ni con el administrador."""
    conexiones = _vigilar_conexiones(monkeypatch)
    capturas: dict[str, Any] = {}

    def generar(_contents: str) -> str:
        raise RuntimeError("fallo controlado de generacion")

    _instalar_gemini(monkeypatch, capturas, generar)
    with pytest.raises(RagError, match="No se inventa el contenido"):
        responder(empresa_id, PREGUNTA)
    assert capturas["llamadas"] == 1
    assert conexiones == [("empresa", empresa_id)]


def _instalar_gemini(
    monkeypatch: pytest.MonkeyPatch,
    capturas: dict[str, Any],
    generar: Any,
) -> None:
    """Sustituye el cliente antes de responder. No usa ADC ni la red."""
    capturas["llamadas"] = 0

    class _Modelos:
        def generate_content(self, *, model: str, contents: str, config: object) -> _RespuestaModelo:
            capturas["llamadas"] += 1
            capturas["model"] = model
            capturas["contents"] = contents
            capturas["config"] = config
            texto = generar(contents)
            if not isinstance(texto, str):
                raise RagError("El doble de prueba no devolvió texto.")
            return _RespuestaModelo(texto)

    class _Cliente:
        def __init__(self, **kwargs: object) -> None:
            if "api_key" in kwargs or "credentials" in kwargs:
                raise AssertionError("No deben enviarse credenciales al cliente de Gemini.")
            capturas["kwargs"] = kwargs
            self.models = _Modelos()

    monkeypatch.setattr("src.rag.genai.Client", _Cliente)


def _vigilar_conexiones(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int | None]]:
    """Registra conexiones de empresa y falla si aparece la administrativa."""
    llamadas: list[tuple[str, int | None]] = []
    empresa_real = conectar_empresa

    def _empresa(empresa_id: int) -> object:
        llamadas.append(("empresa", empresa_id))
        return empresa_real(empresa_id)

    def _admin(*args: object, **kwargs: object) -> object:
        llamadas.append(("admin", None))
        raise AssertionError("No debe abrirse la conexión administrativa.")

    monkeypatch.setattr("src.database.conectar_empresa", _empresa)
    monkeypatch.setattr("src.database.conectar_admin", _admin)
    monkeypatch.setattr("src.database.conexion_admin", _admin)
    return llamadas


def _fragmento_ajeno(empresa_id: int) -> ResultadoBusqueda:
    return ResultadoBusqueda(
        empresa_id=empresa_id,
        documento_id="cs-guarderia" if empresa_id == 2 else "ha-vacaciones",
        titulo="Documento de otra empresa",
        numero=1,
        texto="Texto ficticio que no debe enviarse al modelo.",
        similitud=0.99,
    )


def _bloques(contents: str) -> list[str]:
    partes = contents.split("<<<FRAGMENTO>>>")[1:]
    return [parte.split("<<<FIN_FRAGMENTO>>>", 1)[0] for parte in partes]


def _primera_cita(contents: str) -> dict[str, object]:
    bloque = _bloques(contents)[0]
    cita: dict[str, object] = {}
    for linea in bloque.splitlines():
        if linea.startswith("documento_id: "):
            cita["documento_id"] = linea.split(": ", 1)[1]
        elif linea.startswith("numero: "):
            cita["numero"] = int(linea.split(": ", 1)[1])
    assert "documento_id" in cita and "numero" in cita
    return cita


def _json(respuesta: str, fuentes: list[dict[str, object]]) -> str:
    return json.dumps({"respuesta": respuesta, "fuentes": fuentes}, ensure_ascii=False)


