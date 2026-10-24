\set ON_ERROR_STOP on
BEGIN;
SET LOCAL TIME ZONE 'UTC';
DO $$ BEGIN
 IF current_setting('server_version_num')::int < 170000 THEN
  RAISE EXCEPTION 'PostgreSQL 17 or newer is required';
 END IF;
END $$;
CREATE SCHEMA korsik;
SET LOCAL search_path = korsik, pg_catalog;

CREATE TABLE schema_migrations (
 version integer PRIMARY KEY,
 applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 description text NOT NULL
);
CREATE TABLE hr_users (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 auth_subject text NOT NULL UNIQUE,
 display_name text NOT NULL CHECK (btrim(display_name) <> ''),
 access_role text NOT NULL DEFAULT 'hr' CHECK (access_role IN ('hr','admin')),
 enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE org_units (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 code text NOT NULL UNIQUE CHECK (code ~ '^[A-Z0-9_-]+$'),
 name text NOT NULL CHECK (btrim(name) <> '')
);
CREATE TABLE employees (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 hr_id bigint NOT NULL REFERENCES hr_users(id) ON DELETE RESTRICT,
 org_unit_id bigint REFERENCES org_units(id) ON DELETE RESTRICT,
 external_employee_id varchar(100) UNIQUE,
 full_name text CHECK (btrim(full_name) <> ''),
 phone text CHECK (phone ~ '^\+[1-9][0-9]{7,14}$'),
 job_title text CHECK (btrim(job_title) <> ''),
 start_date date,
 timezone text NOT NULL DEFAULT 'Europe/Moscow',
 lifecycle text NOT NULL DEFAULT 'invited' CHECK (lifecycle IN
  ('invited','registering','hr_review','ready','adapting','completed','stopped')),
 registration_step text NOT NULL DEFAULT 'full_name' CHECK (registration_step IN
  ('full_name','phone','confirm','submitted')),
 registration_revision integer NOT NULL DEFAULT 0 CHECK (registration_revision >= 0),
 submitted_at timestamptz,
 hr_verified_at timestamptz,
 max_account_id text UNIQUE,
 max_chat_id text UNIQUE,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK ((max_account_id IS NULL) = (max_chat_id IS NULL)),
 CHECK (submitted_at IS NULL OR (full_name IS NOT NULL AND phone IS NOT NULL)),
 CHECK ((registration_step = 'submitted') = (submitted_at IS NOT NULL)),
 CHECK (hr_verified_at IS NULL OR (submitted_at IS NOT NULL AND
  org_unit_id IS NOT NULL AND job_title IS NOT NULL AND start_date IS NOT NULL)),
 CHECK (lifecycle NOT IN ('ready','adapting','completed') OR hr_verified_at IS NOT NULL)
);
CREATE TABLE invitations (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 employee_id bigint NOT NULL REFERENCES employees(id) ON DELETE RESTRICT,
 token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL,
 revoked_at timestamptz,
 activated_at timestamptz,
 activated_account_id text,
 CHECK (expires_at > created_at),
 CHECK ((activated_at IS NULL) = (activated_account_id IS NULL)),
 CHECK (NOT (revoked_at IS NOT NULL AND activated_at IS NOT NULL))
);
CREATE UNIQUE INDEX invitations_one_live ON invitations(employee_id)
 WHERE revoked_at IS NULL AND activated_at IS NULL;

CREATE TABLE plan_templates (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 code text NOT NULL UNIQUE,
 name text NOT NULL,
 suggested_job_title text,
 archived boolean NOT NULL DEFAULT false
);
CREATE TABLE plan_versions (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 template_id bigint NOT NULL REFERENCES plan_templates(id) ON DELETE RESTRICT,
 version_no integer NOT NULL CHECK (version_no > 0),
 published_at timestamptz,
 created_by bigint NOT NULL REFERENCES hr_users(id) ON DELETE RESTRICT,
 UNIQUE (template_id, version_no)
);
CREATE TABLE template_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 version_id bigint NOT NULL REFERENCES plan_versions(id) ON DELETE RESTRICT,
 code text NOT NULL,
 position integer NOT NULL CHECK (position > 0),
 kind text NOT NULL CHECK (kind IN ('info','task','rating','feedback')),
 body text NOT NULL CHECK (btrim(body) <> ''),
 offset_months integer NOT NULL DEFAULT 0 CHECK (offset_months BETWEEN 0 AND 24),
 offset_days integer NOT NULL DEFAULT 0 CHECK (offset_days BETWEEN -30 AND 730),
 local_time time NOT NULL DEFAULT '09:00',
 response_hours integer NOT NULL DEFAULT 72 CHECK (response_hours BETWEEN 1 AND 8760),
 required boolean NOT NULL DEFAULT true,
 reminder_hours integer NOT NULL DEFAULT 24 CHECK (reminder_hours > 0),
 max_reminders integer NOT NULL DEFAULT 2 CHECK (max_reminders BETWEEN 0 AND 10),
 depends_on_id bigint,
 UNIQUE (version_id, code), UNIQUE (version_id, position), UNIQUE (id,version_id),
 FOREIGN KEY (depends_on_id,version_id) REFERENCES template_events(id,version_id) ON DELETE RESTRICT,
 CHECK (depends_on_id IS NULL OR depends_on_id <> id)
);
CREATE TABLE employee_plans (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 employee_id bigint NOT NULL REFERENCES employees(id) ON DELETE RESTRICT,
 version_id bigint NOT NULL REFERENCES plan_versions(id) ON DELETE RESTRICT,
 assigned_by bigint NOT NULL REFERENCES hr_users(id) ON DELETE RESTRICT,
 start_date date NOT NULL,
 timezone text NOT NULL,
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','cancelled')),
 assigned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 ended_at timestamptz,
 UNIQUE(id,employee_id,version_id),
 CHECK ((status='active') = (ended_at IS NULL))
);
CREATE UNIQUE INDEX employee_plans_one_active ON employee_plans(employee_id) WHERE status='active';
CREATE TABLE plan_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 plan_id bigint NOT NULL,
 employee_id bigint NOT NULL,
 version_id bigint NOT NULL,
 template_event_id bigint NOT NULL,
 code text NOT NULL,
 position integer NOT NULL CHECK (position > 0),
 kind text NOT NULL CHECK (kind IN ('info','task','rating','feedback')),
 body text NOT NULL CHECK (btrim(body) <> ''),
 offset_months integer NOT NULL CHECK (offset_months BETWEEN 0 AND 24),
 offset_days integer NOT NULL CHECK (offset_days BETWEEN -30 AND 730),
 local_time time NOT NULL,
 response_hours integer NOT NULL CHECK (response_hours BETWEEN 1 AND 8760),
 required boolean NOT NULL,
 reminder_hours integer NOT NULL CHECK (reminder_hours > 0),
 max_reminders integer NOT NULL CHECK (max_reminders BETWEEN 0 AND 10),
 depends_on_id bigint,
 scheduled_at timestamptz NOT NULL,
 deadline_at timestamptz NOT NULL,
 next_reminder_at timestamptz,
 reminders_sent integer NOT NULL DEFAULT 0 CHECK (reminders_sent BETWEEN 0 AND 10),
 status text NOT NULL DEFAULT 'scheduled' CHECK (status IN
 ('scheduled','queued','awaiting_answer','awaiting_comment','completed','overdue','skipped','cancelled')),
 rating smallint CHECK (rating BETWEEN 1 AND 10),
 comment text CHECK (btrim(comment) <> ''),
 task_confirmed_at timestamptz,
 answered_at timestamptz,
 completed_at timestamptz,
 UNIQUE(id,employee_id), UNIQUE(id,plan_id), UNIQUE(plan_id,code),
 FOREIGN KEY (plan_id,employee_id,version_id) REFERENCES employee_plans(id,employee_id,version_id) ON DELETE RESTRICT,
 FOREIGN KEY (template_event_id,version_id) REFERENCES template_events(id,version_id) ON DELETE RESTRICT,
 FOREIGN KEY (depends_on_id,plan_id) REFERENCES plan_events(id,plan_id) ON DELETE RESTRICT,
 CHECK (depends_on_id IS NULL OR depends_on_id <> id),
 CHECK (deadline_at > scheduled_at),
 CHECK (rating IS NULL OR kind='rating'),
 CHECK (comment IS NULL OR kind IN ('rating','feedback')),
 CHECK (task_confirmed_at IS NULL OR kind='task'),
 CHECK ((status='completed') = (completed_at IS NOT NULL)),
 CHECK (status <> 'awaiting_comment' OR (kind='rating' AND rating IS NOT NULL AND rating<8 AND comment IS NULL)),
 CHECK (status <> 'completed' OR
   (kind='info' OR
    (kind='rating' AND rating IS NOT NULL AND (rating>=8 OR comment IS NOT NULL)) OR
    (kind='task' AND task_confirmed_at IS NOT NULL) OR
    (kind='feedback' AND comment IS NOT NULL)))
);

