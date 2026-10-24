\set ON_ERROR_STOP on
BEGIN;
GRANT USAGE ON SCHEMA korsik TO korsik_app;
GRANT SELECT ON ALL TABLES IN SCHEMA korsik TO korsik_app;
GRANT INSERT,UPDATE ON korsik.employees,korsik.invitations,korsik.plan_templates,
 korsik.plan_versions,korsik.template_events,korsik.employee_plans,korsik.plan_events,
 korsik.max_inbox,korsik.messages,korsik.hr_cases,korsik.outbox,korsik.documents,
 korsik.document_versions,korsik.document_chunks,korsik.event_answers,
 korsik.pending_interactions,korsik.message_analysis TO korsik_app;
GRANT INSERT ON korsik.answer_sources,korsik.message_analysis,korsik.audit_log TO korsik_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA korsik TO korsik_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA korsik TO korsik_app;
REVOKE ALL ON korsik.schema_migrations FROM korsik_app;
REVOKE UPDATE,DELETE,TRUNCATE ON korsik.audit_log,korsik.answer_sources FROM korsik_app;
-- No DELETE / TRUNCATE, no schema CREATE, no owner role, no future automatic grants.
COMMIT;
