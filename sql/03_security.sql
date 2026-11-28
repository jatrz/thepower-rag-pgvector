-- Aislamiento multiempresa de fragmentos mediante roles distintos y RLS.
-- Ejecutar con psql, conectado a rag_chatbot como rag_admin:
--   psql -h 127.0.0.1 -p 5434 -U rag_admin -d rag_chatbot -f sql/03_security.sql
--
-- No usar el puerto 5433 ni la base rag_multitenant: pertenecen a la instancia
-- anterior. La primera instrucción aborta si la base activa no es rag_chatbot,
-- antes de crear roles o cambiar permisos.
-- Los roles se crean sin contraseña. La autenticación por contraseña fallará
-- hasta configurarla fuera de este archivo, para no versionar secretos:
--   \password rag_empresa_1
-- No inserta empresas ni documentos y no crea índices.

\set ON_ERROR_STOP on

-- Comprueba la base de datos antes de crear roles, conceder permisos o activar RLS.
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

    IF to_regclass('public.empresas') IS NULL
        OR to_regclass('public.fragmentos') IS NULL THEN
        RAISE EXCEPTION 'Ejecute antes sql/02_schema.sql';
    END IF;
END
$$;

-- La creación condicional de roles se realiza antes de la transacción
-- de permisos y políticas RLS. Se utiliza \gexec, metaorden de psql,
-- para crear únicamente los roles que todavía no existen.
-- No se incluye PASSWORD: repetir el script tampoco borra una contraseña ya configurada.
SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT',
    'rag_empresa_1'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'rag_empresa_1'
)
\gexec

SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT',
    'rag_empresa_2'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'rag_empresa_2'
)
\gexec

SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT',
    'rag_empresa_3'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'rag_empresa_3'
)
\gexec

-- Refuerza los atributos en cada ejecución, también si el rol ya existía.
-- NOINHERIT evita heredar privilegios o políticas de otro rol.
-- Hornos Alba, Clínica Sonrisa y Ruta Norte se asocian después por política,
-- no por una variable de sesión que el usuario pueda modificar.
ALTER ROLE rag_empresa_1
    WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
ALTER ROLE rag_empresa_2
    WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
ALTER ROLE rag_empresa_3
    WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;

BEGIN;

-- Segunda barrera por si este bloque se ejecuta suelto: no modifica otra base.
DO $$
BEGIN
    IF current_database() <> 'rag_chatbot' THEN
        RAISE EXCEPTION
            'Ejecute este script en rag_chatbot, no en %',
            current_database();
    END IF;
END
$$;

-- Impide que una empresa adopte el rol de otra o el rol administrativo.
-- Sin membresía no puede usar SET ROLE ni heredar sus políticas.
REVOKE rag_admin FROM rag_empresa_1, rag_empresa_2, rag_empresa_3;
REVOKE rag_empresa_1 FROM rag_empresa_2, rag_empresa_3, rag_admin;
REVOKE rag_empresa_2 FROM rag_empresa_1, rag_empresa_3, rag_admin;
REVOKE rag_empresa_3 FROM rag_empresa_1, rag_empresa_2, rag_admin;

-- Retira permisos amplios antes de conceder solo los necesarios.
-- Así el script corrige una concesión previa de escritura o administración.
REVOKE ALL PRIVILEGES ON TABLE public.empresas FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.fragmentos FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE public.empresas_id_seq FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SEQUENCE public.fragmentos_id_seq FROM PUBLIC;

REVOKE ALL PRIVILEGES ON TABLE public.empresas
    FROM rag_empresa_1, rag_empresa_2, rag_empresa_3;
REVOKE ALL PRIVILEGES ON TABLE public.fragmentos
    FROM rag_empresa_1, rag_empresa_2, rag_empresa_3;
REVOKE ALL PRIVILEGES ON SEQUENCE public.empresas_id_seq
    FROM rag_empresa_1, rag_empresa_2, rag_empresa_3;
REVOKE ALL PRIVILEGES ON SEQUENCE public.fragmentos_id_seq
    FROM rag_empresa_1, rag_empresa_2, rag_empresa_3;
REVOKE CREATE ON SCHEMA public
    FROM PUBLIC, rag_empresa_1, rag_empresa_2, rag_empresa_3;

-- Conexión y lectura mínima. No se concede INSERT, UPDATE, DELETE ni TRUNCATE.
GRANT CONNECT ON DATABASE rag_chatbot
    TO rag_empresa_1, rag_empresa_2, rag_empresa_3;
GRANT USAGE ON SCHEMA public
    TO rag_empresa_1, rag_empresa_2, rag_empresa_3;
GRANT SELECT ON TABLE public.fragmentos
    TO rag_empresa_1, rag_empresa_2, rag_empresa_3;

-- El propietario rag_admin es superusuario y conserva la ingesta.
-- FORCE ROW LEVEL SECURITY aplica las políticas también a un propietario
-- que no sea superusuario. No crea una política USING (true) para rag_admin:
-- las políticas se heredan por membresía y esa política abriría una fuga.
ALTER TABLE public.fragmentos ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fragmentos FORCE ROW LEVEL SECURITY;

-- Cada política es de solo SELECT y nombra a un único rol.
-- La condición es constante: no depende de current_setting() ni de un parámetro
-- que la sesión pueda cambiar.
DROP POLICY IF EXISTS fragmentos_select_empresa_1 ON public.fragmentos;
CREATE POLICY fragmentos_select_empresa_1
    ON public.fragmentos
    AS PERMISSIVE
    FOR SELECT
    TO rag_empresa_1
    USING (empresa_id = 1);

DROP POLICY IF EXISTS fragmentos_select_empresa_2 ON public.fragmentos;
CREATE POLICY fragmentos_select_empresa_2
    ON public.fragmentos
    AS PERMISSIVE
    FOR SELECT
    TO rag_empresa_2
    USING (empresa_id = 2);

DROP POLICY IF EXISTS fragmentos_select_empresa_3 ON public.fragmentos;
CREATE POLICY fragmentos_select_empresa_3
    ON public.fragmentos
    AS PERMISSIVE
    FOR SELECT
    TO rag_empresa_3
    USING (empresa_id = 3);

-- Aborta si queda otra política. Una política TO PUBLIC, por ejemplo,
-- permitiría a todas las empresas leer filas no previstas.
DO $$
DECLARE
    politica_inesperada text;
BEGIN
    SELECT string_agg(polname, ', ' ORDER BY polname)
    INTO politica_inesperada
    FROM pg_policy
    WHERE polrelid = 'public.fragmentos'::regclass
      AND polname NOT IN (
          'fragmentos_select_empresa_1',
          'fragmentos_select_empresa_2',
          'fragmentos_select_empresa_3'
      );

    IF politica_inesperada IS NOT NULL THEN
        RAISE EXCEPTION
            'Hay políticas no previstas en public.fragmentos: %',
            politica_inesperada;
    END IF;
END
$$;

COMMIT;

-- Verificación de solo lectura. No modifica el catálogo.
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication,
       rolbypassrls, rolcanlogin, rolinherit
FROM pg_roles
WHERE rolname IN ('rag_empresa_1', 'rag_empresa_2', 'rag_empresa_3')
ORDER BY rolname;

SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname = 'fragmentos';

SELECT pol.polname,
       pg_get_expr(pol.polqual, pol.polrelid) AS using_expr,
       pol.polcmd,
       pol.polroles::regrole[] AS roles
FROM pg_policy AS pol
WHERE pol.polrelid = 'public.fragmentos'::regclass
ORDER BY pol.polname;