CREATE TABLE max_inbox (
 event_id text PRIMARY KEY CHECK (btrim(event_id) <> ''),
 employee_id bigint REFERENCES employees(id) ON DELETE RESTRICT,
 received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object' AND octet_length(payload::text)<=65536),
 status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','dead')),
 attempts integer NOT NULL DEFAULT 0 CHECK (attempts>=0),
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 lease_until timestamptz,
 generation bigint NOT NULL DEFAULT 0 CHECK (generation>=0),
 result jsonb CHECK (result IS NULL OR jsonb_typeof(result)='object'),
 last_error text,
 CHECK ((status='processing')=(lease_until IS NOT NULL)),
 CHECK (status<>'done' OR result IS NOT NULL)
);
CREATE TABLE messages (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 employee_id bigint NOT NULL REFERENCES employees(id) ON DELETE RESTRICT,
 direction text NOT NULL CHECK (direction IN ('in','out')),
 author text NOT NULL CHECK (author IN ('employee','bot','hr','system')),
 author_hr_id bigint REFERENCES hr_users(id) ON DELETE RESTRICT,
 body text NOT NULL CHECK (btrim(body) <> ''),
 deduplication_key text UNIQUE,
 inbox_event_id text UNIQUE REFERENCES max_inbox(event_id) ON DELETE RESTRICT,
 reply_to_id bigint,
 event_id bigint,
 case_id bigint,
 rag_status text NOT NULL DEFAULT 'none' CHECK (rag_status IN
  ('none','pending','running','answered','not_found','error','handed_to_hr')),
 rag_model_version text,
 rag_attempts integer NOT NULL DEFAULT 0 CHECK (rag_attempts>=0),
 rag_generation bigint NOT NULL DEFAULT 0 CHECK (rag_generation>=0),
 rag_lease_until timestamptz,
 rag_next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 rag_last_error text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(id,employee_id),
 FOREIGN KEY (reply_to_id,employee_id) REFERENCES messages(id,employee_id) ON DELETE RESTRICT,
 FOREIGN KEY (event_id,employee_id) REFERENCES plan_events(id,employee_id) ON DELETE RESTRICT,
 CHECK ((author='employee')=(direction='in')),
 CHECK ((author='hr')=(author_hr_id IS NOT NULL)),
 CHECK (author<>'hr' OR case_id IS NOT NULL),
 CHECK ((direction='out')=(deduplication_key IS NOT NULL)),
 CHECK (direction<>'out' OR inbox_event_id IS NULL)
 ,CHECK ((rag_status='running')=(rag_lease_until IS NOT NULL))
);
CREATE TABLE hr_cases (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 employee_id bigint NOT NULL REFERENCES employees(id) ON DELETE RESTRICT,
 hr_id bigint NOT NULL REFERENCES hr_users(id) ON DELETE RESTRICT,
 root_message_id bigint NOT NULL UNIQUE,
 reason text NOT NULL CHECK (reason IN ('explicit_human','no_answer','problem','manual')),
 context_summary text NOT NULL,
 status text NOT NULL DEFAULT 'awaiting_hr' CHECK (status IN
  ('awaiting_hr','answered','queued','sent','delivered','closed')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 closed_at timestamptz,
 UNIQUE(id,employee_id), UNIQUE(id,employee_id,hr_id),
 FOREIGN KEY (root_message_id,employee_id) REFERENCES messages(id,employee_id) ON DELETE RESTRICT,
 CHECK ((status='closed')=(closed_at IS NOT NULL))
);
ALTER TABLE messages ADD CONSTRAINT messages_case_employee_fk
 FOREIGN KEY (case_id,employee_id) REFERENCES hr_cases(id,employee_id) ON DELETE RESTRICT;
ALTER TABLE messages ADD CONSTRAINT messages_hr_case_owner_fk
 FOREIGN KEY (case_id,employee_id,author_hr_id) REFERENCES hr_cases(id,employee_id,hr_id) ON DELETE RESTRICT;
CREATE TABLE outbox (
 message_id bigint PRIMARY KEY REFERENCES messages(id) ON DELETE RESTRICT,
 status text NOT NULL DEFAULT 'pending' CHECK (status IN
  ('pending','sending','accepted','delivered','uncertain','dead','cancelled')),
 purpose text NOT NULL CHECK (purpose IN ('event','reminder','hr_reply','rag','notice')),
 attempts integer NOT NULL DEFAULT 0 CHECK (attempts>=0),
 generation bigint NOT NULL DEFAULT 0 CHECK (generation>=0),
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 lease_until timestamptz,
 last_error text,
 external_message_id text UNIQUE,
 accepted_at timestamptz,
 delivered_at timestamptz,
 CHECK ((status='sending')=(lease_until IS NOT NULL)),
 CHECK ((accepted_at IS NULL)=(external_message_id IS NULL)),
 CHECK (status NOT IN ('accepted','delivered') OR accepted_at IS NOT NULL),
 -- accepted_at is when this DB observed API success. A provider delivery can
 -- precede our receipt of the API response, so do not compare those clocks.
 CHECK ((status='delivered')=(delivered_at IS NOT NULL))
);

CREATE TABLE documents (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 code text NOT NULL UNIQUE,
 title text NOT NULL,
 scope_org_unit_id bigint REFERENCES org_units(id) ON DELETE RESTRICT,
 enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE document_versions (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 document_id bigint NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
 version_no integer NOT NULL CHECK (version_no>0),
 object_uri text NOT NULL,
 sha256 bytea NOT NULL CHECK (octet_length(sha256)=32),
 valid_from timestamptz NOT NULL,
 valid_until timestamptz,
 is_current boolean NOT NULL DEFAULT false,
 index_status text NOT NULL DEFAULT 'pending' CHECK (index_status IN ('pending','ready','error')),
 indexed_at timestamptz,
 UNIQUE(document_id,version_no),
 CHECK (valid_until IS NULL OR valid_until>valid_from),
 CHECK (NOT is_current OR index_status='ready'),
 CHECK (index_status<>'ready' OR indexed_at IS NOT NULL)
);
CREATE UNIQUE INDEX document_versions_current ON document_versions(document_id) WHERE is_current;
CREATE TABLE document_chunks (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 version_id bigint NOT NULL REFERENCES document_versions(id) ON DELETE RESTRICT,
 ordinal integer NOT NULL CHECK (ordinal>0),
 locator text NOT NULL,
 content text NOT NULL CHECK (btrim(content)<>''),
 qdrant_point_id uuid NOT NULL UNIQUE,
 UNIQUE(version_id,ordinal)
);
CREATE TABLE answer_sources (
 message_id bigint NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
 chunk_id bigint NOT NULL REFERENCES document_chunks(id) ON DELETE RESTRICT,
 rank integer NOT NULL CHECK (rank>0),
 PRIMARY KEY(message_id,chunk_id), UNIQUE(message_id,rank)
);
CREATE TABLE message_analysis (
 message_id bigint NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
 model_version text NOT NULL,
 topic text NOT NULL,
 sentiment text NOT NULL CHECK (sentiment IN ('positive','neutral','negative','mixed','unknown')),
 problem_level smallint NOT NULL CHECK (problem_level BETWEEN 0 AND 3),
 confidence numeric(5,4) CHECK (confidence BETWEEN 0 AND 1),
 analyzed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(message_id,model_version)
);
CREATE TABLE audit_log (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 employee_id bigint REFERENCES employees(id) ON DELETE RESTRICT,
 actor_hr_id bigint REFERENCES hr_users(id) ON DELETE RESTRICT,
 action text NOT NULL CHECK (btrim(action)<>''),
 details jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(details)='object' AND octet_length(details::text)<=8192)
);

CREATE INDEX employees_hr_list ON employees(hr_id,id);
CREATE INDEX employees_hr_review ON employees(hr_id,id) WHERE lifecycle='hr_review';
CREATE INDEX plan_events_due ON plan_events(scheduled_at,id) WHERE status='scheduled';
CREATE INDEX plan_events_deadline ON plan_events(deadline_at,id)
 WHERE status IN ('queued','awaiting_answer','awaiting_comment');
CREATE INDEX plan_events_reminders ON plan_events(next_reminder_at,id)
 WHERE status IN ('awaiting_answer','awaiting_comment');
CREATE INDEX plan_events_low_ratings ON plan_events(answered_at,id) WHERE rating<8;
CREATE INDEX max_inbox_pending ON max_inbox(next_attempt_at,event_id) WHERE status='pending';
CREATE INDEX max_inbox_leases ON max_inbox(lease_until) WHERE status='processing';
CREATE INDEX messages_conversation ON messages(employee_id,created_at DESC,id DESC);
CREATE INDEX messages_case ON messages(case_id,id) WHERE case_id IS NOT NULL;
CREATE INDEX messages_reply ON messages(reply_to_id,id) WHERE reply_to_id IS NOT NULL;
CREATE INDEX messages_rag_pending ON messages(rag_next_attempt_at,id)
 WHERE rag_status IN ('pending','error') AND rag_attempts<8;
CREATE INDEX messages_rag_leases ON messages(rag_lease_until,id) WHERE rag_status='running';
CREATE INDEX outbox_pending ON outbox(next_attempt_at,message_id) WHERE status='pending';
CREATE INDEX outbox_leases ON outbox(lease_until,message_id) WHERE status='sending';
CREATE INDEX hr_cases_open ON hr_cases(hr_id,created_at,id) WHERE status<>'closed';
CREATE INDEX audit_log_time ON audit_log(occurred_at,id);
CREATE INDEX audit_log_employee ON audit_log(employee_id,occurred_at,id);

-- No user-defined extensions. All FKs use ON UPDATE NO ACTION (the default).
-- All surrogate IDs are immutable; no deletes of business history via application.
INSERT INTO schema_migrations VALUES(1,clock_timestamp(),'Initial PostgreSQL 17 schema');
REVOKE ALL ON SCHEMA korsik FROM PUBLIC;
COMMIT;
