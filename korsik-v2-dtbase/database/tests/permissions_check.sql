\set ON_ERROR_STOP on
-- Execute through the actual korsik_app login after grants.sql, not as owner.
BEGIN;
DO $$
BEGIN
 IF current_user<>'korsik_app' THEN RAISE EXCEPTION 'Connect as korsik_app for this test'; END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=current_user AND
  (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
  RAISE EXCEPTION 'Application role has excessive cluster privileges'; END IF;
 IF has_schema_privilege(current_user,'korsik','CREATE') OR
  has_table_privilege(current_user,'korsik.employees','DELETE') OR
  has_table_privilege(current_user,'korsik.employees','TRUNCATE') OR
  has_table_privilege(current_user,'korsik.audit_log','UPDATE') OR
  has_table_privilege(current_user,'korsik.schema_migrations','SELECT') THEN
  RAISE EXCEPTION 'Application role has excessive object privileges'; END IF;
 IF NOT has_schema_privilege(current_user,'korsik','USAGE') OR
  NOT has_table_privilege(current_user,'korsik.messages','SELECT') OR
  NOT has_table_privilege(current_user,'korsik.messages','INSERT') OR
  NOT has_function_privilege(current_user,'korsik.prepare_due_events(integer)','EXECUTE') THEN
  RAISE EXCEPTION 'Required application privilege missing'; END IF;
 RAISE NOTICE 'Application object/role privilege checks passed';
END $$;
SELECT * FROM korsik.hr_dashboard LIMIT 2;
ROLLBACK;
