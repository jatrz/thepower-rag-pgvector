"""Fragmentación de los documentos JSON multiempresa.

Divide cada documento por párrafos y solo subdivide un párrafo si no cabe
en el modelo de embeddings. No genera vectores ni accede a PostgreSQL.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

# Mismo modelo e instancia que src/embeddings.py. Estas funciones no calculan
# embeddings: solo exponen el tokenizador y el límite, incluidos los tokens
# especiales. No se carga una segunda copia del modelo.
from .embeddings import _contar_tokens, _longitud_maxima, _obtener_modelo

DIRECTORIO_DATOS = Path(__file__).resolve().parent.parent / "data"

# Archivos inspeccionados. El orden de lectura no decide el informe: las
# empresas se ordenan después por empresa_id.
ARCHIVOS_JSON = (
    "clinica_sonrisa.json",
    "hornos_alba.json",
    "ruta_norte.json",
)

CAMPOS_EMPRESA = frozenset({"empresa_id", "nombre", "documentos"})
CAMPOS_DOCUMENTO = frozenset({"id", "titulo", "contenido"})

# Cada patrón consume el separador y deja el corte al inicio del siguiente
# tramo, de modo que las piezas concatenadas reconstruyen el texto.
_PATRON_FRASES = re.compile(r"(?<=[.!?])\s+(?=[¿¡A-ZÁÉÍÓÚÜÑ])")
_PATRON_CLAUSULAS = re.compile(r"(?<=[;:])\s+")
_PATRON_COMAS = re.compile(r"(?<=,)\s+")
_PATRON_PALABRAS = re.compile(r"\s+")
_PATRONES = (_PATRON_FRASES, _PATRON_CLAUSULAS, _PATRON_COMAS, _PATRON_PALABRAS)


class DocumentoFuente(TypedDict):
    """Documento ya validado, todavía sin fragmentar."""

    id: str
    titulo: str
    contenido: str


class EmpresaFuente(TypedDict):
    """Empresa ya validada y sus documentos en el orden del JSON."""

    empresa_id: int
    nombre: str
    documentos: list[DocumentoFuente]


@dataclass(frozen=True, slots=True)
class Fragmento:
    """Trozo recuperable de un único documento de una única empresa.

    Attributes:
        empresa_id: Identificador de la empresa propietaria.
        nombre_empresa: Nombre de esa empresa.
        documento_id: Identificador del documento de origen.
        titulo: Título de ese documento.
        numero: Posición del fragmento dentro del documento, desde 1.
        texto: Texto del fragmento, sin mezclar otros documentos.
    """

    empresa_id: int
    nombre_empresa: str
    documento_id: str
    titulo: str
    numero: int
    texto: str


def cargar_empresas(directorio: Path = DIRECTORIO_DATOS) -> list[EmpresaFuente]:
    """Lee y valida los tres JSON de ``data/``.

    Returns:
        Empresas ordenadas por ``empresa_id``. Cada elemento conserva el
        nombre y los documentos en el orden del archivo.

    Raises:
        FileNotFoundError: Si falta uno de los JSON inspeccionados.
        ValueError: Si la estructura o un campo obligatorio no es válido.
    """
    rutas = _rutas_json(directorio)
    empresas = [_leer_empresa(ruta) for ruta in rutas]
    empresas.sort(key=lambda empresa: int(empresa["empresa_id"]))
    _validar_identificadores_unicos(empresas)
    return empresas


def fragmentar_empresas(empresas: list[EmpresaFuente]) -> list[Fragmento]:
    """Fragmenta los documentos sin mezclar empresas ni documentos.

    Args:
        empresas: Resultado validado de :func:`cargar_empresas`.

    Returns:
        Fragmentos en orden de empresa, documento y número.
    """
    fragmentos: list[Fragmento] = []
    for empresa in empresas:
        empresa_id = int(empresa["empresa_id"])
        nombre = str(empresa["nombre"])
        for documento in empresa["documentos"]:
            fragmentos.extend(
                fragmentar_documento(
                    empresa_id=empresa_id,
                    nombre_empresa=nombre,
                    documento_id=str(documento["id"]),
                    titulo=str(documento["titulo"]),
                    contenido=str(documento["contenido"]),
                )
            )
    _validar_fragmentos(fragmentos)
    return fragmentos


def fragmentar_documento(
    empresa_id: int,
    nombre_empresa: str,
    documento_id: str,
    titulo: str,
    contenido: str,
) -> list[Fragmento]:
    """Divide un documento por párrafos y subdivide solo lo que no cabe.

    Raises:
        ValueError: Si el documento no aporta texto.
        RuntimeError: Si la división perdiera texto o un fragmento siguiera
            superando el límite del modelo.
    """
    parrafos = _parrafos(contenido)
    if not parrafos:
        raise ValueError(f"El documento {documento_id} no contiene párrafos.")

    textos: list[str] = []
    for parrafo in parrafos:
        piezas = _fragmentar_bloque(parrafo)
        if not piezas:
            raise RuntimeError(f"El documento {documento_id} produjo un párrafo vacío.")
        textos.extend(piezas)

    _comprobar_cobertura(documento_id, contenido, textos)
    return [
        Fragmento(
            empresa_id=empresa_id,
            nombre_empresa=nombre_empresa,
            documento_id=documento_id,
            titulo=titulo,
            numero=numero,
            texto=texto,
        )
        for numero, texto in enumerate(textos, start=1)
    ]


def main() -> None:
    """Carga los JSON, fragmenta y muestra el resumen de la prueba."""
    empresas = cargar_empresas()
    fragmentos = fragmentar_empresas(empresas)
    _mostrar_resumen(empresas, fragmentos)


def _rutas_json(directorio: Path) -> list[Path]:
    """Comprueba que ``data/`` contiene exactamente los tres archivos conocidos."""
    if not directorio.is_dir():
        raise FileNotFoundError(f"No existe el directorio de datos: {directorio}")
    encontrados = {ruta.name for ruta in directorio.glob("*.json")}
    esperados = set(ARCHIVOS_JSON)
    if encontrados != esperados:
        faltan = sorted(esperados - encontrados)
        sobran = sorted(encontrados - esperados)
        raise FileNotFoundError(
            "El directorio data/ debe contener exactamente "
            f"{', '.join(ARCHIVOS_JSON)}. Faltan: {faltan or '-'}. Sobran: {sobran or '-'}."
        )
    return [directorio / nombre for nombre in ARCHIVOS_JSON]


def _leer_empresa(ruta: Path) -> EmpresaFuente:
    """Carga un JSON y valida sus campos obligatorios."""
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{ruta.name} no contiene JSON válido: {error}") from error
    if not isinstance(datos, dict):
        raise ValueError(f"{ruta.name} debe contener un objeto JSON.")
    _exigir_claves(datos, CAMPOS_EMPRESA, ruta.name)

    empresa_id = datos["empresa_id"]
    if type(empresa_id) is not int or empresa_id <= 0:
        raise ValueError(f"{ruta.name}: empresa_id debe ser un entero positivo.")
    nombre = _texto_obligatorio(datos["nombre"], f"{ruta.name}: nombre")

    documentos = datos["documentos"]
    if not isinstance(documentos, list) or not documentos:
        raise ValueError(f"{ruta.name}: documentos debe ser una lista no vacía.")

    documentos_validos: list[dict[str, str]] = []
    for indice, documento in enumerate(documentos):
        contexto = f"{ruta.name}: documentos[{indice}]"
        if not isinstance(documento, dict):
            raise ValueError(f"{contexto} debe ser un objeto.")
        _exigir_claves(documento, CAMPOS_DOCUMENTO, contexto)
        documentos_validos.append(
            {
                "id": _texto_obligatorio(documento["id"], f"{contexto}: id"),
                "titulo": _texto_obligatorio(documento["titulo"], f"{contexto}: titulo"),
                "contenido": _texto_obligatorio(documento["contenido"], f"{contexto}: contenido"),
            }
        )
    return {
        "empresa_id": empresa_id,
        "nombre": nombre,
        "documentos": documentos_validos,
    }


def _validar_identificadores_unicos(empresas: list[EmpresaFuente]) -> None:
    """Impide que dos empresas o dos documentos compartan identificador."""
    empresas_vistas: set[int] = set()
    nombres_vistos: set[str] = set()
    documentos_vistos: set[str] = set()
    for empresa in empresas:
        empresa_id = int(empresa["empresa_id"])
        nombre = str(empresa["nombre"])
        if empresa_id in empresas_vistas:
            raise ValueError(f"empresa_id duplicado: {empresa_id}")
        if nombre in nombres_vistos:
            raise ValueError(f"nombre de empresa duplicado: {nombre}")
        empresas_vistas.add(empresa_id)
        nombres_vistos.add(nombre)
        for documento in empresa["documentos"]:
            documento_id = documento["id"]
            if documento_id in documentos_vistos:
                raise ValueError(f"identificador de documento duplicado: {documento_id}")
            documentos_vistos.add(documento_id)


def _exigir_claves(datos: dict[str, object], esperadas: frozenset[str], contexto: str) -> None:
    """Rechaza objetos a los que les falta o les sobra algún campo inspeccionado."""
    claves = set(datos)
    if claves == esperadas:
        return
    faltan = sorted(esperadas - claves)
    sobran = sorted(claves - esperadas)
    raise ValueError(f"{contexto}: claves inválidas. Faltan: {faltan or '-'}. Sobran: {sobran or '-'}.")


def _texto_obligatorio(valor: object, contexto: str) -> str:
    """Exige una cadena con contenido visible."""
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError(f"{contexto} debe ser una cadena no vacía.")
    return valor.strip()


def _parrafos(contenido: str) -> list[str]:
    """Separa el contenido por líneas en blanco, que es como están escritos los JSON."""
    normalizado = contenido.replace("\r\n", "\n").replace("\r", "\n")
    return [parrafo.strip() for parrafo in re.split(r"\n\s*\n", normalizado) if parrafo.strip()]


def _fragmentar_bloque(texto: str, nivel: int = 0) -> list[str]:
    """Trocea un bloque hasta que cada parte quepa en el modelo.

    El orden de corte es frases, cláusulas, comas, palabras y, solo como
    último recurso, caracteres. Cada parte emitida se vuelve a medir con el
    tokenizador; no hay truncamiento silencioso.
    """
    limpio = texto.strip()
    if not limpio:
        return []
    if _cabe(limpio):
        return [limpio]
    if nivel >= len(_PATRONES):
        return _dividir_por_caracteres(limpio)

    piezas = _partir(limpio, _PATRONES[nivel])
    if len(piezas) <= 1:
        return _fragmentar_bloque(limpio, nivel + 1)

    resultado: list[str] = []
    for grupo in _empaquetar(piezas):
        if _cabe(grupo):
            resultado.append(grupo)
        else:
            resultado.extend(_fragmentar_bloque(grupo, nivel + 1))
    if not resultado:
        raise RuntimeError("La subdivisión no produjo fragmentos.")
    return resultado


def _partir(texto: str, patron: re.Pattern[str]) -> list[str]:
    """Corta por el patrón conservando el orden y el texto de cada tramo."""
    cortes = [0]
    for coincidencia in patron.finditer(texto):
        posicion = coincidencia.end()
        if cortes[-1] < posicion < len(texto):
            cortes.append(posicion)
    cortes.append(len(texto))
    # No se descartan tramos en blanco interiores: si se omitieran, las piezas
    # dejarían de ser contiguas y al unirlas se pegarían palabras distintas.
    return [texto[inicio:fin] for inicio, fin in zip(cortes, cortes[1:]) if inicio < fin]


def _empaquetar(piezas: list[str]) -> list[str]:
    """Agrupa piezas contiguas mientras el grupo siga cabiendo en el modelo."""
    grupos: list[str] = []
    actual = ""
    for pieza in piezas:
        propuesta = pieza if not actual else f"{actual}{pieza}"
        if _cabe(propuesta.strip()):
            actual = propuesta
            continue
        if actual.strip():
            grupos.append(actual.strip())
        actual = pieza
    if actual.strip():
        grupos.append(actual.strip())
    return grupos


def _dividir_por_caracteres(texto: str) -> list[str]:
    """Reparte un texto sin espacios en prefijos que caben, sin descartar caracteres."""
    resto = texto.strip()
    partes: list[str] = []
    while resto:
        if _cabe(resto):
            partes.append(resto)
            break
        if not _cabe(resto[0]):
            raise RuntimeError(
                "Un carácter supera el límite del modelo. No se trunca el texto."
            )
        corte = _mayor_prefijo_que_cabe(resto)
        if corte <= 0 or corte >= len(resto):
            raise RuntimeError("No se pudo subdividir un texto demasiado largo sin truncarlo.")
        partes.append(resto[:corte].strip())
        resto = resto[corte:].strip()
    return [parte for parte in partes if parte]


def _mayor_prefijo_que_cabe(texto: str) -> int:
    """Busca el prefijo más largo que el tokenizador acepta."""
    bajo = 1
    alto = len(texto) - 1
    mejor = 0
    while bajo <= alto:
        medio = (bajo + alto) // 2
        if _cabe(texto[:medio]):
            mejor = medio
            bajo = medio + 1
        else:
            alto = medio - 1
    espacio = texto.rfind(" ", 0, mejor)
    if espacio > 0 and _cabe(texto[:espacio]):
        return espacio
    return mejor


def _cabe(texto: str) -> bool:
    """Indica si el texto cabe, con los tokens especiales incluidos."""
    if not texto.strip():
        return False
    modelo = _obtener_modelo()
    return _contar_tokens(modelo, texto) <= _longitud_maxima(modelo)


def _comprobar_cobertura(documento_id: str, contenido: str, textos: list[str]) -> None:
    """Comprueba que no se pierde texto ni se pegan palabras de frases distintas.

    Una subdivisión por caracteres puede aumentar el número de palabras, pero
    no puede reducirlas: eso indicaría que dos palabras se han pegado.
    """
    originales = contenido.split()
    obtenidas = " ".join(textos).split()
    if len(obtenidas) < len(originales) or "".join(originales) != "".join(obtenidas):
        raise RuntimeError(
            f"La fragmentación de {documento_id} perdió o alteró texto. "
            "No se ha truncado el documento."
        )


def _validar_fragmentos(fragmentos: list[Fragmento]) -> None:
    """Revisa orden, aislamiento y longitud antes de devolver el resultado."""
    if not fragmentos:
        raise RuntimeError("No se ha generado ningún fragmento.")
    anterior: tuple[int, str] | None = None
    numeros: dict[tuple[int, str], int] = {}
    for fragmento in fragmentos:
        if not fragmento.texto.strip():
            raise RuntimeError("Hay un fragmento vacío.")
        if not _cabe(fragmento.texto):
            raise RuntimeError(
                f"{fragmento.documento_id}#{fragmento.numero} supera el límite del modelo. "
                "No se trunca."
            )
        clave = (fragmento.empresa_id, fragmento.documento_id)
        if anterior is not None and clave != anterior and clave in numeros:
            raise RuntimeError(
                f"Los fragmentos de {fragmento.documento_id} quedaron mezclados con otro documento."
            )
        esperado = numeros.get(clave, 0) + 1
        if fragmento.numero != esperado:
            raise RuntimeError(
                f"Numeración no determinista en {fragmento.documento_id}: "
                f"se esperaba {esperado} y llegó {fragmento.numero}."
            )
        numeros[clave] = esperado
        anterior = clave


def _mostrar_resumen(empresas: list[EmpresaFuente], fragmentos: list[Fragmento]) -> None:
    """Muestra los conteos pedidos y un fragmento de ejemplo por empresa."""
    modelo = _obtener_modelo()
    limite = _longitud_maxima(modelo)
    longitudes = [_contar_tokens(modelo, fragmento.texto) for fragmento in fragmentos]
    maxima = max(longitudes)
    if maxima > limite:
        raise RuntimeError(
            f"La longitud máxima observada ({maxima}) supera el límite del modelo ({limite})."
        )

    total_documentos = sum(len(empresa["documentos"]) for empresa in empresas)
    print(f"Número de empresas: {len(empresas)}")
    print(f"Número total de documentos: {total_documentos}")
    print(f"Número total de fragmentos: {len(fragmentos)}")
    print(f"Longitud máxima de los fragmentos en tokens: {maxima}")
    print(f"Límite del modelo en tokens: {limite}")

    for empresa in empresas:
        empresa_id = int(empresa["empresa_id"])
        nombre = str(empresa["nombre"])
        documentos = empresa["documentos"]
        propios = [fragmento for fragmento in fragmentos if fragmento.empresa_id == empresa_id]
        if not propios:
            raise RuntimeError(f"{nombre} no tiene fragmentos.")
        if any(fragmento.nombre_empresa != nombre for fragmento in propios):
            raise RuntimeError(f"Hay fragmentos de {nombre} con otro nombre de empresa.")
        ejemplo = propios[0]
        print(f"Empresa: {nombre} (empresa_id={empresa_id})")
        print(f"  Número de documentos: {len(documentos)}")
        print(f"  Número de fragmentos: {len(propios)}")
        print(
            "  Ejemplo: "
            f"{ejemplo.documento_id} #{ejemplo.numero} — {ejemplo.titulo}"
        )
        print(f"  Texto: {ejemplo.texto}")


if __name__ == "__main__":
    main()
