"""Generación local de embeddings para el asistente RAG multiempresa.

El modelo se ejecuta solo en CPU. No se consulta PostgreSQL ni ninguna API
de embeddings: el vector se calcula con Sentence Transformers.
"""

from __future__ import annotations

import os
import threading

# Debe fijarse antes de importar Torch. Oculta cualquier GPU visible y evita
# que una build con CUDA inicialice dispositivos NVIDIA.
os.environ["CUDA_VISIBLE_DEVICES"] = ""

import numpy as np
from sentence_transformers import SentenceTransformer

# Identificador de Hugging Face. La primera ejecución puede descargar los pesos.
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Coincide con la columna fragmentos.embedding VECTOR(384).
EMBEDDING_DIM = 384

_DISPOSITIVO = "cpu"
_TAMANO_LOTE = 32
# model_max_length sin configurar puede valer un entero enorme. Esa cifra no
# es una longitud real del modelo y no debe usarse como límite.
_LONGITUD_SENTINELA = 100_000

_modelo: SentenceTransformer | None = None
# RLock: la carga y la inferencia comparten el mismo candado sin bloquearse
# si una llamada anida la otra.
_bloqueo = threading.RLock()


def generate_embedding(text: str) -> np.ndarray:
    """Genera el embedding normalizado de un único texto.

    Args:
        text: Texto no vacío que cabe en la longitud máxima del modelo.

    Returns:
        Vector ``float32`` de forma ``(384,)`` y norma L2 igual a 1.

    Raises:
        TypeError: Si ``text`` no es una cadena.
        ValueError: Si el texto está vacío o supera la longitud máxima.
        RuntimeError: Si el modelo no devuelve un vector válido.
    """
    if not isinstance(text, str):
        raise TypeError("text debe ser una cadena.")
    if not text.strip():
        raise ValueError("El texto está vacío.")
    vectores = generate_embeddings([text])
    return vectores[0]


def generate_embeddings(texts: list[str]) -> np.ndarray:
    """Genera embeddings normalizados para un lote de textos.

    Los textos se validan antes de llamar al modelo. Si uno no cabe en la
    ventana, se rechaza el lote completo: no se trunca ningún texto.

    Args:
        texts: Lista no vacía de cadenas.

    Returns:
        Matriz ``float32`` de forma ``(n, 384)``. Cada fila tiene norma L2 1.

    Raises:
        TypeError: Si ``texts`` no es una lista de cadenas.
        ValueError: Si la lista está vacía, hay un texto vacío o alguno
            supera la longitud máxima, contando los tokens especiales.
        RuntimeError: Si el modelo no devuelve vectores válidos.
    """
    _validar_textos(texts)
    # La carga queda fuera del bloque de inferencia. _obtener_modelo ya
    # protege su propia inicialización con el mismo candado reentrante.
    modelo = _obtener_modelo()
    with _bloqueo:
        _rechazar_textos_demasiado_largos(modelo, texts)
        # truncation=False impide que el tokenizador recorte si la validación
        # anterior no se hubiera ejecutado. No se añade prompt: este modelo
        # no fue entrenado con prefijos de consulta o documento.
        crudos = modelo.encode(
            texts,
            batch_size=_TAMANO_LOTE,
            show_progress_bar=False,
            convert_to_numpy=True,
            convert_to_tensor=False,
            normalize_embeddings=False,
            device=_DISPOSITIVO,
            precision="float32",
            processing_kwargs={"text": {"truncation": False}},
        )
    return _normalizar_y_validar(crudos, len(texts))


def main() -> None:
    """Genera un embedding de prueba y muestra sus características."""
    texto = "La política de vacaciones establece el descanso anual del personal."
    modelo = _obtener_modelo()
    vector = generate_embedding(texto)
    norma = float(np.linalg.norm(vector))
    print(f"Modelo: {MODEL_NAME}")
    print(f"Dispositivo: {modelo.device}")
    print(f"Dimensiones: {vector.shape[0]}")
    print(f"Tipo de datos: {vector.dtype}")
    print(f"Norma L2: {norma:.6f}")


def _obtener_modelo() -> SentenceTransformer:
    """Carga el modelo la primera vez y reutiliza la misma instancia."""
    global _modelo
    if _modelo is not None:
        return _modelo
    with _bloqueo:
        if _modelo is None:
            _modelo = _crear_modelo()
        return _modelo


def _crear_modelo() -> SentenceTransformer:
    """Carga el modelo en CPU y comprueba que produce 384 dimensiones."""
    modelo = SentenceTransformer(
        MODEL_NAME,
        device=_DISPOSITIVO,
        backend="torch",
        local_files_only=False,
    )
    modelo.to(_DISPOSITIVO)
    modelo.eval()

    dispositivo = getattr(modelo.device, "type", str(modelo.device))
    if dispositivo != _DISPOSITIVO:
        raise RuntimeError(
            f"El modelo debe ejecutarse exclusivamente en CPU; dispositivo actual: {modelo.device}."
        )

    dimension = modelo.get_embedding_dimension()
    if dimension != EMBEDDING_DIM:
        raise RuntimeError(
            f"El modelo {MODEL_NAME} devuelve {dimension} dimensiones; "
            f"se esperaban {EMBEDDING_DIM}."
        )

    # Fuerza la lectura del límite real antes de aceptar textos.
    _longitud_maxima(modelo)
    return modelo


