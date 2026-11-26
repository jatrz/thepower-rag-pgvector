-- Habilita pgvector en la base de datos actual.
-- IF NOT EXISTS hace la instrucción idempotente: si la extensión ya está
-- instalada, PostgreSQL no devuelve error al ejecutar el script de nuevo.
CREATE EXTENSION IF NOT EXISTS vector;

-- Comprueba que la extensión quedó registrada y muestra su versión.
-- Es una consulta de verificación: no modifica datos y puede repetirse.
SELECT extname, extversion
FROM pg_extension
WHERE extname = 'vector';
