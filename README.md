# thepower-rag-pgvector

Asistente de IA multiempresa basado en RAG (Retrieval-Augmented Generation), desarrollado como proyecto académico del Máster AI Engineer de thePower.

El sistema responderá preguntas a partir de la documentación propia de cada empresa. PostgreSQL y pgvector almacenarán los fragmentos y sus embeddings. El aislamiento entre clientes se garantizará en base de datos mediante Row Level Security (RLS), de modo que una consulta de una empresa no pueda leer documentos de otra.

Esta primera fase contiene únicamente el scaffolding. No incluye todavía la lógica RAG, el esquema de tablas ni las políticas RLS.

## Objetivos

- Indexar documentos de varias empresas en un mismo almacén, sin mezclar su información.
- Generar embeddings de 384 dimensiones con `paraphrase-multilingual-MiniLM-L12-v2`.
- Recuperar contexto relevante con búsqueda por similitud en pgvector.
- Generar la respuesta con Gemini 3.1 Flash-Lite a partir solo del contexto recuperado.
- Separar las credenciales administrativas de las credenciales de la aplicación, para que el rol de runtime quede sujeto a RLS.
- Dejar el diseño preparado para pruebas automatizadas con Pytest.

## Arquitectura prevista

```text
Empresa A / Empresa B
        |
        v
Aplicación Python (src/)
        |
        +--> Embeddings locales (Sentence Transformers, 384 dimensiones)
        |
        +--> PostgreSQL 16 + pgvector
        |       - Rol administrador: migraciones y mantenimiento
        |       - Rol de aplicación: consultas de runtime, sujeto a RLS
        |       - Cada fila queda asociada a una empresa
        |
        +--> Gemini 3.1 Flash-Lite
                - Recibe la pregunta y el contexto ya filtrado
                - No accede directamente a la base de datos
```

Flujo previsto, todavía no implementado:

1. Identificar la empresa activa y fijar el contexto de sesión que usará RLS.
2. Trocear e indexar sus documentos con el modelo de embeddings.
3. Ante una pregunta, calcular su embedding y buscar fragmentos similares.
4. Dejar que RLS descarte cualquier fila de otra empresa, además del filtro explícito por empresa.
5. Enviar a Gemini únicamente la pregunta y los fragmentos autorizados.
6. Registrar evidencias de aislamiento en `evidencias/` cuando se ejecuten las pruebas del proyecto.

## Tecnologías

| Componente | Elección |
| --- | --- |
| Lenguaje | Python 3.11 o superior |
| Base de datos | PostgreSQL 16 |
| Búsqueda vectorial | pgvector |
| Contenedores | Docker Compose |
| Acceso a datos | Psycopg 3 |
| Embeddings | Sentence Transformers, `paraphrase-multilingual-MiniLM-L12-v2` |
| Generación | Google Gen AI SDK, `gemini-3.1-flash-lite` |
| Configuración | python-dotenv |
| Pruebas | Pytest |
| Aislamiento | Row Level Security |

## Estructura del proyecto

```text
thepower-rag-pgvector/
├── .env.example          # Plantilla de configuración, sin secretos reales
├── .gitignore
├── docker-compose.yml    # PostgreSQL 16 con pgvector
├── README.md
├── requirements.txt
├── data/                 # Datos locales no versionados
├── evidencias/           # Capturas y resultados de las pruebas de aislamiento
├── sql/                  # Futuras migraciones y políticas RLS
├── src/                  # Paquete de la aplicación
└── tests/                # Futuras pruebas Pytest
```

## Instalación inicial

Requisitos: Python 3.11 o superior, Docker y Docker Compose.

1. Copiar la plantilla de entorno y sustituir los valores ficticios:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Crear y activar un entorno virtual:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Instalar dependencias cuando se vaya a desarrollar la lógica:

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. Levantar PostgreSQL:

   ```powershell
   docker compose up -d
   docker compose ps
   ```

El servicio expone PostgreSQL en el puerto definido por `POSTGRES_PORT` (5432 por defecto) y conserva los datos en el volumen Docker `pgdata`. El healthcheck usa `pg_isready`.

No hace falta crear `.env` para revisar el repositorio. Sí hace falta, con valores propios y no compartidos, antes de arrancar el contenedor. `.env` no debe versionarse.

## Alcance de esta fase

Incluido:

- Estructura de directorios, configuración de ejemplo y servicio de base de datos.

Pendiente para fases posteriores:

- Esquema de empresas, documentos, fragmentos y embeddings.
- Políticas RLS y creación del rol de aplicación.
- Ingesta, recuperación y generación de respuestas.
- Pruebas de aislamiento y evidencias.

## Buenas prácticas

- El rol administrador solo se usa para migraciones y tareas de mantenimiento.
- La aplicación se conectará con `APP_DB_USER`, un rol sin privilegios de superusuario ni `BYPASSRLS`.
- No guardar claves de API, contraseñas ni volcados de documentos en el repositorio.
- Tratar el identificador de empresa como un dato de autorización, no como un filtro opcional de la interfaz.
