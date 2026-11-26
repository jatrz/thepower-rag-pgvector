"""Pruebas de recuperación vectorial sobre la base local ya ingerida.

No modifican datos, no llaman a Gemini y no leen .env. La empresa se pasa de
forma explícita: no se deduce de la pregunta. Los rechazos de parámetros se
comprueban antes de abrir una conexión.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parents[1]
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from src.config import ConfiguracionError  # noqa: E402
from src.retrieval import (  # noqa: E402
    BUSQUEDAS_DEMOSTRACION,
    RecuperacionError,
    ResultadoBusqueda,
    buscar,
)

PREGUNTA_VACACIONES = "¿Cuántos días de vacaciones tengo?"
PREGUNTA_GUARDERIA = "¿La empresa me ayuda a pagar la guardería de mi hijo?"
POLITICAS_VACACIONES = (
    (1, "ha-vacaciones", 23),
    (2, "cs-vacaciones", 22),
    (3, "rn-vacaciones", 25),
)
# Cada cantidad debe reconocerse en cifra o en letra, pero siempre unida a los
# días laborables de vacaciones. No basta con que el número aparezca en otra frase.
_FORMAS_DE_DIAS = {
    23: ("23", "veintitres"),
    22: ("22", "veintidos"),
    25: ("25", "veinticinco"),
}
PREGUNTAS_VACIAS = ("", "   ", "\n\t")
EMPRESAS_INVALIDAS = (
    (0, ConfiguracionError),
    (4, ConfiguracionError),
    (-1, ConfiguracionError),
    (99, ConfiguracionError),
    (None, RecuperacionError),
    ("1", RecuperacionError),
    (True, RecuperacionError),
    (1.5, RecuperacionError),
)
TOP_K_INVALIDOS = (0, -1, -5, True, False, None, "3", 1.5, 3.0)


def test_las_busquedas_de_demostracion_cubren_el_contrato() -> None:
    """La suite reutiliza las cinco preguntas ya verificadas manualmente."""
    assert BUSQUEDAS_DEMOSTRACION == (
        (1, PREGUNTA_VACACIONES),
        (2, PREGUNTA_VACACIONES),
        (3, PREGUNTA_VACACIONES),
        (1, PREGUNTA_GUARDERIA),
        (2, PREGUNTA_GUARDERIA),
    )


@pytest.mark.parametrize(("empresa_id", "pregunta"), BUSQUEDAS_DEMOSTRACION)
def test_recupera_tres_fragmentos_de_la_empresa_ordenados(
    empresa_id: int,
    pregunta: str,
) -> None:
    """Cada consulta devuelve tres fragmentos propios, de mayor a menor similitud."""
    resultados = buscar(empresa_id, pregunta)
    assert len(resultados) == 3
    assert all(resultado.empresa_id == empresa_id for resultado in resultados)
    similitudes = [resultado.similitud for resultado in resultados]
    assert similitudes == sorted(similitudes, reverse=True)
    assert all(_finita(similitud) for similitud in similitudes)
    assert all(resultado.numero >= 1 for resultado in resultados)
    assert all(resultado.documento_id and resultado.titulo for resultado in resultados)


@pytest.mark.parametrize(("empresa_id", "documento_id", "dias"), POLITICAS_VACACIONES)
def test_recupera_la_politica_de_vacaciones_de_la_empresa(
    empresa_id: int,
    documento_id: str,
    dias: int,
) -> None:
    """La política recuperada es la de la empresa solicitante, con sus días.

    Acepta la cantidad en cifra o en letra, siempre asociada a los días
    laborables de vacaciones de ese documento.
    """
    resultados = buscar(empresa_id, PREGUNTA_VACACIONES)
    _exigir_aislamiento(empresa_id, resultados)
    propios = [resultado for resultado in resultados if resultado.documento_id == documento_id]
    assert propios, f"No se recuperó {documento_id} para la empresa {empresa_id}"
    assert any(_cantidad_de_vacaciones(resultado.texto, dias) for resultado in propios), (
        f"{documento_id} no asocia {dias} días, en cifra o en letra, "
        "a los días laborables de vacaciones."
    )
    otras = set(_FORMAS_DE_DIAS) - {dias}
    assert not any(
        _cantidad_de_vacaciones(resultado.texto, otra)
        for resultado in propios
        for otra in otras
    ), f"{documento_id} asocia una cantidad de vacaciones de otra empresa."
    documentos_de_vacaciones = {
        resultado.documento_id
        for resultado in resultados
        if resultado.documento_id.endswith("-vacaciones")
    }
    assert documentos_de_vacaciones == {documento_id}


def test_hornos_alba_no_recupera_la_guarderia_de_clinica_sonrisa() -> None:
    """La ayuda de guardería existe solo en Clínica Sonrisa y no debe filtrarse."""
    resultados = buscar(1, PREGUNTA_GUARDERIA)
    _exigir_aislamiento(1, resultados)
    assert resultados
    assert all(resultado.documento_id != "cs-guarderia" for resultado in resultados)
    assert all(not resultado.documento_id.startswith(("cs-", "rn-")) for resultado in resultados)
    for resultado in resultados:
        texto = f"{resultado.titulo}\n{resultado.texto}".lower()
        assert "guarder" not in texto


def test_clinica_sonrisa_recupera_su_ayuda_de_guarderia() -> None:
    """La misma pregunta, con el rol de Clínica Sonrisa, sí alcanza su documento."""
    resultados = buscar(2, PREGUNTA_GUARDERIA)
    _exigir_aislamiento(2, resultados)
    assert any(resultado.documento_id == "cs-guarderia" for resultado in resultados)
    assert all(not resultado.documento_id.startswith(("ha-", "rn-")) for resultado in resultados)


@pytest.mark.parametrize("pregunta", PREGUNTAS_VACIAS)
def test_rechaza_pregunta_vacia(pregunta: str) -> None:
    """Una pregunta vacía no debe generar un embedding ni abrir PostgreSQL."""
    with pytest.raises(RecuperacionError):
        buscar(1, pregunta)


@pytest.mark.parametrize(("empresa_id", "excepcion"), EMPRESAS_INVALIDAS)
def test_rechaza_identificador_invalido(empresa_id: object, excepcion: type[Exception]) -> None:
    """Los identificadores ajenos al mapa o de tipo incorrecto se rechazan."""
    with pytest.raises(excepcion):
        buscar(empresa_id, PREGUNTA_VACACIONES)  # type: ignore[arg-type]


@pytest.mark.parametrize("top_k", TOP_K_INVALIDOS)
def test_rechaza_top_k_incorrecto(top_k: object) -> None:
    """top_k solo es válido cuando es un entero mayor o igual que 1."""
    with pytest.raises(RecuperacionError):
        buscar(1, PREGUNTA_VACACIONES, top_k=top_k)  # type: ignore[arg-type]


def _cantidad_de_vacaciones(texto: str, dias: int) -> bool:
    """Comprueba que la cantidad corresponde a los días laborables de vacaciones."""
    formas = _FORMAS_DE_DIAS[dias]
    normalizado = _sin_acentos(texto).lower()
    alternativas = "|".join(re.escape(forma) for forma in formas)
    patron = re.compile(rf"\b(?:{alternativas})\s+dias\s+laborables\b")
    return patron.search(normalizado) is not None


def _sin_acentos(texto: str) -> str:
    return "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caracter) != "Mn"
    )


def _exigir_aislamiento(empresa_id: int, resultados: list[ResultadoBusqueda]) -> None:
    assert resultados
    assert all(resultado.empresa_id == empresa_id for resultado in resultados)


def _finita(valor: float) -> bool:
    return valor == valor and valor not in {float("inf"), float("-inf")}
