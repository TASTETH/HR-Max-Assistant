BEGIN;
SELECT pg_advisory_xact_lock(hashtext('korsik_router.install'));
DO $$ BEGIN
 IF to_regclass('korsik.max_inbox') IS NULL OR
    to_regprocedure('korsik.open_hr_case(bigint,bigint,text,text)') IS NULL THEN
  RAISE EXCEPTION 'Install the supplied korsik database schema and operations first';
 END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS korsik_router;

CREATE TABLE IF NOT EXISTS korsik_router.profiles (
 employee_id bigint PRIMARY KEY REFERENCES korsik.employees(id),
 site_code text CHECK (site_code ~ '^[A-Za-z0-9_-]{1,64}$'),
 preferred_address text NOT NULL DEFAULT 'formal' CHECK (preferred_address IN ('formal','informal')),
 revision bigint NOT NULL DEFAULT 0 CHECK (revision>=0),
 input_mode jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(input_mode)='object' AND octet_length(input_mode::text)<=8192)
);
CREATE TABLE IF NOT EXISTS korsik_router.hr_contacts (
 hr_id bigint PRIMARY KEY REFERENCES korsik.hr_users(id),
 url text NOT NULL CHECK (url ~ '^https?://' AND length(url)<=2048)
);
CREATE TABLE IF NOT EXISTS korsik_router.question_configs (
 event_id bigint PRIMARY KEY REFERENCES korsik.plan_events(id),
 config jsonb NOT NULL CHECK (jsonb_typeof(config)='object' AND octet_length(config::text)<=16384)
);
CREATE TABLE IF NOT EXISTS korsik_router.answers (
 event_id bigint PRIMARY KEY,
 employee_id bigint NOT NULL,
 revision integer NOT NULL DEFAULT 0 CHECK (revision>=0),
 stage text NOT NULL DEFAULT 'draft' CHECK (stage IN ('draft','awaiting_comment','completed')),
 selected text[] NOT NULL DEFAULT '{}' CHECK (cardinality(selected)<=20),
 score smallint CHECK (score BETWEEN 1 AND 10),
 comment text CHECK (length(comment)<=3000),
 root_message_id bigint,
 FOREIGN KEY(event_id,employee_id) REFERENCES korsik.plan_events(id,employee_id),
 FOREIGN KEY(root_message_id,employee_id) REFERENCES korsik.messages(id,employee_id)
);
CREATE TABLE IF NOT EXISTS korsik_router.pending (
 id uuid PRIMARY KEY,
 employee_id bigint NOT NULL REFERENCES korsik.employees(id),
 message_id bigint NOT NULL,
 targets jsonb NOT NULL CHECK (jsonb_typeof(targets)='array' AND octet_length(targets::text)<=16384),
 resolved_at timestamptz,
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '1 day',
 FOREIGN KEY(message_id,employee_id) REFERENCES korsik.messages(id,employee_id)
);
CREATE TABLE IF NOT EXISTS korsik_router.actions (
 token_hash bytea PRIMARY KEY CHECK (octet_length(token_hash)=32),
 employee_id bigint NOT NULL REFERENCES korsik.employees(id),
 action jsonb NOT NULL CHECK (jsonb_typeof(action)='object' AND octet_length(action::text)<=4096),
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '7 days'
);
CREATE TABLE IF NOT EXISTS korsik_router.envelopes (
 message_id bigint PRIMARY KEY REFERENCES korsik.messages(id),
 data jsonb NOT NULL CHECK (jsonb_typeof(data)='object' AND octet_length(data::text)<=65536)
);
CREATE TABLE IF NOT EXISTS korsik_router.materials (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 code text NOT NULL CHECK (code ~ '^[a-z_]{1,40}$'),
 site_code text,
 body text NOT NULL CHECK (btrim(body)<>'' AND length(body)<=12000),
 attachments jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(attachments)='array'),
 enabled boolean NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX IF NOT EXISTS router_material_scope
 ON korsik_router.materials(code,coalesce(site_code,''));
CREATE TABLE IF NOT EXISTS korsik_router.case_details (
 case_id bigint PRIMARY KEY REFERENCES korsik.hr_cases(id),
 priority text NOT NULL DEFAULT 'low' CHECK (priority IN ('low','medium','high')),
 category text NOT NULL DEFAULT 'general_hr_request',
 source_message_id bigint REFERENCES korsik.messages(id)
);
CREATE TABLE IF NOT EXISTS korsik_router.hr_jobs (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 deduplication_key text NOT NULL UNIQUE,
 employee_id bigint NOT NULL REFERENCES korsik.employees(id),
 message_id bigint NOT NULL,
 root_message_id bigint NOT NULL,
 input jsonb NOT NULL CHECK (jsonb_typeof(input)='object' AND octet_length(input::text)<=24000),
 continue_with_rag boolean NOT NULL DEFAULT false,
 status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','dead')),
 attempts integer NOT NULL DEFAULT 0 CHECK (attempts>=0),
 generation bigint NOT NULL DEFAULT 0 CHECK (generation>=0),
 lease_until timestamptz,
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 last_error text CHECK (length(last_error)<=100),
 CHECK ((status='running')=(lease_until IS NOT NULL)),
 FOREIGN KEY(message_id,employee_id) REFERENCES korsik.messages(id,employee_id),
 FOREIGN KEY(root_message_id,employee_id) REFERENCES korsik.messages(id,employee_id)
);
CREATE TABLE IF NOT EXISTS korsik_router.hr_results (
 job_id bigint NOT NULL REFERENCES korsik_router.hr_jobs(id),
 generation bigint NOT NULL,
 model_version text NOT NULL,
 result jsonb NOT NULL CHECK (jsonb_typeof(result)='object'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(job_id,generation)
);
CREATE INDEX IF NOT EXISTS router_jobs_pending ON korsik_router.hr_jobs(next_attempt_at,id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS router_jobs_expired ON korsik_router.hr_jobs(lease_until) WHERE status='running';
CREATE INDEX IF NOT EXISTS router_pending_employee ON korsik_router.pending(employee_id) WHERE resolved_at IS NULL;

CREATE OR REPLACE FUNCTION korsik_router.guard_question_config() RETURNS trigger
LANGUAGE plpgsql SET search_path=korsik_router,korsik,pg_catalog AS $$
DECLARE target bigint; state text;
BEGIN
 IF TG_OP='DELETE' THEN target:=OLD.event_id; ELSE target:=NEW.event_id; END IF;
 IF TG_OP='UPDATE' AND NEW.event_id<>OLD.event_id THEN RAISE EXCEPTION 'Question identity is immutable'; END IF;
 SELECT status INTO STRICT state FROM korsik.plan_events WHERE id=target FOR UPDATE;
 IF state<>'scheduled' THEN RAISE EXCEPTION 'Only a never-queued question may be configured'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS question_config_history ON korsik_router.question_configs;
CREATE TRIGGER question_config_history BEFORE INSERT OR UPDATE OR DELETE ON korsik_router.question_configs
 FOR EACH ROW EXECUTE FUNCTION korsik_router.guard_question_config();

DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='korsik_app') THEN
  GRANT USAGE ON SCHEMA korsik_router TO korsik_app;
  GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA korsik_router TO korsik_app;
  GRANT USAGE ON ALL SEQUENCES IN SCHEMA korsik_router TO korsik_app;
  REVOKE UPDATE ON korsik_router.hr_results FROM korsik_app;
 END IF;
END $$;
REVOKE ALL ON SCHEMA korsik_router FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA korsik_router FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='korsik_app') THEN
  GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA korsik_router TO korsik_app;
 END IF;
END $$;
COMMIT;