def _validar_textos(texts: list[str]) -> None:
    """Rechaza listas mal formadas y textos vacíos o en blanco."""
    if not isinstance(texts, list):
        raise TypeError("texts debe ser una lista de cadenas.")
    if not texts:
        raise ValueError("La lista de textos no puede estar vacía.")
    for indice, texto in enumerate(texts):
        if not isinstance(texto, str):
            raise TypeError(f"El elemento de la posición {indice} no es una cadena.")
        if not texto.strip():
            raise ValueError(f"{_referencia_texto(indice, len(texts))} está vacío.")


def _longitud_maxima(modelo: SentenceTransformer) -> int:
    """Devuelve la longitud máxima de secuencia admitida por el modelo.

    Esa cifra incluye los tokens especiales. No se usa el valor centinela
    que algunos tokenizadores publican cuando no tienen un máximo real.
    """
    longitud = modelo.max_seq_length
    if isinstance(longitud, int) and 0 < longitud <= _LONGITUD_SENTINELA:
        return longitud

    transformador = modelo.transformers_model
    config = getattr(transformador, "config", None)
    posiciones = getattr(config, "max_position_embeddings", None)
    if isinstance(posiciones, int) and 0 < posiciones <= _LONGITUD_SENTINELA:
        return posiciones

    raise RuntimeError(
        "No se pudo determinar la longitud máxima de secuencia del modelo. "
        "Sin ese límite no es seguro generar embeddings."
    )


def _contar_tokens(modelo: SentenceTransformer, texto: str) -> int:
    """Cuenta tokens con el tokenizador del modelo, incluidos los especiales."""
    codificacion = modelo.tokenizer(
        texto,
        add_special_tokens=True,
        truncation=False,
        padding=False,
        return_attention_mask=False,
    )
    identificadores = codificacion["input_ids"]
    if hasattr(identificadores, "shape"):
        return int(identificadores.shape[-1])
    if identificadores and isinstance(identificadores[0], list):
        return len(identificadores[0])
    return len(identificadores)


def _rechazar_textos_demasiado_largos(
    modelo: SentenceTransformer,
    texts: list[str],
) -> None:
    """Rechaza textos que el modelo tendría que truncar.

    La comparación usa la misma tokenización que la inferencia, con los
    tokens especiales ya incluidos. Así un texto que ocupa exactamente el
    máximo se acepta y uno que lo supera no se recorta en silencio.
    """
    longitud_maxima = _longitud_maxima(modelo)
    tokens_especiales = modelo.tokenizer.num_special_tokens_to_add(pair=False)
    for indice, texto in enumerate(texts):
        num_tokens = _contar_tokens(modelo, texto)
        if num_tokens > longitud_maxima:
            raise ValueError(
                f"{_referencia_texto(indice, len(texts))} ocupa {num_tokens} tokens, "
                f"incluidos {tokens_especiales} tokens especiales, y el modelo "
                f"admite como máximo {longitud_maxima}. No se trunca el texto."
            )


def _referencia_texto(indice: int, cantidad: int) -> str:
    """Describe el texto afectado sin hablar de posición si el lote tiene uno."""
    if cantidad == 1:
        return "El texto"
    return f"El texto de la posición {indice}"


def _normalizar_y_validar(crudos: object, cantidad: int) -> np.ndarray:
    """Convierte la salida a ``float32``, la normaliza y comprueba el contrato."""
    vectores = np.asarray(crudos, dtype=np.float32)
    if vectores.shape != (cantidad, EMBEDDING_DIM):
        raise RuntimeError(
            f"Se esperaban embeddings con forma ({cantidad}, {EMBEDDING_DIM}); "
            f"se obtuvo {vectores.shape}."
        )
    if not np.isfinite(vectores).all():
        raise RuntimeError("El modelo devolvió valores no finitos.")

    normas = np.linalg.norm(vectores, axis=1, keepdims=True)
    if not np.isfinite(normas).all() or np.any(normas == 0):
        raise RuntimeError("No se puede normalizar un embedding de norma nula o no finita.")

    normalizados = (vectores / normas).astype(np.float32, copy=False)
    if not np.isfinite(normalizados).all():
        raise RuntimeError("La normalización produjo valores no finitos.")
    if normalizados.shape[1] != EMBEDDING_DIM:
        raise RuntimeError(
            f"Cada embedding debe tener {EMBEDDING_DIM} dimensiones; "
            f"se obtuvieron {normalizados.shape[1]}."
        )
    return normalizados


if __name__ == "__main__":
    main()
