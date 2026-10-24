\set ON_ERROR_STOP on
BEGIN;
-- Run as a cluster security administrator. No passwords in this file.
-- Intentionally fails if these role names already exist: review existing grants first.
CREATE ROLE korsik_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE korsik_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE korsik_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT korsik_owner TO korsik_migrator;
-- In psql supply: -v dbname=korsik_demo
GRANT CONNECT ON DATABASE :"dbname" TO korsik_migrator,korsik_app;
GRANT CREATE ON DATABASE :"dbname" TO korsik_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER ROLE korsik_app SET search_path=korsik,pg_catalog;
ALTER ROLE korsik_app SET timezone='UTC';
ALTER ROLE korsik_app SET statement_timeout='15s';
ALTER ROLE korsik_app SET lock_timeout='3s';
ALTER ROLE korsik_app SET idle_in_transaction_session_timeout='15s';
-- install.sql must subsequently run with SET ROLE korsik_owner.
COMMIT;
