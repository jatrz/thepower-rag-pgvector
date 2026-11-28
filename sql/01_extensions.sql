-- Habilita pgvector en la base de datos actual.
-- Ejecutar con psql, conectado a rag_chatbot. ON_ERROR_STOP impide continuar
-- si la guarda rechaza otra base.
-- IF NOT EXISTS hace la instrucción idempotente: si la extensión ya está
-- instalada, PostgreSQL no devuelve error al ejecutar el script de nuevo.

\set ON_ERROR_STOP on

-- Aborta antes de crear la extensión si la base activa no es rag_chatbot.
DO $$
BEGIN
    IF current_database() = 'rag_multitenant' THEN
        RAISE EXCEPTION
            'Rechazado: rag_multitenant pertenece a la infraestructura anterior. Este script solo puede ejecutarse en rag_chatbot.';
    END IF;

    IF current_database() <> 'rag_chatbot' THEN
        RAISE EXCEPTION
            'Ejecute este script en rag_chatbot, no en %',
            current_database();
    END IF;
END
$$;

CREATE EXTENSION IF NOT EXISTS vector;

-- Comprueba que la extensión quedó registrada y muestra su versión.
-- Es una consulta de verificación: no modifica datos y puede repetirse.
SELECT extname, extversion
FROM pg_extension
WHERE extname = 'vector';
