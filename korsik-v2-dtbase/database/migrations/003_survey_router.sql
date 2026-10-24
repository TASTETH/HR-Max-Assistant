\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
SET LOCAL TIME ZONE 'UTC';
SET LOCAL lock_timeout='5s';
SELECT pg_advisory_xact_lock(736482003);
DO $$ BEGIN
 IF (SELECT array_agg(version ORDER BY version) FROM schema_migrations) IS DISTINCT FROM ARRAY[1,2] THEN
  RAISE EXCEPTION 'Migration 003 requires exactly the released schema versions 1 and 2'; END IF;
 IF EXISTS(SELECT employee_id FROM plan_events WHERE kind<>'info' AND
  status IN ('queued','awaiting_answer','awaiting_comment') GROUP BY employee_id HAVING count(*)>1) THEN
  RAISE EXCEPTION 'Multiple active forms: reconcile explicitly before migration; no form is cancelled silently'; END IF;
 IF EXISTS(SELECT m.employee_id FROM outbox o JOIN messages m ON m.id=o.message_id WHERE o.status IN ('sending','uncertain') GROUP BY m.employee_id HAVING count(*)>1) THEN
  RAISE EXCEPTION 'Multiple in-flight sends: reconcile explicitly before migration'; END IF;
END $$;

-- Closed, size-bounded JSON contracts. No generic workflow or profile storage.
CREATE FUNCTION json_keys_allowed(j jsonb,allowed text[]) RETURNS boolean
 LANGUAGE sql IMMUTABLE STRICT SET search_path=korsik,pg_catalog AS $$
 SELECT jsonb_typeof(j)='object' AND NOT EXISTS
  (SELECT 1 FROM jsonb_object_keys(CASE WHEN jsonb_typeof(j)='object' THEN j ELSE '{}' END) k WHERE NOT k=ANY(allowed))
$$;
CREATE FUNCTION valid_audience(j jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=korsik,pg_catalog AS $$
DECLARE key text; val jsonb;
BEGIN
 IF NOT json_keys_allowed(j,ARRAY['location_tags','employee_types']) THEN RETURN false; END IF;
 FOR key,val IN SELECT * FROM jsonb_each(j) LOOP
  IF jsonb_typeof(val)<>'array' OR jsonb_array_length(val)=0 THEN RETURN false; END IF;
  IF key='location_tags' AND EXISTS(SELECT 1 FROM jsonb_array_elements_text(val) x WHERE x NOT IN ('GrPP','GTP','Luzhniki')) THEN RETURN false; END IF;
  IF key='employee_types' AND EXISTS(SELECT 1 FROM jsonb_array_elements_text(val) x WHERE x NOT IN ('office','production')) THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;
CREATE FUNCTION valid_questions(a jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=korsik,pg_catalog AS $$
DECLARE q jsonb; opt jsonb; rule jsonb; seen text[]:=ARRAY[]::text[]; ids text[]; code text; typ text;
BEGIN
 IF jsonb_typeof(a)<>'array' OR jsonb_array_length(a) NOT BETWEEN 1 AND 50 THEN RETURN false; END IF;
 FOR q IN SELECT value FROM jsonb_array_elements(a) LOOP
  IF NOT json_keys_allowed(q,ARRAY['code','type','prompt','required','options','comment_rule','when','exclusive_options','scale_direction','analyze','reply_text']) THEN RETURN false; END IF;
  code:=q->>'code';typ:=q->>'type';
  IF code IS NULL OR code !~ '^[a-z][a-z0-9_]{0,49}$' OR code=ANY(seen) OR
   typ IS NULL OR typ NOT IN ('rating','boolean','single_choice','multi_choice','text','attachment') OR
   jsonb_typeof(q->'prompt') IS DISTINCT FROM 'string' OR length(btrim(q->>'prompt')) NOT BETWEEN 1 AND 4000 THEN RETURN false; END IF;
  IF q ? 'required' AND jsonb_typeof(q->'required')<>'boolean' THEN RETURN false; END IF;
  IF q ? 'analyze' AND jsonb_typeof(q->'analyze')<>'boolean' THEN RETURN false; END IF;
  IF q ? 'reply_text' AND (jsonb_typeof(q->'reply_text')<>'string' OR length(q->>'reply_text')>4000) THEN RETURN false; END IF;
  IF typ='rating' AND q->>'scale_direction' IS DISTINCT FROM 'low_bad' THEN RETURN false; END IF;
  ids:=ARRAY[]::text[];
  IF typ IN ('single_choice','multi_choice') THEN
   IF jsonb_typeof(q->'options') IS DISTINCT FROM 'array' OR jsonb_array_length(q->'options') NOT BETWEEN 1 AND 30 THEN RETURN false; END IF;
   FOR opt IN SELECT value FROM jsonb_array_elements(q->'options') LOOP
    IF NOT json_keys_allowed(opt,ARRAY['id','label','reply_text']) OR (opt->>'id') IS NULL OR
     opt->>'id' !~ '^[a-zA-Z0-9_-]{1,50}$' OR (opt->>'id')=ANY(ids) OR
     jsonb_typeof(opt->'label') IS DISTINCT FROM 'string' OR length(btrim(opt->>'label')) NOT BETWEEN 1 AND 300 THEN RETURN false; END IF;
    IF opt ? 'reply_text' AND (jsonb_typeof(opt->'reply_text')<>'string' OR length(opt->>'reply_text')>4000) THEN RETURN false; END IF;
    ids:=array_append(ids,opt->>'id');
   END LOOP;
  ELSIF q ? 'options' THEN RETURN false;
  END IF;
  IF q ? 'exclusive_options' THEN
   IF typ<>'multi_choice' OR jsonb_typeof(q->'exclusive_options')<>'array' OR
    EXISTS(SELECT 1 FROM jsonb_array_elements_text(q->'exclusive_options') x WHERE NOT x=ANY(ids)) THEN RETURN false; END IF;
  END IF;
  IF q ? 'comment_rule' THEN
   rule:=q->'comment_rule';
   IF NOT json_keys_allowed(rule,ARRAY['mode','values']) OR rule->>'mode' IS NULL OR
    rule->>'mode' NOT IN ('always','never','low_rating','values') THEN RETURN false; END IF;
   IF rule->>'mode'='low_rating' AND typ<>'rating' THEN RETURN false; END IF;
   IF typ='rating' AND rule->>'mode' NOT IN ('low_rating','always') THEN RETURN false; END IF;
   IF rule->>'mode'='values' THEN
    IF typ NOT IN ('boolean','single_choice','multi_choice') OR jsonb_typeof(rule->'values') IS DISTINCT FROM 'array' OR jsonb_array_length(rule->'values')=0 THEN RETURN false; END IF;
    FOR opt IN SELECT value FROM jsonb_array_elements(rule->'values') LOOP
     IF typ='boolean' AND jsonb_typeof(opt)<>'boolean' THEN RETURN false; END IF;
     IF typ<>'boolean' AND (jsonb_typeof(opt)<>'string' OR NOT (opt#>>'{}')=ANY(ids)) THEN RETURN false; END IF;
    END LOOP;
   ELSIF rule ? 'values' THEN RETURN false;
   END IF;
  END IF;
  IF q ? 'when' THEN
   rule:=q->'when';
   IF NOT json_keys_allowed(rule,ARRAY['question_code','operator','value']) OR
    NOT COALESCE((rule->>'question_code')=ANY(seen),false) OR
    rule->>'operator' IS NULL OR rule->>'operator' NOT IN ('equals','contains') OR NOT rule ? 'value' THEN RETURN false; END IF;
  END IF;
  seen:=array_append(seen,code);
 END LOOP;
 RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;
CREATE FUNCTION valid_definition(j jsonb,kind text) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=korsik,pg_catalog AS $$
DECLARE v jsonb;
BEGIN
 IF NOT json_keys_allowed(j,ARRAY['schema_version','questions','audience','variants','completion_text','attachments','options']) OR octet_length(j::text)>65536 THEN RETURN false; END IF;
 IF j='{}' THEN RETURN kind<>'survey'; END IF;
 IF j->'schema_version' IS DISTINCT FROM '1'::jsonb THEN RETURN false; END IF;
 IF j ? 'audience' AND NOT valid_audience(j->'audience') THEN RETURN false; END IF;
 IF (j ? 'attachments' OR j ? 'options') AND NOT valid_message_payload((j-'schema_version'-'questions'-'audience'-'variants'-'completion_text')) THEN RETURN false; END IF;
 IF j ? 'completion_text' AND (jsonb_typeof(j->'completion_text')<>'string' OR length(j->>'completion_text')>4000) THEN RETURN false; END IF;
 IF kind='survey' THEN
  IF NOT COALESCE(valid_questions(j->'questions'),false) THEN RETURN false; END IF;
 ELSIF j ? 'questions' THEN RETURN false; END IF;
 IF j ? 'variants' THEN
  IF jsonb_typeof(j->'variants')<>'array' OR jsonb_array_length(j->'variants')>20 THEN RETURN false; END IF;
  FOR v IN SELECT value FROM jsonb_array_elements(j->'variants') LOOP
   IF NOT json_keys_allowed(v,ARRAY['audience','body','questions','attachments','options']) OR NOT COALESCE(valid_audience(v->'audience'),false) OR v->'audience'='{}' THEN RETURN false; END IF;
   IF (v ? 'attachments' OR v ? 'options') AND NOT valid_message_payload(v-'audience'-'body'-'questions') THEN RETURN false; END IF;
   IF v ? 'body' AND (jsonb_typeof(v->'body')<>'string' OR length(btrim(v->>'body')) NOT BETWEEN 1 AND 4000) THEN RETURN false; END IF;
   IF kind='survey' AND v ? 'questions' AND NOT valid_questions(v->'questions') THEN RETURN false; END IF;
   IF kind<>'survey' AND v ? 'questions' THEN RETURN false; END IF;
  END LOOP;
 END IF;
 RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;
CREATE FUNCTION valid_calendar(j jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=korsik,pg_catalog AS $$
DECLARE key text; val jsonb; item jsonb; ds text;
BEGIN
 IF NOT json_keys_allowed(j,ARRAY['weekdays','holidays','extra_workdays']) OR octet_length(j::text)>16384 THEN RETURN false; END IF;
 IF NOT j ?& ARRAY['weekdays','holidays','extra_workdays'] OR jsonb_typeof(j->'weekdays')<>'array' OR jsonb_array_length(j->'weekdays') NOT BETWEEN 1 AND 7 THEN RETURN false; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(j->'weekdays') x WHERE x::text !~ '^[1-7]$') OR
  (SELECT count(*)<>count(DISTINCT value) FROM jsonb_array_elements(j->'weekdays')) THEN RETURN false; END IF;
 FOREACH key IN ARRAY ARRAY['holidays','extra_workdays'] LOOP
  val:=j->key;
  IF jsonb_typeof(val)<>'array' OR jsonb_array_length(val)>366 THEN RETURN false; END IF;
  FOR item IN SELECT value FROM jsonb_array_elements(val) LOOP
   ds:=item#>>'{}';
   IF jsonb_typeof(item)<>'string' OR ds !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN RETURN false; END IF;
   PERFORM ds::date;
  END LOOP;
  IF (SELECT count(*)<>count(DISTINCT value) FROM jsonb_array_elements(val)) THEN RETURN false; END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(j->'holidays') h JOIN jsonb_array_elements(j->'extra_workdays') w ON h.value=w.value) THEN RETURN false; END IF;
 RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;
CREATE FUNCTION valid_message_payload(j jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=korsik,pg_catalog AS $$
DECLARE a jsonb; o jsonb;
BEGIN
 IF NOT json_keys_allowed(j,ARRAY['kind','role','question_code','interaction_revision','options','attachments','pending_interaction_id','text_origin','answer_type','selected_options','allow_skip']) OR octet_length(j::text)>65536 THEN RETURN false; END IF;
 IF j ? 'allow_skip' AND jsonb_typeof(j->'allow_skip')<>'boolean' THEN RETURN false; END IF;
 IF j ? 'answer_type' AND (jsonb_typeof(j->'answer_type')<>'string' OR j->>'answer_type' NOT IN ('rating','boolean','single_choice','multi_choice','text','attachment','comment')) THEN RETURN false; END IF;
 IF j ? 'selected_options' AND (jsonb_typeof(j->'selected_options')<>'array' OR jsonb_array_length(j->'selected_options')>30 OR EXISTS(SELECT 1 FROM jsonb_array_elements(j->'selected_options') z WHERE jsonb_typeof(z)<>'string')) THEN RETURN false; END IF;
 IF j ? 'kind' AND (jsonb_typeof(j->'kind')<>'string' OR j->>'kind' NOT IN ('text','action','attachment')) THEN RETURN false; END IF;
 IF j ? 'role' AND (jsonb_typeof(j->'role')<>'string' OR j->>'role' NOT IN ('prompt','completion','notice')) THEN RETURN false; END IF;
 IF j ? 'question_code' AND (jsonb_typeof(j->'question_code')<>'string' OR length(j->>'question_code')>50) THEN RETURN false; END IF;
 IF j ? 'text_origin' AND j->>'text_origin' NOT IN ('employee','option_label','action') THEN RETURN false; END IF;
 IF j ? 'interaction_revision' AND (jsonb_typeof(j->'interaction_revision')<>'number' OR j->>'interaction_revision' !~ '^[0-9]+$' OR length(j->>'interaction_revision')>18) THEN RETURN false; END IF;
 IF j ? 'pending_interaction_id' AND (jsonb_typeof(j->'pending_interaction_id')<>'number' OR j->>'pending_interaction_id' !~ '^[1-9][0-9]*$' OR length(j->>'pending_interaction_id')>18) THEN RETURN false; END IF;
 IF j ? 'options' THEN
  a:=j->'options';
  IF jsonb_typeof(a)<>'array' OR jsonb_array_length(a)>30 THEN RETURN false; END IF;
  FOR o IN SELECT value FROM jsonb_array_elements(a) LOOP
   IF NOT json_keys_allowed(o,ARRAY['id','label']) OR jsonb_typeof(o->'id') IS DISTINCT FROM 'string' OR
    length(o->>'id') NOT BETWEEN 1 AND 100 OR jsonb_typeof(o->'label') IS DISTINCT FROM 'string' OR length(o->>'label') NOT BETWEEN 1 AND 300 THEN RETURN false; END IF;
  END LOOP;
 END IF;
 IF j ? 'attachments' THEN
  a:=j->'attachments';
  IF jsonb_typeof(a)<>'array' OR jsonb_array_length(a)>10 THEN RETURN false; END IF;
  FOR o IN SELECT value FROM jsonb_array_elements(a) LOOP
   IF NOT json_keys_allowed(o,ARRAY['asset_ref','kind']) OR jsonb_typeof(o->'asset_ref') IS DISTINCT FROM 'string' OR
    length(o->>'asset_ref') NOT BETWEEN 1 AND 500 OR o->>'kind' IS NULL OR
    o->>'kind' NOT IN ('image','photo','pdf','map','file') THEN RETURN false; END IF;
  END LOOP;
 END IF;
 RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;

ALTER TABLE employees ADD COLUMN location_tag text CHECK(location_tag IN ('GrPP','GTP','Luzhniki')),
 ADD COLUMN employee_type text CHECK(employee_type IN ('office','production')),
 ADD COLUMN communication_style text CHECK(communication_style IN ('informal','formal')),
 ADD COLUMN probation_end_date date CHECK(probation_end_date IS NULL OR start_date IS NULL OR probation_end_date>=start_date),
 ADD COLUMN context_revision bigint NOT NULL DEFAULT 0 CHECK(context_revision>=0);
ALTER TABLE hr_users ADD COLUMN contact_uri text CHECK(contact_uri ~ '^https://[^[:space:]]+$'),
 ADD COLUMN contact_verified_at timestamptz,
 ADD CHECK((contact_uri IS NULL)=(contact_verified_at IS NULL));
ALTER TABLE employee_plans ADD COLUMN probation_end_date date,
 ADD COLUMN location_tag text CHECK(location_tag IN ('GrPP','GTP','Luzhniki')),
 ADD COLUMN employee_type text CHECK(employee_type IN ('office','production')),
 ADD COLUMN work_calendar jsonb NOT NULL DEFAULT '{"weekdays":[1,2,3,4,5],"holidays":[],"extra_workdays":[]}'
 CHECK(valid_calendar(work_calendar)),
 ADD CHECK(probation_end_date IS NULL OR probation_end_date>=start_date);
ALTER TABLE template_events ADD COLUMN definition jsonb NOT NULL DEFAULT '{}',
 ADD COLUMN schedule_anchor text NOT NULL DEFAULT 'start_date' CHECK(schedule_anchor IN ('start_date','probation_end_date')),
 ADD COLUMN workday_no smallint CHECK(workday_no BETWEEN 1 AND 366),
 ADD COLUMN send_window_hours integer NOT NULL DEFAULT 72 CHECK(send_window_hours BETWEEN 1 AND 8760),
 ADD CHECK(workday_no IS NULL OR (offset_months=0 AND offset_days=0)),
 ADD CHECK(valid_definition(definition,kind));
ALTER TABLE template_events DROP CONSTRAINT template_events_kind_check;
ALTER TABLE template_events ADD CONSTRAINT template_events_kind_check CHECK(kind IN ('info','task','rating','feedback','survey'));
ALTER TABLE plan_events ADD COLUMN definition jsonb NOT NULL DEFAULT '{}',
 ADD COLUMN current_question_code text,
 ADD COLUMN interaction_revision bigint NOT NULL DEFAULT 0 CHECK(interaction_revision>=0),
 ADD COLUMN schedule_anchor text NOT NULL DEFAULT 'start_date' CHECK(schedule_anchor IN ('start_date','probation_end_date')),
 ADD COLUMN workday_no smallint CHECK(workday_no BETWEEN 1 AND 366),
 ADD COLUMN send_window_hours integer NOT NULL DEFAULT 72 CHECK(send_window_hours BETWEEN 1 AND 8760),
 ADD COLUMN send_until_at timestamptz,
 ADD CHECK(workday_no IS NULL OR (offset_months=0 AND offset_days=0)),
 ADD CHECK(valid_definition(definition,kind) AND NOT definition ? 'variants'),
 ADD CHECK(current_question_code IS NULL OR kind='survey');
ALTER TABLE plan_events DISABLE TRIGGER plan_events_history;
UPDATE plan_events SET send_until_at=scheduled_at+make_interval(hours=>response_hours);
ALTER TABLE plan_events ENABLE TRIGGER plan_events_history;
ALTER TABLE plan_events ALTER COLUMN send_until_at SET NOT NULL, ALTER COLUMN deadline_at DROP NOT NULL;
ALTER TABLE plan_events ADD CHECK(send_until_at>=scheduled_at);
ALTER TABLE plan_events DROP CONSTRAINT plan_events_kind_check;
ALTER TABLE plan_events ADD CONSTRAINT plan_events_kind_check CHECK(kind IN ('info','task','rating','feedback','survey'));
-- Replace the v1 completion CHECK by its meaning, rather than guessing auto-generated names.
DO $$ DECLARE c record; BEGIN
 FOR c IN SELECT conname FROM pg_constraint WHERE conrelid='plan_events'::regclass AND contype='c'
  AND pg_get_constraintdef(oid) LIKE '%kind = ''info''%' AND pg_get_constraintdef(oid) LIKE '%task_confirmed_at%' LOOP
  EXECUTE format('ALTER TABLE plan_events DROP CONSTRAINT %I',c.conname);
 END LOOP;
END $$;
ALTER TABLE plan_events ADD CONSTRAINT plan_events_completed_response CHECK(status<>'completed' OR
 kind IN ('info','survey') OR (kind='rating' AND rating IS NOT NULL AND (rating>=8 OR comment IS NOT NULL)) OR
 (kind='task' AND task_confirmed_at IS NOT NULL) OR (kind='feedback' AND comment IS NOT NULL));
-- survey's awaiting_comment is checked against event_answers by the operation/trigger.
DO $$ DECLARE c record; BEGIN
 FOR c IN SELECT conname FROM pg_constraint WHERE conrelid='plan_events'::regclass AND contype='c'
  AND pg_get_constraintdef(oid) LIKE '%awaiting_comment%' AND pg_get_constraintdef(oid) LIKE '%rating IS NOT NULL%' LOOP
  EXECUTE format('ALTER TABLE plan_events DROP CONSTRAINT %I',c.conname);
 END LOOP;
END $$;
ALTER TABLE plan_events ADD CHECK(status<>'awaiting_comment' OR kind='survey' OR
 (kind='rating' AND rating IS NOT NULL AND rating<8 AND comment IS NULL));
CREATE UNIQUE INDEX plan_events_one_interactive ON plan_events(employee_id)
 WHERE kind<>'info' AND status IN ('queued','awaiting_answer','awaiting_comment');
CREATE INDEX plan_events_send_expiry ON plan_events(send_until_at,id) WHERE status IN ('scheduled','queued');
DROP INDEX plan_events_deadline;
CREATE INDEX plan_events_deadline ON plan_events(deadline_at,id) WHERE status IN ('awaiting_answer','awaiting_comment');

CREATE TABLE event_answers (
 event_id bigint NOT NULL REFERENCES plan_events(id) ON DELETE RESTRICT,
 question_code text NOT NULL,
 answer_type text NOT NULL CHECK(answer_type IN ('rating','boolean','single_choice','multi_choice','text','attachment')),
 state text NOT NULL CHECK(state IN ('draft','awaiting_comment','accepted','skipped')),
 revision integer NOT NULL DEFAULT 0 CHECK(revision>=0),
 rating smallint CHECK(rating BETWEEN 1 AND 10),
 value jsonb,
 comment text CHECK(length(btrim(comment)) BETWEEN 1 AND 8000),
 answered_at timestamptz,
 commented_at timestamptz,
 source_message_id bigint REFERENCES messages(id) ON DELETE RESTRICT,
 PRIMARY KEY(event_id,question_code),
 CHECK((state='draft')=(answered_at IS NULL)),
 CHECK((comment IS NULL)=(commented_at IS NULL)),
 CHECK(state<>'awaiting_comment' OR comment IS NULL),
 CHECK(state='skipped' OR
  (answer_type='rating' AND rating IS NOT NULL AND value IS NULL) OR
  (answer_type='boolean' AND rating IS NULL AND jsonb_typeof(value)='boolean') OR
  (answer_type='single_choice' AND rating IS NULL AND jsonb_typeof(value)='string') OR
  (answer_type='multi_choice' AND rating IS NULL AND jsonb_typeof(value)='array') OR
  (answer_type='text' AND rating IS NULL AND jsonb_typeof(value)='string' AND length(btrim(value#>>'{}')) BETWEEN 1 AND 8000) OR
  (answer_type='attachment' AND rating IS NULL AND json_keys_allowed(value,ARRAY['asset_ref','kind'])
   AND jsonb_typeof(value->'asset_ref')='string' AND length(value->>'asset_ref') BETWEEN 1 AND 500
   AND value->>'kind' IN ('photo','image','pdf','file'))),
 CHECK(state='skipped' OR ((answer_type='rating')=(rating IS NOT NULL)) AND (answer_type='rating' OR value IS NOT NULL)),
 CHECK(state<>'draft' OR answer_type='multi_choice')
);
CREATE INDEX event_answers_low ON event_answers(answered_at,event_id,question_code) WHERE rating<8;

ALTER TABLE messages ADD COLUMN payload jsonb NOT NULL DEFAULT '{}' CHECK(valid_message_payload(payload)),
 ADD COLUMN analysis_gate text NOT NULL DEFAULT 'none' CHECK(analysis_gate IN ('none','pending','clear','hr'));
ALTER TABLE messages DROP CONSTRAINT messages_body_check;
ALTER TABLE messages ADD CHECK(btrim(body)<>'' OR payload->>'kind'='action' OR
 COALESCE(jsonb_array_length(payload->'attachments'),0)>0);
ALTER TABLE hr_cases ADD COLUMN source_key text,
 ADD COLUMN source_event_id bigint,
 ADD COLUMN source_question_code text,
 ADD COLUMN category text,
 ADD COLUMN priority text NOT NULL DEFAULT 'medium' CHECK(priority IN ('low','medium','high')),
 ADD COLUMN closure_basis text CHECK(closure_basis IN ('verified_delivery','api_accepted')),
 ADD COLUMN closure_note text,
 ADD FOREIGN KEY(source_event_id,employee_id) REFERENCES plan_events(id,employee_id) ON DELETE RESTRICT,
 ADD FOREIGN KEY(source_event_id,source_question_code) REFERENCES event_answers(event_id,question_code) ON DELETE RESTRICT,
 ADD CHECK(source_question_code IS NULL OR source_event_id IS NOT NULL);
UPDATE hr_cases SET source_key='message:'||root_message_id;
ALTER TABLE hr_cases ALTER COLUMN source_key SET NOT NULL;
CREATE UNIQUE INDEX hr_cases_one_open_source ON hr_cases(source_key) WHERE status<>'closed';

-- Evolve the existing analysis table into durable jobs+results; no parallel analysis table.
ALTER TABLE message_analysis ADD COLUMN id bigint GENERATED ALWAYS AS IDENTITY,
 ADD COLUMN employee_id bigint REFERENCES employees(id) ON DELETE RESTRICT,
 ADD COLUMN source_key text,
 ADD COLUMN input_revision integer NOT NULL DEFAULT 0 CHECK(input_revision>=0),
 ADD COLUMN policy_version text NOT NULL DEFAULT 'legacy',
 ADD COLUMN source_event_id bigint,
 ADD COLUMN source_question_code text,
 ADD COLUMN request jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(request)='object' AND octet_length(request::text)<=65536),
 ADD COLUMN result jsonb CHECK(jsonb_typeof(result)='object' AND octet_length(result::text)<=131072),
 ADD COLUMN status text NOT NULL DEFAULT 'done' CHECK(status IN ('pending','running','done','error','dead','superseded')),
 ADD COLUMN attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
 ADD COLUMN generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),
 ADD COLUMN lease_until timestamptz,
 ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 ADD COLUMN last_error text,
 ADD COLUMN case_id bigint,
 ADD FOREIGN KEY(source_event_id,employee_id) REFERENCES plan_events(id,employee_id) ON DELETE RESTRICT,
 ADD FOREIGN KEY(source_event_id,source_question_code) REFERENCES event_answers(event_id,question_code) ON DELETE RESTRICT,
 ADD FOREIGN KEY(message_id,employee_id) REFERENCES messages(id,employee_id) ON DELETE RESTRICT,
 ADD FOREIGN KEY(case_id,employee_id) REFERENCES hr_cases(id,employee_id) ON DELETE RESTRICT;
UPDATE message_analysis a SET employee_id=m.employee_id,source_key='message:'||a.message_id,
 result=jsonb_build_object('legacy_summary',jsonb_build_object('topic',a.topic,'sentiment',a.sentiment,'problem_level',a.problem_level,'confidence',a.confidence))
 FROM messages m WHERE m.id=a.message_id;
ALTER TABLE message_analysis DROP CONSTRAINT message_analysis_pkey,
 ADD PRIMARY KEY(id), ALTER COLUMN employee_id SET NOT NULL, ALTER COLUMN source_key SET NOT NULL,
 ALTER COLUMN topic DROP NOT NULL, ALTER COLUMN sentiment DROP NOT NULL,
 ALTER COLUMN problem_level DROP NOT NULL, ALTER COLUMN analyzed_at DROP NOT NULL,
 ADD UNIQUE(source_key,input_revision,model_version,policy_version),
 ADD CHECK((status='running')=(lease_until IS NOT NULL)),
 ADD CHECK(status<>'done' OR result IS NOT NULL),
 ADD CHECK(source_question_code IS NULL OR source_event_id IS NOT NULL);
CREATE INDEX message_analysis_work ON message_analysis(model_version,policy_version,next_attempt_at,id) WHERE status IN ('pending','error');
CREATE INDEX message_analysis_leases ON message_analysis(lease_until,id) WHERE status='running';

CREATE TABLE pending_interactions (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 employee_id bigint NOT NULL REFERENCES employees(id) ON DELETE RESTRICT,
 original_message_id bigint NOT NULL,
 context_revision bigint NOT NULL CHECK(context_revision>=0),
 choices jsonb NOT NULL CHECK(jsonb_typeof(choices)='array' AND jsonb_array_length(choices) BETWEEN 2 AND 10 AND octet_length(choices::text)<=8192),
 expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved','expired','cancelled')),
 chosen_code text,
 resolution jsonb CHECK(jsonb_typeof(resolution)='object'),
 resolved_at timestamptz,
 FOREIGN KEY(original_message_id,employee_id) REFERENCES messages(id,employee_id) ON DELETE RESTRICT,
 CHECK(expires_at>created_at),
 CHECK((status='resolved')=(resolved_at IS NOT NULL)),
 CHECK(status<>'resolved' OR (chosen_code IS NOT NULL AND resolution IS NOT NULL)),
 CHECK(status='resolved' OR (chosen_code IS NULL AND resolution IS NULL))
);
CREATE UNIQUE INDEX pending_interactions_one_pending ON pending_interactions(original_message_id) WHERE status='pending';
CREATE UNIQUE INDEX pending_interactions_one_result ON pending_interactions(original_message_id) WHERE status='resolved';
CREATE INDEX pending_interactions_active ON pending_interactions(employee_id,expires_at,id) WHERE status='pending';

-- Remaining operation definitions follow in this same transaction.

CREATE OR REPLACE FUNCTION guard_event_history() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 IF OLD.status IN ('completed','overdue','skipped','cancelled') AND
  (NEW.rating,NEW.comment,NEW.task_confirmed_at,NEW.answered_at,NEW.completed_at,NEW.definition,NEW.current_question_code)
  IS DISTINCT FROM (OLD.rating,OLD.comment,OLD.task_confirmed_at,OLD.answered_at,OLD.completed_at,OLD.definition,OLD.current_question_code) THEN
  RAISE EXCEPTION 'Terminal event response is immutable'; END IF;
 IF (NEW.plan_id,NEW.employee_id,NEW.version_id,NEW.template_event_id,NEW.code,NEW.position,NEW.kind,NEW.definition,
  NEW.schedule_anchor,NEW.workday_no) IS DISTINCT FROM
  (OLD.plan_id,OLD.employee_id,OLD.version_id,OLD.template_event_id,OLD.code,OLD.position,OLD.kind,OLD.definition,
  OLD.schedule_anchor,OLD.workday_no) THEN RAISE EXCEPTION 'Event identity and selected definition are immutable'; END IF;
 IF OLD.status<>'scheduled' AND
  (NEW.body,NEW.offset_months,NEW.offset_days,NEW.local_time,NEW.response_hours,NEW.required,
   NEW.reminder_hours,NEW.max_reminders,NEW.scheduled_at,NEW.send_until_at,NEW.send_window_hours,NEW.depends_on_id)
  IS DISTINCT FROM
  (OLD.body,OLD.offset_months,OLD.offset_days,OLD.local_time,OLD.response_hours,OLD.required,
   OLD.reminder_hours,OLD.max_reminders,OLD.scheduled_at,OLD.send_until_at,OLD.send_window_hours,OLD.depends_on_id) THEN
  RAISE EXCEPTION 'Queued event configuration is immutable'; END IF;
 IF OLD.status<>'scheduled' AND NEW.deadline_at IS DISTINCT FROM OLD.deadline_at AND
  NOT (OLD.status='queued' AND NEW.status IN ('awaiting_answer','completed','overdue')) THEN
  RAISE EXCEPTION 'Response deadline can start only at the first API acceptance'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION guard_calendar_snapshot() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 IF (NEW.work_calendar,NEW.location_tag,NEW.employee_type) IS DISTINCT FROM (OLD.work_calendar,OLD.location_tag,OLD.employee_type) THEN RAISE EXCEPTION 'Calendar snapshot is immutable; assign a replacement plan'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER employee_plans_calendar BEFORE UPDATE ON employee_plans FOR EACH ROW EXECUTE FUNCTION guard_calendar_snapshot();
CREATE OR REPLACE FUNCTION guard_message_history() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 IF (NEW.employee_id,NEW.direction,NEW.author,NEW.author_hr_id,NEW.body,NEW.payload,NEW.deduplication_key,
  NEW.inbox_event_id,NEW.reply_to_id,NEW.event_id,NEW.created_at) IS DISTINCT FROM
  (OLD.employee_id,OLD.direction,OLD.author,OLD.author_hr_id,OLD.body,OLD.payload,OLD.deduplication_key,
  OLD.inbox_event_id,OLD.reply_to_id,OLD.event_id,OLD.created_at) THEN
  RAISE EXCEPTION 'Persisted message text, payload, identity and authorship are immutable'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION bump_employee_context() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 IF (NEW.hr_id,NEW.org_unit_id,NEW.job_title,NEW.start_date,NEW.timezone,NEW.lifecycle,NEW.registration_step,
  NEW.registration_revision,NEW.location_tag,NEW.employee_type,NEW.communication_style,NEW.probation_end_date)
  IS DISTINCT FROM (OLD.hr_id,OLD.org_unit_id,OLD.job_title,OLD.start_date,OLD.timezone,OLD.lifecycle,OLD.registration_step,
  OLD.registration_revision,OLD.location_tag,OLD.employee_type,OLD.communication_style,OLD.probation_end_date) THEN
  NEW.context_revision:=OLD.context_revision+1;
 ELSIF NEW.context_revision NOT BETWEEN OLD.context_revision AND OLD.context_revision+1 THEN
  RAISE EXCEPTION 'Context revision must increase monotonically'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER employees_context_revision BEFORE UPDATE ON employees FOR EACH ROW EXECUTE FUNCTION bump_employee_context();
CREATE FUNCTION touch_context(p_employee bigint) RETURNS void LANGUAGE sql SET search_path=korsik,pg_catalog AS $$
 UPDATE employees SET context_revision=context_revision+1 WHERE id=p_employee
$$;
CREATE FUNCTION audience_matches(j jsonb,loc text,typ text) RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path=korsik,pg_catalog AS $$
 SELECT (NOT j ? 'location_tags' OR COALESCE(j->'location_tags' ? loc,false)) AND
        (NOT j ? 'employee_types' OR COALESCE(j->'employee_types' ? typ,false))
$$;
CREATE FUNCTION workday_date(anchor date,n integer,cal jsonb) RETURNS date LANGUAGE plpgsql IMMUTABLE SET search_path=korsik,pg_catalog AS $$
DECLARE d date:=anchor; found integer:=0; i integer;
BEGIN
 IF anchor IS NULL OR n IS NULL OR n NOT BETWEEN 1 AND 366 OR NOT valid_calendar(cal) THEN RAISE EXCEPTION 'Invalid workday inputs'; END IF;
 FOR i IN 0..3660 LOOP
  IF (cal->'extra_workdays') ? to_char(d,'YYYY-MM-DD') OR
   ((cal->'weekdays') @> to_jsonb(ARRAY[extract(isodow FROM d)::int]) AND NOT (cal->'holidays') ? to_char(d,'YYYY-MM-DD')) THEN
   found:=found+1;IF found=n THEN RETURN d; END IF;
  END IF;
  d:=d+1;
 END LOOP;
 RAISE EXCEPTION 'Calendar horizon exceeded';
END $$;
CREATE FUNCTION scheduled_event_time(start_day date,probation_day date,tz text,cal jsonb,
 anchor text,workday integer,months integer,days integer,local_clock time) RETURNS timestamptz
 LANGUAGE plpgsql STABLE SET search_path=korsik,pg_catalog AS $$
DECLARE d date;
BEGIN
 d:=CASE anchor WHEN 'start_date' THEN start_day WHEN 'probation_end_date' THEN probation_day ELSE NULL END;
 IF d IS NULL THEN RAISE EXCEPTION 'Required schedule anchor is unknown'; END IF;
 IF workday IS NOT NULL THEN RETURN (workday_date(d,workday,cal)+local_clock) AT TIME ZONE tz; END IF;
 RETURN event_time(d,tz,months,days,local_clock);
END $$;
CREATE FUNCTION send_limit(run_at timestamptz,start_day date,tz text,hours integer) RETURNS timestamptz
 LANGUAGE sql STABLE STRICT SET search_path=korsik,pg_catalog AS $$
 SELECT CASE WHEN run_at < (start_day+TIME '00:00') AT TIME ZONE tz
  THEN LEAST(run_at+make_interval(hours=>hours),(start_day+TIME '00:00') AT TIME ZONE tz)
  ELSE run_at+make_interval(hours=>hours) END
$$;

DROP FUNCTION queue_message(bigint,text,text,text,text,bigint,bigint,bigint,bigint);
CREATE FUNCTION queue_message(p_employee bigint,p_author text,p_body text,p_key text,p_purpose text,
 p_event bigint DEFAULT NULL,p_case bigint DEFAULT NULL,p_hr bigint DEFAULT NULL,p_reply bigint DEFAULT NULL,
 p_payload jsonb DEFAULT '{}') RETURNS bigint LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE mid bigint; m messages; payload_value jsonb:=p_payload;
BEGIN
 IF p_key LIKE 'comment-prompt:%' AND payload_value='{}' THEN
  payload_value:=jsonb_build_object('role','prompt','question_code','_legacy_comment'); END IF;
 INSERT INTO messages(employee_id,direction,author,author_hr_id,body,payload,deduplication_key,event_id,case_id,reply_to_id)
 VALUES(p_employee,'out',p_author,p_hr,p_body,payload_value,p_key,p_event,p_case,p_reply)
 ON CONFLICT(deduplication_key) DO NOTHING RETURNING id INTO mid;
 IF mid IS NULL THEN
  SELECT * INTO STRICT m FROM messages WHERE deduplication_key=p_key;
  IF (m.employee_id,m.author,m.author_hr_id,m.body,m.payload,m.event_id,m.case_id,m.reply_to_id)
   IS DISTINCT FROM (p_employee,p_author,p_hr,p_body,payload_value,p_event,p_case,p_reply) THEN
   RAISE EXCEPTION 'Idempotency key reused with different content'; END IF;
  mid:=m.id;
 END IF;
 INSERT INTO outbox(message_id,purpose) VALUES(mid,p_purpose) ON CONFLICT DO NOTHING;
 RETURN mid;
END $$;

DROP FUNCTION assign_plan(bigint,bigint,bigint);
CREATE FUNCTION assign_plan(p_hr bigint,p_employee bigint,p_version bigint,
 p_calendar jsonb DEFAULT '{"weekdays":[1,2,3,4,5],"holidays":[],"extra_workdays":[]}') RETURNS bigint
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e employees; pid bigint; t template_events; def jsonb; variant jsonb; chosen jsonb;
 run_at timestamptz; chosen_body text; matches integer;
BEGIN
 SELECT * INTO STRICT e FROM employees WHERE id=p_employee FOR UPDATE;
 IF e.hr_id<>p_hr OR e.lifecycle NOT IN ('ready','adapting') OR e.max_account_id IS NULL OR
  NOT EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) OR NOT valid_calendar(p_calendar) THEN
  RAISE EXCEPTION 'Plan assignment denied or calendar invalid'; END IF;
 PERFORM 1 FROM plan_versions WHERE id=p_version AND published_at IS NOT NULL FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Published version required'; END IF;
 INSERT INTO employee_plans(employee_id,version_id,assigned_by,start_date,timezone,probation_end_date,work_calendar,location_tag,employee_type)
 VALUES(p_employee,p_version,p_hr,e.start_date,e.timezone,e.probation_end_date,p_calendar,e.location_tag,e.employee_type) RETURNING id INTO pid;
 FOR t IN SELECT * FROM template_events WHERE version_id=p_version ORDER BY position LOOP
  def:=t.definition;
  IF NOT audience_matches(COALESCE(def->'audience','{}'),e.location_tag,e.employee_type) THEN CONTINUE; END IF;
  chosen:=NULL;matches:=0;chosen_body:=t.body;
  FOR variant IN SELECT value FROM jsonb_array_elements(COALESCE(def->'variants','[]')) LOOP
   IF audience_matches(variant->'audience',e.location_tag,e.employee_type) THEN chosen:=variant;matches:=matches+1; END IF;
  END LOOP;
  IF matches>1 THEN RAISE EXCEPTION 'Ambiguous audience variants for event %',t.code; END IF;
  def:=def-'variants';
  IF chosen IS NOT NULL THEN
   chosen_body:=COALESCE(chosen->>'body',chosen_body);
   IF chosen ? 'attachments' THEN def:=jsonb_set(def,'{attachments}',chosen->'attachments'); END IF;
   IF chosen ? 'options' THEN def:=jsonb_set(def,'{options}',chosen->'options'); END IF;
   IF chosen ? 'questions' THEN def:=jsonb_set(def,'{questions}',chosen->'questions'); END IF;
  END IF;
  run_at:=scheduled_event_time(e.start_date,e.probation_end_date,e.timezone,p_calendar,t.schedule_anchor,
   t.workday_no,t.offset_months,t.offset_days,t.local_time);
  INSERT INTO plan_events(plan_id,employee_id,version_id,template_event_id,code,position,kind,body,definition,
   offset_months,offset_days,local_time,response_hours,required,reminder_hours,max_reminders,
   schedule_anchor,workday_no,send_window_hours,scheduled_at,send_until_at,deadline_at)
  VALUES(pid,p_employee,p_version,t.id,t.code,t.position,t.kind,chosen_body,def,
   t.offset_months,t.offset_days,t.local_time,t.response_hours,t.required,t.reminder_hours,t.max_reminders,
   t.schedule_anchor,t.workday_no,t.send_window_hours,run_at,send_limit(run_at,e.start_date,e.timezone,t.send_window_hours),NULL);
 END LOOP;
 IF NOT EXISTS(SELECT 1 FROM plan_events WHERE plan_id=pid) THEN RAISE EXCEPTION 'No events match employee audience'; END IF;
 IF EXISTS(SELECT 1 FROM plan_events a JOIN template_events origin ON origin.id=a.template_event_id
  WHERE a.plan_id=pid AND origin.depends_on_id IS NOT NULL AND NOT EXISTS
   (SELECT 1 FROM plan_events b WHERE b.plan_id=pid AND b.template_event_id=origin.depends_on_id)) THEN
  RAISE EXCEPTION 'Audience filtering removed a required dependency'; END IF;
 UPDATE plan_events a SET depends_on_id=b.id FROM template_events origin,plan_events b
  WHERE a.plan_id=pid AND origin.id=a.template_event_id AND b.plan_id=pid AND b.template_event_id=origin.depends_on_id;
 UPDATE employees SET lifecycle='adapting' WHERE id=p_employee;
 PERFORM touch_context(p_employee);
 INSERT INTO audit_log(employee_id,actor_hr_id,action,details) VALUES(p_employee,p_hr,'plan_assigned',
  jsonb_build_object('plan_id',pid,'version_id',p_version,'calendar_sha256',encode(sha256(convert_to(p_calendar::text,'UTF8')),'hex')));
 RETURN pid;
END $$;
CREATE OR REPLACE FUNCTION configure_event(p_hr bigint,p_event bigint,p_response_hours integer,p_max_reminders integer,p_local_time time)
 RETURNS void LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e plan_events; p employee_plans; moment timestamptz;
BEGIN
 SELECT * INTO STRICT e FROM plan_events WHERE id=p_event;
 PERFORM 1 FROM employees WHERE id=e.employee_id AND hr_id=p_hr AND EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'HR access denied'; END IF;
 SELECT * INTO STRICT e FROM plan_events WHERE id=p_event AND status='scheduled' FOR UPDATE;
 SELECT * INTO STRICT p FROM employee_plans WHERE id=e.plan_id AND status='active';
 moment:=scheduled_event_time(p.start_date,p.probation_end_date,p.timezone,p.work_calendar,e.schedule_anchor,e.workday_no,e.offset_months,e.offset_days,p_local_time);
 UPDATE plan_events SET response_hours=p_response_hours,max_reminders=p_max_reminders,local_time=p_local_time,
  scheduled_at=moment,send_until_at=send_limit(moment,p.start_date,p.timezone,send_window_hours),deadline_at=NULL WHERE id=p_event;
 PERFORM touch_context(e.employee_id);
 INSERT INTO audit_log(employee_id,actor_hr_id,action,details) VALUES(e.employee_id,p_hr,'event_configured',jsonb_build_object('event_id',p_event));
END $$;
DROP FUNCTION reschedule_plan(bigint,bigint,date,text);
CREATE FUNCTION reschedule_plan(p_hr bigint,p_plan bigint,p_start date,p_timezone text,p_probation_end date DEFAULT NULL)
 RETURNS void LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE p employee_plans; end_day date;
BEGIN
 SELECT * INTO STRICT p FROM employee_plans WHERE id=p_plan;
 PERFORM 1 FROM employees WHERE id=p.employee_id AND hr_id=p_hr AND EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'HR access denied'; END IF;
 SELECT * INTO STRICT p FROM employee_plans WHERE id=p_plan AND status='active' FOR UPDATE;
 end_day:=COALESCE(p_probation_end,p.probation_end_date);
 UPDATE employee_plans SET start_date=p_start,timezone=p_timezone,probation_end_date=end_day WHERE id=p_plan;
 UPDATE employees SET start_date=p_start,timezone=p_timezone,probation_end_date=end_day WHERE id=p.employee_id;
 UPDATE plan_events SET scheduled_at=scheduled_event_time(p_start,end_day,p_timezone,p.work_calendar,schedule_anchor,workday_no,offset_months,offset_days,local_time),
  send_until_at=send_limit(scheduled_event_time(p_start,end_day,p_timezone,p.work_calendar,schedule_anchor,workday_no,offset_months,offset_days,local_time),p_start,p_timezone,send_window_hours),
  deadline_at=NULL WHERE plan_id=p_plan AND status='scheduled';
 PERFORM touch_context(p.employee_id);
 INSERT INTO audit_log(employee_id,actor_hr_id,action,details) VALUES(p.employee_id,p_hr,'plan_rescheduled',
  jsonb_build_object('plan_id',p_plan,'old_start_date',p.start_date,'new_start_date',p_start,'old_probation_end',p.probation_end_date,'new_probation_end',end_day));
END $$;

CREATE FUNCTION survey_question(def jsonb,code text) RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path=korsik,pg_catalog AS $$
 SELECT q FROM jsonb_array_elements(COALESCE(def->'questions','[]')) q WHERE q->>'code'=code
$$;
CREATE FUNCTION question_enabled(p_event bigint,q jsonb) RETURNS boolean LANGUAGE plpgsql STABLE SET search_path=korsik,pg_catalog AS $$
DECLARE a event_answers; expected jsonb;
BEGIN
 IF NOT q ? 'when' THEN RETURN true; END IF;
 SELECT * INTO a FROM event_answers WHERE event_id=p_event AND question_code=q#>>'{when,question_code}' AND state='accepted';
 IF NOT FOUND THEN RETURN false; END IF;
 expected:=q#>'{when,value}';
 IF q#>>'{when,operator}'='equals' THEN RETURN COALESCE(COALESCE(a.value,to_jsonb(a.rating))=expected,false); END IF;
 RETURN COALESCE(a.value @> jsonb_build_array(expected),false);
END $$;
CREATE FUNCTION comment_needed(q jsonb,r integer,v jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=korsik,pg_catalog AS $$
DECLARE mode text:=COALESCE(q#>>'{comment_rule,mode}',CASE WHEN q->>'type'='rating' THEN 'low_rating' ELSE 'never' END); x jsonb;
BEGIN
 IF mode='always' THEN RETURN true; END IF;
 IF mode='low_rating' THEN RETURN r<8; END IF;
 IF mode='values' THEN
  FOR x IN SELECT value FROM jsonb_array_elements(q#>'{comment_rule,values}') LOOP
   IF x=v OR (jsonb_typeof(v)='array' AND v @> jsonb_build_array(x)) THEN RETURN true; END IF;
  END LOOP;
 END IF;
 RETURN false;
END $$;
CREATE FUNCTION validate_event_answer() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e plan_events; q jsonb; x jsonb; seen text[]:=ARRAY[]::text[]; options text[];
BEGIN
 SELECT * INTO STRICT e FROM plan_events WHERE id=NEW.event_id FOR UPDATE;
 q:=survey_question(e.definition,NEW.question_code);
 IF e.kind<>'survey' OR q IS NULL OR q->>'type'<>NEW.answer_type OR NOT question_enabled(e.id,q) THEN RAISE EXCEPTION 'Unknown question or answer type mismatch'; END IF;
 IF TG_OP='UPDATE' AND OLD.state IN ('accepted','skipped') THEN RAISE EXCEPTION 'Final question answer is immutable'; END IF;
 IF e.status NOT IN ('awaiting_answer','awaiting_comment') OR e.current_question_code IS DISTINCT FROM NEW.question_code OR
  e.deadline_at IS NULL OR e.deadline_at<=clock_timestamp() THEN RAISE EXCEPTION 'Question is not current or expired'; END IF;
 IF NEW.source_message_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM messages WHERE id=NEW.source_message_id AND employee_id=e.employee_id AND direction='in') THEN
  RAISE EXCEPTION 'Answer source belongs to another employee'; END IF;
 IF NEW.state='skipped' THEN
  IF COALESCE((q->>'required')::boolean,true) THEN RAISE EXCEPTION 'Required question cannot be skipped'; END IF;
  RETURN NEW;
 END IF;
 IF NEW.answer_type='rating' AND NEW.rating IS NULL THEN RAISE EXCEPTION 'Rating required'; END IF;
 IF NEW.answer_type IN ('single_choice','multi_choice') THEN
  SELECT array_agg(o->>'id') INTO options FROM jsonb_array_elements(q->'options') o;
  IF NEW.answer_type='single_choice' THEN
   IF NOT COALESCE((NEW.value#>>'{}')=ANY(options),false) THEN RAISE EXCEPTION 'Unknown option'; END IF;
  ELSE
   IF jsonb_typeof(NEW.value) IS DISTINCT FROM 'array' OR jsonb_array_length(NEW.value)>30 THEN RAISE EXCEPTION 'Invalid multiselect'; END IF;
   FOR x IN SELECT value FROM jsonb_array_elements(NEW.value) LOOP
    IF jsonb_typeof(x)<>'string' OR NOT (x#>>'{}')=ANY(options) OR (x#>>'{}')=ANY(seen) THEN RAISE EXCEPTION 'Unknown or duplicate option'; END IF;
    seen:=array_append(seen,x#>>'{}');
   END LOOP;
   IF NEW.state<>'draft' AND cardinality(seen)=0 THEN RAISE EXCEPTION 'Submit requires a nonempty set or explicit optional skip'; END IF;
   IF cardinality(seen)>1 AND EXISTS(SELECT 1 FROM jsonb_array_elements_text(COALESCE(q->'exclusive_options','[]')) z WHERE z=ANY(seen)) THEN
    RAISE EXCEPTION 'Exclusive option cannot be combined'; END IF;
  END IF;
 END IF;
 IF NEW.answer_type='attachment' AND NOT COALESCE(json_keys_allowed(NEW.value,ARRAY['asset_ref','kind']) AND
  jsonb_typeof(NEW.value->'asset_ref')='string' AND length(NEW.value->>'asset_ref') BETWEEN 1 AND 500 AND
  NEW.value->>'kind' IN ('photo','image','pdf','file'),false) THEN RAISE EXCEPTION 'Invalid attachment reference'; END IF;
 IF NEW.state='accepted' AND comment_needed(q,NEW.rating,NEW.value) AND NEW.comment IS NULL THEN RAISE EXCEPTION 'Comment required'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER event_answers_contract BEFORE INSERT OR UPDATE ON event_answers FOR EACH ROW EXECUTE FUNCTION validate_event_answer();
CREATE FUNCTION analysis_defaults() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 IF NEW.employee_id IS NULL THEN SELECT employee_id INTO STRICT NEW.employee_id FROM messages WHERE id=NEW.message_id; END IF;
 IF NEW.source_key IS NULL THEN NEW.source_key:='message:'||NEW.message_id; END IF;
 IF NEW.status='done' AND NEW.result IS NULL THEN
  NEW.result:=jsonb_build_object('legacy_summary',jsonb_build_object('topic',NEW.topic,'sentiment',NEW.sentiment,'problem_level',NEW.problem_level,'confidence',NEW.confidence));
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER message_analysis_defaults BEFORE INSERT ON message_analysis FOR EACH ROW EXECUTE FUNCTION analysis_defaults();
CREATE FUNCTION enqueue_analysis(p_employee bigint,p_message bigint,p_source_key text,p_revision integer,
 p_input jsonb,p_event bigint DEFAULT NULL,p_question text DEFAULT NULL,p_mixed boolean DEFAULT false,
 p_model text DEFAULT NULL,p_policy text DEFAULT NULL) RETURNS bigint
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE jid bigint;
BEGIN
 p_model:=COALESCE(NULLIF(p_model,''),NULLIF(current_setting('korsik.analysis_model_version',true),''),'unconfigured');
 p_policy:=COALESCE(NULLIF(p_policy,''),NULLIF(current_setting('korsik.analysis_policy_version',true),''),'unconfigured');
 PERFORM 1 FROM employees WHERE id=p_employee FOR UPDATE;
 PERFORM 1 FROM messages WHERE id=p_message AND employee_id=p_employee AND direction='in' FOR UPDATE;
 IF NOT FOUND OR p_source_key IS NULL OR p_input->>'source' IS NULL OR p_input->>'employee_id' IS DISTINCT FROM p_employee::text OR jsonb_typeof(p_input->'text') IS DISTINCT FROM 'string' OR p_input->>'source' NOT IN ('message','survey','hr_request') THEN RAISE EXCEPTION 'Invalid analysis source'; END IF;
 IF NOT (p_source_key='message:'||p_message OR (p_event IS NOT NULL AND p_question IS NULL AND p_source_key='legacy-answer:'||p_event) OR
  (p_event IS NOT NULL AND p_question IS NOT NULL AND p_source_key='answer:'||p_event||':'||p_question)) THEN RAISE EXCEPTION 'Analysis source key does not match its business source'; END IF;
 INSERT INTO message_analysis(message_id,employee_id,source_key,input_revision,model_version,policy_version,request,
  source_event_id,source_question_code,status,analyzed_at,topic,sentiment,problem_level)
 VALUES(p_message,p_employee,p_source_key,p_revision,p_model,p_policy,p_input,p_event,p_question,'pending',NULL,NULL,NULL,NULL)
 ON CONFLICT(source_key,input_revision,model_version,policy_version) DO NOTHING RETURNING id INTO jid;
 IF jid IS NULL THEN SELECT id INTO STRICT jid FROM message_analysis WHERE source_key=p_source_key AND input_revision=p_revision AND model_version=p_model AND policy_version=p_policy; END IF;
 IF p_mixed THEN UPDATE messages SET analysis_gate='pending' WHERE id=p_message AND analysis_gate IN ('none','clear'); END IF;
 RETURN jid;
END $$;
CREATE FUNCTION answer_as_text(q jsonb,r integer,v jsonb) RETURNS text LANGUAGE sql IMMUTABLE SET search_path=korsik,pg_catalog AS $$
 SELECT CASE q->>'type'
  WHEN 'rating' THEN r::text
  WHEN 'boolean' THEN CASE WHEN v='true'::jsonb THEN 'Да' ELSE 'Нет' END
  WHEN 'single_choice' THEN (SELECT o->>'label' FROM jsonb_array_elements(q->'options') o WHERE o->>'id'=v#>>'{}')
  WHEN 'multi_choice' THEN (SELECT string_agg(o->>'label',E'\n' ORDER BY n) FROM jsonb_array_elements(q->'options') WITH ORDINALITY a(o,n) WHERE v ? (o->>'id'))
  WHEN 'text' THEN v#>>'{}'
  WHEN 'attachment' THEN '' ELSE '' END
$$;
CREATE FUNCTION analysis_for_answer(p_event bigint,p_question text,p_message bigint DEFAULT NULL) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e plan_events; a event_answers; q jsonb; mid bigint; provider text; body_text text;
BEGIN
 SELECT * INTO STRICT e FROM plan_events WHERE id=p_event;
 SELECT * INTO STRICT a FROM event_answers WHERE event_id=p_event AND question_code=p_question;
 q:=survey_question(e.definition,p_question);
 IF NOT COALESCE((q->>'analyze')::boolean,false) OR a.state IN ('draft','skipped') THEN RETURN; END IF;
 body_text:=answer_as_text(q,a.rating,a.value);
 IF a.comment IS NOT NULL THEN body_text:=body_text||E'\n'||a.comment; END IF;
 mid:=COALESCE(p_message,a.source_message_id);
 IF mid IS NULL THEN
  INSERT INTO messages(employee_id,direction,author,body,event_id,payload)
  VALUES(e.employee_id,'in','employee',body_text,e.id,jsonb_build_object('kind','action','question_code',p_question,'text_origin',CASE WHEN a.answer_type IN ('boolean','single_choice','multi_choice') THEN 'option_label' ELSE 'employee' END)) RETURNING id INTO mid;
 END IF;
 SELECT inbox_event_id INTO provider FROM messages WHERE id=mid;
 PERFORM enqueue_analysis(e.employee_id,mid,'answer:'||p_event||':'||p_question,a.revision,
  jsonb_build_object('source','survey','employee_id',e.employee_id::text,'provider_event_id',provider,
   'survey_session_id',p_event::text,'question_id',p_question,'question',q->>'prompt','score',a.rating,'text',body_text,
   'scale_direction',q->>'scale_direction'),p_event,p_question);
END $$;
CREATE FUNCTION queue_survey_prompt(p_event bigint,p_initial boolean DEFAULT false) RETURNS bigint
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e plan_events; q jsonb; options jsonb; prompt text;
BEGIN
 SELECT * INTO STRICT e FROM plan_events WHERE id=p_event;
 q:=survey_question(e.definition,e.current_question_code);
 IF q IS NULL THEN RAISE EXCEPTION 'No current survey question'; END IF;
 SELECT COALESCE(jsonb_agg(jsonb_build_object('id',o->>'id','label',o->>'label')),'[]') INTO options FROM jsonb_array_elements(COALESCE(q->'options','[]')) o;
 IF q->>'type'='rating' THEN SELECT jsonb_agg(jsonb_build_object('id',i::text,'label',i::text)) INTO options FROM generate_series(1,10) i; END IF;
 IF q->>'type'='boolean' THEN options:='[{"id":"true","label":"Да"},{"id":"false","label":"Нет"}]'; END IF;
 prompt:=CASE WHEN e.status='awaiting_comment' THEN 'Пожалуйста, добавьте комментарий к вашему ответу.' ELSE q->>'prompt' END;
 IF p_initial THEN prompt:=e.body||E'\n'||prompt; END IF;
 RETURN queue_message(e.employee_id,'bot',prompt,
  CASE WHEN p_initial THEN 'event:'||e.id ELSE 'survey-prompt:'||e.id||':'||e.interaction_revision END,
  CASE WHEN p_initial THEN 'event' ELSE 'notice' END,e.id,NULL,NULL,NULL,
  jsonb_build_object('role','prompt','question_code',e.current_question_code,'interaction_revision',e.interaction_revision,
   'options',CASE WHEN e.status='awaiting_comment' THEN '[]'::jsonb ELSE options END,
   'answer_type',CASE WHEN e.status='awaiting_comment' THEN 'comment' ELSE q->>'type' END,
   'allow_skip',e.status='awaiting_answer' AND NOT COALESCE((q->>'required')::boolean,true),'selected_options',COALESCE((SELECT value FROM event_answers WHERE event_id=e.id AND question_code=e.current_question_code AND answer_type='multi_choice'),'[]'::jsonb),
   'attachments',CASE WHEN p_initial THEN COALESCE(e.definition->'attachments','[]'::jsonb) ELSE '[]'::jsonb END));
END $$;
CREATE FUNCTION advance_survey(p_event bigint) RETURNS void LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e plan_events; q jsonb; next_code text; next_prompt text;
BEGIN
 SELECT * INTO STRICT e FROM plan_events WHERE id=p_event FOR UPDATE;
 FOR q IN SELECT value FROM jsonb_array_elements(e.definition->'questions') LOOP
  IF question_enabled(e.id,q) AND NOT EXISTS(SELECT 1 FROM event_answers WHERE event_id=e.id AND question_code=q->>'code' AND state IN ('accepted','skipped')) THEN
   next_code:=q->>'code';EXIT;
  END IF;
 END LOOP;
 IF next_code IS NULL THEN
  UPDATE plan_events SET current_question_code=NULL,status='completed',completed_at=clock_timestamp(),next_reminder_at=NULL,
   interaction_revision=interaction_revision+1 WHERE id=e.id;
  PERFORM queue_message(e.employee_id,'bot',COALESCE(e.definition->>'completion_text','Спасибо! Опрос завершён.'),
   'survey-completed:'||e.id,'notice',e.id,NULL,NULL,NULL,'{"role":"completion"}');
 ELSE
  UPDATE plan_events SET current_question_code=next_code,status='awaiting_answer',interaction_revision=interaction_revision+1 WHERE id=e.id;
  PERFORM queue_survey_prompt(e.id);
 END IF;
 UPDATE outbox o SET status='cancelled' FROM messages m WHERE m.id=o.message_id AND m.event_id=e.id AND
  o.status='pending' AND (o.purpose='reminder' OR (m.payload->>'role'='prompt' AND m.payload->>'interaction_revision' IS DISTINCT FROM
   (SELECT interaction_revision::text FROM plan_events WHERE id=e.id)));
 PERFORM touch_context(e.employee_id);
END $$;
-- awaiting_comment -> awaiting_answer moves to the next question, with a new revision.
CREATE OR REPLACE FUNCTION guard_status() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE a text;b text;allowed boolean:=false;
BEGIN
 IF TG_TABLE_NAME='employees' THEN
  a:=OLD.lifecycle;b:=NEW.lifecycle;
  allowed:=(a,b) IN (('invited','registering'),('registering','hr_review'),('hr_review','registering'),('hr_review','ready'),('ready','adapting'),('adapting','completed'),('invited','stopped'),('registering','stopped'),('hr_review','stopped'),('ready','stopped'),('adapting','stopped'));
 ELSIF TG_TABLE_NAME='plan_events' THEN
  a:=OLD.status;b:=NEW.status;
  allowed:=(a,b) IN (('scheduled','queued'),('scheduled','cancelled'),('scheduled','skipped'),('queued','awaiting_answer'),('queued','completed'),('queued','overdue'),('queued','cancelled'),('awaiting_answer','awaiting_comment'),('awaiting_answer','completed'),('awaiting_answer','overdue'),('awaiting_answer','cancelled'),('awaiting_answer','skipped'),('awaiting_comment','completed'),('awaiting_comment','overdue'),('awaiting_comment','cancelled')) OR
   (OLD.kind='survey' AND a='awaiting_comment' AND b='awaiting_answer');
 ELSIF TG_TABLE_NAME='hr_cases' THEN
  a:=OLD.status;b:=NEW.status;
  allowed:=(a,b) IN (('awaiting_hr','answered'),('answered','queued'),('queued','sent'),('sent','delivered'),('delivered','closed')) OR
   (a='sent' AND b='closed' AND NEW.closure_basis='api_accepted' AND length(btrim(NEW.closure_note))>0);
 ELSIF TG_TABLE_NAME='employee_plans' THEN
  a:=OLD.status;b:=NEW.status;allowed:=a='active' AND b IN ('completed','cancelled');
  IF NEW.version_id<>OLD.version_id OR NEW.employee_id<>OLD.employee_id THEN RAISE EXCEPTION 'Plan identity immutable'; END IF;
 END IF;
 IF a IS DISTINCT FROM b AND NOT allowed THEN RAISE EXCEPTION 'Invalid transition in %: % -> %',TG_TABLE_NAME,a,b; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION answer_survey_question(p_employee bigint,p_event bigint,p_question text,p_type text,p_value jsonb,
 p_revision bigint,p_source_message bigint DEFAULT NULL) RETURNS text LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e plan_events; q jsonb; a event_answers; r integer; val jsonb; need_comment boolean; count_old integer;
BEGIN
 PERFORM 1 FROM employees WHERE id=p_employee FOR UPDATE;
 SELECT * INTO STRICT e FROM plan_events WHERE id=p_event AND employee_id=p_employee FOR UPDATE;
 q:=survey_question(e.definition,p_question);
 IF e.kind<>'survey' OR e.current_question_code IS DISTINCT FROM p_question OR p_revision IS DISTINCT FROM e.interaction_revision OR
  e.status NOT IN ('awaiting_answer','awaiting_comment') OR e.deadline_at<=clock_timestamp() OR q IS NULL THEN
  RAISE EXCEPTION 'Stale, expired or unrelated survey action'; END IF;
 IF p_source_message IS NOT NULL AND NOT EXISTS(SELECT 1 FROM messages WHERE id=p_source_message AND employee_id=p_employee AND direction='in') THEN RAISE EXCEPTION 'Foreign survey source message'; END IF;
 IF p_type='comment' THEN
  SELECT * INTO STRICT a FROM event_answers WHERE event_id=p_event AND question_code=p_question AND state='awaiting_comment' FOR UPDATE;
  IF jsonb_typeof(p_value) IS DISTINCT FROM 'string' OR length(btrim(p_value#>>'{}')) NOT BETWEEN 1 AND 8000 THEN RAISE EXCEPTION 'Nonempty comment required'; END IF;
  UPDATE event_answers SET comment=p_value#>>'{}',commented_at=clock_timestamp(),state='accepted',revision=revision+1
   WHERE event_id=p_event AND question_code=p_question;
  PERFORM analysis_for_answer(p_event,p_question,p_source_message);
  PERFORM queue_question_ack(p_event,p_question);
  PERFORM advance_survey(p_event);
 ELSIF p_type='skip' THEN
  IF e.status<>'awaiting_answer' THEN RAISE EXCEPTION 'Cannot skip a pending comment'; END IF;
  INSERT INTO event_answers(event_id,question_code,answer_type,state,answered_at,source_message_id)
  VALUES(p_event,p_question,q->>'type','skipped',clock_timestamp(),p_source_message)
  ON CONFLICT(event_id,question_code) DO UPDATE SET state='skipped',value=NULL,rating=NULL,answered_at=clock_timestamp(),revision=event_answers.revision+1
   WHERE event_answers.state='draft';
  GET DIAGNOSTICS count_old=ROW_COUNT;
  IF count_old<>1 THEN RAISE EXCEPTION 'Question already finalized'; END IF;
  PERFORM advance_survey(p_event);
 ELSE
  IF e.status<>'awaiting_answer' OR p_type<>q->>'type' THEN RAISE EXCEPTION 'Answer type mismatch'; END IF;
  IF p_type='rating' THEN
   IF jsonb_typeof(p_value) IS DISTINCT FROM 'number' OR p_value::text !~ '^[0-9]+$' THEN RAISE EXCEPTION 'Integer rating required'; END IF;
   r:=(p_value::text)::int;val:=NULL;
  ELSE val:=p_value; END IF;
  need_comment:=comment_needed(q,r,val);
  IF p_type='multi_choice' THEN
   -- Canonical option order, independent from click/toggle order.
   SELECT jsonb_agg(to_jsonb(o->>'id') ORDER BY n) INTO val FROM jsonb_array_elements(q->'options') WITH ORDINALITY a(o,n) WHERE p_value ? (o->>'id');
   IF p_value IS NULL OR jsonb_typeof(p_value)<>'array' OR COALESCE(jsonb_array_length(val),0)<>jsonb_array_length(p_value) THEN RAISE EXCEPTION 'Unknown/duplicate multiselect value'; END IF;
   need_comment:=comment_needed(q,r,val);
  END IF;
  INSERT INTO event_answers(event_id,question_code,answer_type,state,rating,value,answered_at,source_message_id)
  VALUES(p_event,p_question,p_type,CASE WHEN need_comment THEN 'awaiting_comment' ELSE 'accepted' END,r,val,clock_timestamp(),p_source_message)
  ON CONFLICT(event_id,question_code) DO UPDATE SET state=EXCLUDED.state,rating=EXCLUDED.rating,value=EXCLUDED.value,
   answered_at=EXCLUDED.answered_at,source_message_id=EXCLUDED.source_message_id,revision=event_answers.revision+1
   WHERE event_answers.state='draft';
  GET DIAGNOSTICS count_old=ROW_COUNT;
  IF count_old<>1 THEN RAISE EXCEPTION 'Question already answered'; END IF;
  PERFORM analysis_for_answer(p_event,p_question,p_source_message);
  IF need_comment THEN
   UPDATE plan_events SET status='awaiting_comment',interaction_revision=interaction_revision+1 WHERE id=p_event;
   PERFORM queue_survey_prompt(p_event);
   PERFORM touch_context(p_employee);
  ELSE PERFORM queue_question_ack(p_event,p_question);PERFORM advance_survey(p_event); END IF;
 END IF;
 RETURN (SELECT status FROM plan_events WHERE id=p_event);
END $$;
CREATE FUNCTION toggle_survey_option(p_employee bigint,p_event bigint,p_question text,p_option text,p_revision bigint)
 RETURNS jsonb LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e plan_events; q jsonb; val jsonb;
BEGIN
 PERFORM 1 FROM employees WHERE id=p_employee FOR UPDATE;
 SELECT * INTO STRICT e FROM plan_events WHERE id=p_event AND employee_id=p_employee FOR UPDATE;
 q:=survey_question(e.definition,p_question);
 IF e.status<>'awaiting_answer' OR e.current_question_code IS DISTINCT FROM p_question OR e.interaction_revision IS DISTINCT FROM p_revision OR
  e.deadline_at<=clock_timestamp() OR q->>'type' IS DISTINCT FROM 'multi_choice' THEN RAISE EXCEPTION 'Stale multiselect action'; END IF;
 SELECT value INTO val FROM event_answers WHERE event_id=p_event AND question_code=p_question;
 val:=COALESCE(val,'[]');
 IF val ? p_option THEN SELECT COALESCE(jsonb_agg(x),'[]') INTO val FROM jsonb_array_elements(val) x WHERE x<>to_jsonb(p_option);
 ELSE val:=val||jsonb_build_array(p_option); END IF;
 INSERT INTO event_answers(event_id,question_code,answer_type,state,value) VALUES(p_event,p_question,'multi_choice','draft',val)
 ON CONFLICT(event_id,question_code) DO UPDATE SET value=EXCLUDED.value,revision=event_answers.revision+1 WHERE event_answers.state='draft';
 IF NOT FOUND THEN RAISE EXCEPTION 'Already submitted'; END IF;
 UPDATE plan_events SET interaction_revision=interaction_revision+1 WHERE id=p_event;
 PERFORM touch_context(p_employee);
 PERFORM queue_survey_prompt(p_event);
 RETURN val;
END $$;

CREATE OR REPLACE FUNCTION prepare_due_events(p_limit integer DEFAULT 100) RETURNS integer
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE emp record; e plan_events; q jsonb; question_value text; n integer:=0;
BEGIN
 IF p_limit NOT BETWEEN 1 AND 1000 THEN RAISE EXCEPTION 'Batch size 1..1000 required'; END IF;
 FOR emp IN SELECT x.id FROM employees x WHERE x.lifecycle='adapting' AND EXISTS
  (SELECT 1 FROM plan_events s JOIN employee_plans p ON p.id=s.plan_id WHERE s.employee_id=x.id AND p.status='active' AND
   ((s.status='scheduled' AND s.scheduled_at<=clock_timestamp()) OR
    (s.status='queued' AND s.send_until_at<=clock_timestamp()) OR
    (s.status IN ('awaiting_answer','awaiting_comment') AND (s.deadline_at<=clock_timestamp() OR s.next_reminder_at<=clock_timestamp()))))
  ORDER BY x.id LIMIT p_limit FOR UPDATE OF x SKIP LOCKED
 LOOP
  FOR e IN SELECT s.* FROM plan_events s JOIN employee_plans p ON p.id=s.plan_id WHERE s.employee_id=emp.id AND p.status='active'
   AND s.status IN ('scheduled','queued','awaiting_answer','awaiting_comment') ORDER BY s.position FOR UPDATE OF s SKIP LOCKED
  LOOP
   IF (e.status IN ('scheduled','queued') AND e.send_until_at<=clock_timestamp()) OR
    (e.status IN ('awaiting_answer','awaiting_comment') AND e.deadline_at<=clock_timestamp()) THEN
    -- A network send already in progress cannot be revoked atomically. Reconcile first.
    IF EXISTS(SELECT 1 FROM outbox o JOIN messages m ON m.id=o.message_id WHERE m.event_id=e.id AND o.status IN ('sending','uncertain')) THEN CONTINUE; END IF;
    UPDATE plan_events SET status=CASE WHEN status='scheduled' THEN 'skipped' ELSE 'overdue' END,next_reminder_at=NULL WHERE id=e.id;
    UPDATE outbox o SET status='cancelled' FROM messages m WHERE m.id=o.message_id AND m.event_id=e.id AND o.status='pending'
     AND (o.purpose IN ('event','reminder') OR m.payload->>'role'='prompt');
    PERFORM touch_context(emp.id);
   ELSIF e.status='scheduled' AND e.scheduled_at<=clock_timestamp() AND
    (e.depends_on_id IS NULL OR EXISTS(SELECT 1 FROM plan_events WHERE id=e.depends_on_id AND status='completed')) THEN
    IF e.kind<>'info' AND EXISTS(SELECT 1 FROM plan_events WHERE employee_id=emp.id AND kind<>'info' AND status IN ('queued','awaiting_answer','awaiting_comment')) THEN CONTINUE; END IF;
    IF e.kind='survey' THEN
     question_value:=NULL;
     FOR q IN SELECT value FROM jsonb_array_elements(e.definition->'questions') LOOP
      IF question_enabled(e.id,q) THEN question_value:=q->>'code';EXIT; END IF;
     END LOOP;
     IF question_value IS NULL THEN RAISE EXCEPTION 'Survey has no enabled first question'; END IF;
     UPDATE plan_events SET status='queued',current_question_code=question_value WHERE id=e.id;
     PERFORM queue_survey_prompt(e.id,true);
    ELSE
     PERFORM queue_message(emp.id,'bot',e.body,'event:'||e.id,'event',e.id,NULL,NULL,NULL,
      (e.definition-'schema_version'-'questions'-'audience'-'completion_text') || CASE WHEN e.kind='info' THEN '{}'::jsonb ELSE jsonb_build_object('role','prompt','interaction_revision',e.interaction_revision) END);
     UPDATE plan_events SET status='queued' WHERE id=e.id;
    END IF;
    PERFORM touch_context(emp.id);n:=n+1;
   ELSIF e.status IN ('awaiting_answer','awaiting_comment') AND e.next_reminder_at<=clock_timestamp() AND e.reminders_sent<e.max_reminders THEN
    PERFORM queue_message(emp.id,'bot','Напоминаю о незавершённом этапе: '||e.body,
     'reminder:'||e.id||':'||(e.reminders_sent+1),'reminder',e.id,NULL,NULL,NULL,
     jsonb_strip_nulls(jsonb_build_object('role','prompt','question_code',e.current_question_code,'interaction_revision',e.interaction_revision)));
    UPDATE plan_events SET reminders_sent=reminders_sent+1,next_reminder_at=clock_timestamp()+make_interval(hours=>reminder_hours) WHERE id=e.id;
    n:=n+1;
   END IF;
  END LOOP;
 END LOOP;
 RETURN n;
END $$;
DROP FUNCTION claim_outbox(integer);
CREATE FUNCTION claim_outbox(p_limit integer DEFAULT 50,p_global_serial boolean DEFAULT false)
 RETURNS TABLE(message_id bigint,generation bigint,body text,max_chat_id text,payload jsonb,employee_id bigint,plan_event_id bigint)
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE emp record; item record; e plan_events;
BEGIN
 IF p_limit NOT BETWEEN 1 AND 1000 THEN RAISE EXCEPTION 'Batch size 1..1000 required'; END IF;
 IF p_global_serial THEN
  PERFORM pg_advisory_xact_lock(736482004);
  IF EXISTS(SELECT 1 FROM outbox WHERE status IN ('sending','uncertain')) THEN RETURN; END IF;
  p_limit:=1;
 END IF;
 FOR emp IN SELECT x.id,x.max_chat_id FROM employees x WHERE x.max_chat_id IS NOT NULL AND
  NOT EXISTS(SELECT 1 FROM messages m JOIN outbox o ON o.message_id=m.id WHERE m.employee_id=x.id AND o.status IN ('sending','uncertain')) AND EXISTS
  (SELECT 1 FROM messages m JOIN outbox o ON o.message_id=m.id WHERE m.employee_id=x.id AND o.status='pending' AND o.next_attempt_at<=clock_timestamp())
  ORDER BY x.id LIMIT p_limit FOR UPDATE OF x SKIP LOCKED
 LOOP
  -- Recheck after the per-employee lock; durable sending/uncertain survives COMMIT.
  IF EXISTS(SELECT 1 FROM messages m JOIN outbox o ON o.message_id=m.id WHERE m.employee_id=emp.id AND o.status IN ('sending','uncertain')) THEN CONTINUE; END IF;
  FOR item IN SELECT o.*,m.body,m.payload,m.reply_to_id,m.event_id FROM outbox o JOIN messages m ON m.id=o.message_id
   WHERE m.employee_id=emp.id AND o.status='pending' AND o.next_attempt_at<=clock_timestamp()
   ORDER BY m.id FOR UPDATE OF o SKIP LOCKED
  LOOP
   IF item.purpose='rag' THEN
    IF EXISTS(SELECT 1 FROM messages WHERE id=item.reply_to_id AND analysis_gate='pending') THEN CONTINUE; END IF;
    IF EXISTS(SELECT 1 FROM messages WHERE id=item.reply_to_id AND (case_id IS NOT NULL OR analysis_gate='hr')) OR
     EXISTS(SELECT 1 FROM answer_sources s WHERE s.message_id=item.message_id AND NOT source_is_accessible(emp.id,s.chunk_id)) THEN
     UPDATE outbox SET status='cancelled' WHERE outbox.message_id=item.message_id;
     UPDATE messages SET rag_status='pending',rag_lease_until=NULL,rag_next_attempt_at=clock_timestamp()
      WHERE id=item.reply_to_id AND case_id IS NULL AND analysis_gate<>'hr' AND rag_status='answered';
     CONTINUE;
    END IF;
   END IF;
   IF item.event_id IS NOT NULL AND (item.purpose IN ('event','reminder') OR item.payload->>'role'='prompt') THEN
    SELECT * INTO STRICT e FROM plan_events WHERE id=item.event_id;
    IF NOT EXISTS(SELECT 1 FROM employee_plans WHERE id=e.plan_id AND status='active') OR
     e.status NOT IN ('queued','awaiting_answer','awaiting_comment') OR
     (e.status='queued' AND e.send_until_at<=clock_timestamp()) OR
     (e.status IN ('awaiting_answer','awaiting_comment') AND e.deadline_at<=clock_timestamp()) OR
     (item.payload->>'question_code'='_legacy_comment' AND e.status<>'awaiting_comment') OR
     (e.kind='survey' AND (item.payload->>'question_code' IS DISTINCT FROM e.current_question_code OR
       item.payload->>'interaction_revision' IS DISTINCT FROM e.interaction_revision::text)) OR
     (item.purpose='reminder' AND e.status NOT IN ('awaiting_answer','awaiting_comment')) THEN
     UPDATE outbox SET status='cancelled' WHERE outbox.message_id=item.message_id;CONTINUE;
    END IF;
   END IF;
   UPDATE outbox SET status='sending',attempts=attempts+1,generation=outbox.generation+1,
    lease_until=clock_timestamp()+interval '2 minutes' WHERE outbox.message_id=item.message_id RETURNING outbox.generation INTO generation;
   message_id:=item.message_id;body:=item.body;max_chat_id:=emp.max_chat_id;payload:=item.payload;
   employee_id:=emp.id;plan_event_id:=item.event_id;
   RETURN NEXT;EXIT; -- at most one message for this employee, even within a batch
  END LOOP;
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION finish_send(p_message bigint,p_generation bigint,p_external text,p_error text DEFAULT NULL,p_uncertain boolean DEFAULT false)
 RETURNS void LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE m messages;o outbox;e plan_events;accepted timestamptz:=clock_timestamp();
BEGIN
 SELECT * INTO STRICT m FROM messages WHERE id=p_message;
 PERFORM 1 FROM employees WHERE id=m.employee_id FOR UPDATE;
 SELECT * INTO STRICT o FROM outbox WHERE message_id=p_message FOR UPDATE;
 IF o.status<>'sending' OR o.generation<>p_generation THEN RAISE EXCEPTION 'Stale delivery lease'; END IF;
 IF p_error IS NOT NULL THEN
  UPDATE outbox SET status=CASE WHEN p_uncertain THEN 'uncertain' WHEN attempts>=8 THEN 'dead' ELSE 'pending' END,
   last_error=left(p_error,2000),lease_until=NULL,next_attempt_at=clock_timestamp()+make_interval(secs=>LEAST(3600,10*(2^LEAST(attempts,8))::int)) WHERE message_id=p_message;
  RETURN;
 END IF;
 IF p_external IS NULL OR btrim(p_external)='' THEN RAISE EXCEPTION 'External message ID required'; END IF;
 UPDATE outbox SET status='accepted',external_message_id=p_external,accepted_at=accepted,lease_until=NULL,last_error=NULL WHERE message_id=p_message;
 IF o.purpose='event' THEN
  SELECT * INTO STRICT e FROM plan_events WHERE id=m.event_id FOR UPDATE;
  IF e.status='queued' THEN
   IF accepted>e.send_until_at THEN
    UPDATE plan_events SET status='overdue',next_reminder_at=NULL WHERE id=e.id;
   ELSE
    UPDATE plan_events SET status=CASE WHEN kind='info' THEN 'completed' ELSE 'awaiting_answer' END,
     completed_at=CASE WHEN kind='info' THEN accepted END,
     deadline_at=CASE WHEN kind<>'info' THEN accepted+make_interval(hours=>response_hours) END,
     next_reminder_at=CASE WHEN kind<>'info' AND max_reminders>0 THEN accepted+make_interval(hours=>reminder_hours) END
    WHERE id=e.id;
   END IF;
   PERFORM touch_context(m.employee_id);
  END IF;
 ELSIF o.purpose='hr_reply' THEN UPDATE hr_cases SET status='sent' WHERE id=m.case_id AND status='queued'; END IF;
END $$;

-- Same public legacy API; survey questions have their own durable answer rows.
CREATE OR REPLACE FUNCTION answer_event(p_employee bigint,p_event bigint,p_type text,p_value text) RETURNS text
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e plan_events;r integer;new_status text;
BEGIN
 PERFORM 1 FROM employees WHERE id=p_employee FOR UPDATE;
 SELECT * INTO STRICT e FROM plan_events WHERE id=p_event AND employee_id=p_employee FOR UPDATE;
 IF e.kind='survey' OR e.status NOT IN ('awaiting_answer','awaiting_comment') OR e.deadline_at<=clock_timestamp() THEN RAISE EXCEPTION 'Event is not awaiting this response'; END IF;
 IF e.kind='rating' AND p_type='rating' AND e.status='awaiting_answer' THEN
  r:=p_value::int;IF r IS NULL OR r NOT BETWEEN 1 AND 10 THEN RAISE EXCEPTION 'Rating 1..10 required'; END IF;
  UPDATE plan_events SET rating=r,answered_at=clock_timestamp(),status=CASE WHEN r<8 THEN 'awaiting_comment' ELSE 'completed' END,
   completed_at=CASE WHEN r>=8 THEN clock_timestamp() END,next_reminder_at=CASE WHEN r<8 THEN next_reminder_at END,
   interaction_revision=interaction_revision+1 WHERE id=p_event;
  IF r<8 THEN PERFORM queue_message(p_employee,'bot','Расскажите, пожалуйста, что можно улучшить.',
   'comment-prompt:'||p_event,'notice',p_event); END IF;
 ELSIF (e.kind='rating' AND e.status='awaiting_comment' AND p_type='comment') OR (e.kind='feedback' AND p_type='feedback') THEN
  IF p_value IS NULL OR btrim(p_value)='' THEN RAISE EXCEPTION 'Nonempty comment required'; END IF;
  UPDATE plan_events SET comment=btrim(p_value),answered_at=COALESCE(answered_at,clock_timestamp()),status='completed',
   completed_at=clock_timestamp(),next_reminder_at=NULL,interaction_revision=interaction_revision+1 WHERE id=p_event;
 ELSIF e.kind='task' AND p_type='task_confirm' AND p_value='yes' THEN
  UPDATE plan_events SET task_confirmed_at=clock_timestamp(),answered_at=clock_timestamp(),status='completed',
   completed_at=clock_timestamp(),next_reminder_at=NULL,interaction_revision=interaction_revision+1 WHERE id=p_event;
 ELSE RAISE EXCEPTION 'Response type does not match event'; END IF;
 UPDATE outbox o SET status='cancelled' FROM messages m WHERE m.id=o.message_id AND m.event_id=p_event AND o.status='pending'
  AND (o.purpose='reminder' OR (m.payload->>'question_code'='_legacy_comment' AND
    (SELECT status FROM plan_events WHERE id=p_event)<>'awaiting_comment'));
 PERFORM touch_context(p_employee);
 SELECT status INTO new_status FROM plan_events WHERE id=p_event;
 IF e.kind IN ('rating','feedback') THEN PERFORM enqueue_legacy_feedback(p_employee,p_event); END IF;
 RETURN new_status;
END $$;

CREATE FUNCTION set_employee_context(p_hr bigint,p_employee bigint,p_location text,p_type text,p_style text,p_probation_end date)
 RETURNS void LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 PERFORM 1 FROM employees WHERE id=p_employee AND hr_id=p_hr AND EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'HR access denied'; END IF;
 IF EXISTS(SELECT 1 FROM employees e JOIN employee_plans p ON p.employee_id=e.id AND p.status='active' WHERE e.id=p_employee AND
  (e.location_tag,e.employee_type) IS DISTINCT FROM (p_location,p_type)) THEN RAISE EXCEPTION 'Cancel/replace active plan before changing its audience'; END IF;
 UPDATE employees SET location_tag=p_location,employee_type=p_type,communication_style=p_style,probation_end_date=p_probation_end WHERE id=p_employee;
 INSERT INTO audit_log(employee_id,actor_hr_id,action) VALUES(p_employee,p_hr,'profile_context_updated');
END $$;
CREATE FUNCTION set_communication_style(p_employee bigint,p_style text,p_context_revision bigint) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 PERFORM 1 FROM employees WHERE id=p_employee AND context_revision=p_context_revision FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Stale preference context'; END IF;
 IF p_style IS NULL THEN RAISE EXCEPTION 'Choose informal or formal'; END IF;
 UPDATE employees SET communication_style=p_style WHERE id=p_employee;
END $$;
CREATE FUNCTION assigned_hr_contact(p_employee bigint) RETURNS TABLE(display_name text,contact_uri text)
 LANGUAGE sql STABLE SET search_path=korsik,pg_catalog AS $$
 SELECT h.display_name,h.contact_uri FROM employees e JOIN hr_users h ON h.id=e.hr_id
 WHERE e.id=p_employee AND h.enabled AND h.contact_verified_at IS NOT NULL
$$;
CREATE FUNCTION validate_pending_choices(p_employee bigint,choices jsonb) RETURNS boolean
 LANGUAGE plpgsql STABLE SET search_path=korsik,pg_catalog AS $$
DECLARE c jsonb; codes text[]:=ARRAY[]::text[];
BEGIN
 IF jsonb_typeof(choices) IS DISTINCT FROM 'array' THEN RETURN false; END IF;
 FOR c IN SELECT value FROM jsonb_array_elements(choices) LOOP
  IF NOT json_keys_allowed(c,ARRAY['code','label','route','event_id','question_code','interaction_revision','case_id']) OR
   jsonb_typeof(c->'code') IS DISTINCT FROM 'string' OR length(c->>'code') NOT BETWEEN 1 AND 50 OR (c->>'code')=ANY(codes) OR
   jsonb_typeof(c->'label') IS DISTINCT FROM 'string' OR length(c->>'label') NOT BETWEEN 1 AND 300 OR
   c->>'route' IS NULL OR c->>'route' NOT IN ('survey','legacy_answer','case_followup','rag_question','hr_request','plan_question') THEN RETURN false; END IF;
  codes:=array_append(codes,c->>'code');
  IF c->>'route' IN ('survey','legacy_answer') THEN
   IF NOT EXISTS(SELECT 1 FROM plan_events e WHERE e.id=(c->>'event_id')::bigint AND e.employee_id=p_employee
    AND e.status IN ('awaiting_answer','awaiting_comment') AND e.interaction_revision=(c->>'interaction_revision')::bigint
    AND e.deadline_at>clock_timestamp() AND
    ((c->>'route'='survey' AND e.kind='survey' AND e.current_question_code=c->>'question_code') OR (c->>'route'='legacy_answer' AND e.kind<>'survey'))) THEN RETURN false; END IF;
  ELSIF c ? 'event_id' OR c ? 'question_code' OR c ? 'interaction_revision' THEN RETURN false; END IF;
  IF c->>'route'='case_followup' THEN
   IF NOT EXISTS(SELECT 1 FROM hr_cases WHERE id=(c->>'case_id')::bigint AND employee_id=p_employee AND status='awaiting_hr') THEN RETURN false; END IF;
  ELSIF c ? 'case_id' THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;
CREATE FUNCTION guard_pending_interaction() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 IF TG_OP='UPDATE' AND OLD.status<>'pending' THEN RAISE EXCEPTION 'Clarification already finalized'; END IF;
 IF TG_OP='UPDATE' AND (NEW.employee_id,NEW.original_message_id,NEW.context_revision,NEW.choices,NEW.expires_at,NEW.created_at)
  IS DISTINCT FROM (OLD.employee_id,OLD.original_message_id,OLD.context_revision,OLD.choices,OLD.expires_at,OLD.created_at) THEN RAISE EXCEPTION 'Clarification source is immutable'; END IF;
 IF TG_OP='UPDATE' AND NEW.status='resolved' THEN
  IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.choices) c WHERE c->>'code'=NEW.chosen_code
    AND NEW.resolution=jsonb_build_object('original_message_id',NEW.original_message_id,'choice',c)
    AND validate_pending_choices(NEW.employee_id,jsonb_build_array(c))) THEN RAISE EXCEPTION 'Invalid clarification resolution'; END IF;
 END IF;
 IF TG_OP='INSERT' AND NOT validate_pending_choices(NEW.employee_id,NEW.choices) THEN RAISE EXCEPTION 'Invalid or unrelated clarification targets'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER pending_interactions_history BEFORE INSERT OR UPDATE ON pending_interactions FOR EACH ROW EXECUTE FUNCTION guard_pending_interaction();
CREATE FUNCTION create_pending_interaction(p_employee bigint,p_original bigint,p_revision bigint,p_choices jsonb,p_expires timestamptz)
 RETURNS bigint LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE id_value bigint; current_revision bigint;
BEGIN
 SELECT context_revision INTO STRICT current_revision FROM employees WHERE id=p_employee FOR UPDATE;
 IF EXISTS(SELECT 1 FROM pending_interactions WHERE original_message_id=p_original AND status='resolved') THEN RAISE EXCEPTION 'Original message already resolved'; END IF;
 IF current_revision IS DISTINCT FROM p_revision OR p_expires<=clock_timestamp() OR p_expires>clock_timestamp()+interval '7 days' THEN RAISE EXCEPTION 'Stale context or invalid clarification expiry'; END IF;
 PERFORM 1 FROM messages WHERE id=p_original AND employee_id=p_employee AND direction='in';
 IF NOT FOUND THEN RAISE EXCEPTION 'Original message not owned'; END IF;
 INSERT INTO pending_interactions(employee_id,original_message_id,context_revision,choices,expires_at)
 VALUES(p_employee,p_original,p_revision,p_choices,p_expires) RETURNING id INTO id_value;
 PERFORM queue_message(p_employee,'bot','Уточните, пожалуйста, к чему относится ваше сообщение.',
  'clarify:'||id_value,'notice',NULL,NULL,NULL,p_original,
  jsonb_build_object('role','notice','pending_interaction_id',id_value,'options',
   (SELECT jsonb_agg(jsonb_build_object('id',c->>'code','label',c->>'label')) FROM jsonb_array_elements(p_choices) c)));
 RETURN id_value;
END $$;
CREATE FUNCTION resolve_pending_interaction(p_employee bigint,p_pending bigint,p_code text,p_revision bigint)
 RETURNS jsonb LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE p pending_interactions; revision bigint; selected jsonb;
BEGIN
 SELECT context_revision INTO STRICT revision FROM employees WHERE id=p_employee FOR UPDATE;
 SELECT * INTO STRICT p FROM pending_interactions WHERE id=p_pending AND employee_id=p_employee FOR UPDATE;
 IF p.status<>'pending' OR p.expires_at<=clock_timestamp() OR revision IS DISTINCT FROM p_revision OR p.context_revision IS DISTINCT FROM revision THEN
  RAISE EXCEPTION 'Clarification is stale, expired or already resolved'; END IF;
 SELECT c INTO selected FROM jsonb_array_elements(p.choices) c WHERE c->>'code'=p_code;
 IF selected IS NULL OR NOT validate_pending_choices(p_employee,jsonb_build_array(selected)) THEN RAISE EXCEPTION 'Clarification target no longer valid'; END IF;
 UPDATE pending_interactions SET status='resolved',chosen_code=p_code,
  resolution=jsonb_build_object('original_message_id',p.original_message_id,'choice',selected),resolved_at=clock_timestamp() WHERE id=p_pending;
 -- Caller applies the returned choice to original_message_id in THIS transaction.
 -- Any handler failure must roll back this resolution as well.
 RETURN jsonb_build_object('original_message_id',p.original_message_id,'choice',selected);
END $$;
CREATE FUNCTION expire_pending_interactions() RETURNS integer LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE n integer;
BEGIN
 UPDATE pending_interactions SET status='expired' WHERE status='pending' AND expires_at<=clock_timestamp();
 GET DIAGNOSTICS n=ROW_COUNT;RETURN n;
END $$;
CREATE FUNCTION claim_message_analysis(p_model text,p_policy text) RETURNS SETOF message_analysis
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE selected bigint;
BEGIN
 IF p_model IS NULL OR p_policy IS NULL OR p_model='unconfigured' OR p_policy='unconfigured' THEN RAISE EXCEPTION 'Configure actual analysis model/policy versions before claiming'; END IF;
 SELECT id INTO selected FROM message_analysis WHERE model_version=p_model AND policy_version=p_policy
  AND status IN ('pending','error') AND attempts<8 AND next_attempt_at<=clock_timestamp()
  ORDER BY next_attempt_at,id LIMIT 1 FOR UPDATE SKIP LOCKED;
 IF selected IS NOT NULL THEN
  RETURN QUERY UPDATE message_analysis SET status='running',attempts=attempts+1,generation=generation+1,
   lease_until=clock_timestamp()+interval '5 minutes' WHERE id=selected RETURNING *;
 END IF;
END $$;
CREATE FUNCTION recover_analysis_leases() RETURNS integer LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE n integer;
BEGIN
 UPDATE message_analysis SET status=CASE WHEN attempts>=8 THEN 'dead' ELSE 'error' END,lease_until=NULL,
  last_error='ANALYSIS_LEASE_EXPIRED',next_attempt_at=clock_timestamp()+interval '1 minute'
 WHERE status='running' AND lease_until<clock_timestamp();
 GET DIAGNOSTICS n=ROW_COUNT;RETURN n;
END $$;
CREATE FUNCTION apply_hr_analysis(p_job bigint,p_generation bigint,p_result jsonb) RETURNS bigint
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE j message_analysis; cid bigint; issue_key text; priority_value text; category_value text; must_hr boolean; current_revision integer;
BEGIN
 SELECT * INTO STRICT j FROM message_analysis WHERE id=p_job;
 PERFORM 1 FROM employees WHERE id=j.employee_id FOR UPDATE;
 SELECT * INTO STRICT j FROM message_analysis WHERE id=p_job FOR UPDATE;
 IF j.status<>'running' OR j.generation<>p_generation THEN RAISE EXCEPTION 'Stale analysis result'; END IF;
 IF NOT COALESCE(json_keys_allowed(p_result,ARRAY['status','requires_hr','ticket','category','priority','full_result','topic','sentiment','problem_level','confidence']) AND
  p_result->>'status' IN ('ok','no_data','error') AND jsonb_typeof(p_result->'requires_hr')='boolean' AND
  p_result->>'priority' IN ('low','medium','high') AND jsonb_typeof(p_result->'full_result')='object' AND
  (NOT p_result ? 'ticket' OR p_result->'ticket'='null' OR jsonb_typeof(p_result->'ticket')='object'),false) THEN
  RAISE EXCEPTION 'Invalid normalized HRResult envelope'; END IF;
 IF j.source_question_code IS NOT NULL THEN
  SELECT revision INTO current_revision FROM event_answers WHERE event_id=j.source_event_id AND question_code=j.source_question_code;
  IF current_revision IS DISTINCT FROM j.input_revision THEN
   UPDATE message_analysis SET status='superseded',result=p_result,lease_until=NULL,analyzed_at=clock_timestamp() WHERE id=p_job;RETURN NULL;
  END IF;
 END IF;
 IF j.source_event_id IS NOT NULL AND j.source_question_code IS NULL AND j.source_key LIKE 'legacy-answer:%' AND
  (SELECT interaction_revision FROM plan_events WHERE id=j.source_event_id) IS DISTINCT FROM j.input_revision::bigint THEN
  UPDATE message_analysis SET status='superseded',result=p_result,lease_until=NULL,analyzed_at=clock_timestamp() WHERE id=p_job;RETURN NULL;
 END IF;
 must_hr:=(p_result->>'requires_hr')::boolean OR (p_result ? 'ticket' AND p_result->'ticket'<>'null');
 priority_value:=p_result->>'priority';category_value:=p_result->>'category';issue_key:=j.source_key;
 IF must_hr THEN
  SELECT id INTO cid FROM hr_cases WHERE source_key=issue_key AND status<>'closed' FOR UPDATE;
  IF cid IS NULL THEN cid:=open_hr_case(j.employee_id,j.message_id,CASE WHEN j.request->>'source'='hr_request' THEN 'explicit_human' ELSE 'problem' END,'Требуется участие закреплённого HR.'); END IF;
  UPDATE hr_cases SET source_key=issue_key,source_event_id=j.source_event_id,source_question_code=j.source_question_code,
   category=category_value,priority=CASE WHEN priority='high' OR priority_value='high' THEN 'high' ELSE priority_value END
   WHERE id=cid;
  UPDATE messages SET case_id=cid,analysis_gate='hr',rag_status='handed_to_hr',rag_lease_until=NULL WHERE id=j.message_id;
  UPDATE outbox o SET status='cancelled' FROM messages m WHERE m.id=o.message_id AND m.reply_to_id=j.message_id AND o.purpose='rag' AND o.status='pending';
 ELSIF p_result->>'status'<>'error' THEN
  UPDATE messages SET analysis_gate='clear' WHERE id=j.message_id AND analysis_gate='pending';
 END IF;
 UPDATE message_analysis SET result=p_result,category=category_value,topic=COALESCE(p_result->>'topic',category_value,'unknown'),
  sentiment=COALESCE(p_result->>'sentiment','unknown'),problem_level=(p_result->>'problem_level')::smallint,confidence=(p_result->>'confidence')::numeric,
  status=CASE WHEN p_result->>'status'='error' THEN CASE WHEN attempts>=8 THEN 'dead' ELSE 'error' END ELSE 'done' END,
  lease_until=NULL,last_error=CASE WHEN p_result->>'status'='error' THEN 'HR_SERVICE_ERROR' END,
  next_attempt_at=clock_timestamp()+interval '1 minute',analyzed_at=clock_timestamp(),case_id=cid WHERE id=p_job;
 RETURN cid;
END $$;
CREATE FUNCTION close_hr_case(p_hr bigint,p_case bigint,p_basis text,p_note text DEFAULT NULL) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE c hr_cases;
BEGIN
 SELECT * INTO STRICT c FROM hr_cases WHERE id=p_case;
 PERFORM 1 FROM employees WHERE id=c.employee_id AND hr_id=p_hr AND EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) FOR UPDATE;
 IF NOT FOUND OR c.hr_id<>p_hr THEN RAISE EXCEPTION 'HR access denied'; END IF;
 SELECT * INTO STRICT c FROM hr_cases WHERE id=p_case FOR UPDATE;
 IF NOT ((p_basis='verified_delivery' AND c.status='delivered') OR
   (p_basis='api_accepted' AND c.status='sent' AND length(btrim(p_note))>0)) THEN RAISE EXCEPTION 'Case closure policy not satisfied'; END IF;
 UPDATE hr_cases SET status='closed',closed_at=clock_timestamp(),closure_basis=p_basis,closure_note=p_note WHERE id=p_case;
 INSERT INTO audit_log(employee_id,actor_hr_id,action,details) VALUES(c.employee_id,p_hr,'case_closed',jsonb_build_object('case_id',p_case,'basis',p_basis));
END $$;

-- PK/FK additions for the delivery mutex: enforced even for accidental direct SQL.
ALTER TABLE outbox ADD COLUMN employee_id bigint;
UPDATE outbox o SET employee_id=m.employee_id FROM messages m WHERE m.id=o.message_id;
ALTER TABLE outbox ALTER COLUMN employee_id SET NOT NULL,
 ADD FOREIGN KEY(message_id,employee_id) REFERENCES messages(id,employee_id) ON DELETE RESTRICT;
CREATE UNIQUE INDEX outbox_one_inflight_per_employee ON outbox(employee_id) WHERE status IN ('sending','uncertain');
CREATE OR REPLACE FUNCTION validate_outbox_message() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE m messages;
BEGIN
 SELECT * INTO m FROM messages WHERE id=NEW.message_id;
 IF NOT FOUND THEN RETURN NEW; END IF;
 IF NEW.employee_id IS NULL THEN NEW.employee_id:=m.employee_id; END IF;
 IF m.direction<>'out' OR (NEW.purpose='hr_reply' AND m.author<>'hr') OR
  (NEW.purpose IN ('event','reminder') AND m.event_id IS NULL) OR
  (NEW.purpose='rag' AND (m.author<>'bot' OR m.reply_to_id IS NULL)) THEN RAISE EXCEPTION 'Message does not match outbox purpose'; END IF;
 RETURN NEW;
END $$;
-- summary category is useful for queue/analytics filters, full native result stays in result.
ALTER TABLE message_analysis ADD COLUMN category text;
CREATE INDEX message_analysis_source ON message_analysis(source_event_id,source_question_code,input_revision);
CREATE INDEX hr_cases_answer_source ON hr_cases(source_event_id,source_question_code) WHERE source_event_id IS NOT NULL;
ALTER TABLE plan_events ADD CHECK(status NOT IN ('awaiting_answer','awaiting_comment') OR deadline_at IS NOT NULL);


CREATE FUNCTION default_case_source() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 IF NEW.source_key IS NULL THEN NEW.source_key:='message:'||NEW.root_message_id; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER hr_cases_00_source BEFORE INSERT ON hr_cases FOR EACH ROW EXECUTE FUNCTION default_case_source();
CREATE FUNCTION guard_analysis_history() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 IF OLD.status IN ('done','superseded') AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'Final analysis result is immutable'; END IF;
 IF (NEW.message_id,NEW.employee_id,NEW.source_key,NEW.input_revision,NEW.model_version,NEW.policy_version,NEW.request,NEW.source_event_id,NEW.source_question_code)
 IS DISTINCT FROM (OLD.message_id,OLD.employee_id,OLD.source_key,OLD.input_revision,OLD.model_version,OLD.policy_version,OLD.request,OLD.source_event_id,OLD.source_question_code) THEN
  RAISE EXCEPTION 'Analysis input and versions are immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER message_analysis_history BEFORE UPDATE ON message_analysis FOR EACH ROW EXECUTE FUNCTION guard_analysis_history();

CREATE OR REPLACE FUNCTION claim_rag_question() RETURNS SETOF messages
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE emp record; q messages;
BEGIN
 FOR emp IN SELECT e.id FROM employees e WHERE EXISTS
  (SELECT 1 FROM messages m WHERE m.employee_id=e.id AND m.rag_status IN ('pending','error')
   AND m.rag_attempts<8 AND m.rag_next_attempt_at<=clock_timestamp() AND m.case_id IS NULL AND m.analysis_gate IN ('none','clear'))
  ORDER BY e.id FOR UPDATE OF e SKIP LOCKED
 LOOP
  SELECT * INTO q FROM messages WHERE employee_id=emp.id AND rag_status IN ('pending','error')
   AND rag_attempts<8 AND rag_next_attempt_at<=clock_timestamp() AND case_id IS NULL AND analysis_gate IN ('none','clear')
   ORDER BY rag_next_attempt_at,id LIMIT 1 FOR UPDATE SKIP LOCKED;
  IF FOUND THEN
   RETURN QUERY UPDATE messages SET rag_status='running',rag_attempts=rag_attempts+1,
    rag_generation=rag_generation+1,rag_lease_until=clock_timestamp()+interval '5 minutes'
    WHERE id=q.id RETURNING *;
   RETURN;
  END IF;
 END LOOP;
END $$;

CREATE OR REPLACE FUNCTION queue_rag_answer(p_question bigint,p_text text,p_chunks bigint[],p_model text,p_found boolean,p_generation bigint DEFAULT NULL)
 RETURNS bigint LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE q messages; mid bigint; cid bigint; chunk bigint; r integer:=0;
BEGIN
 SELECT * INTO STRICT q FROM messages WHERE id=p_question;
 PERFORM 1 FROM employees WHERE id=q.employee_id FOR UPDATE;
 SELECT * INTO STRICT q FROM messages WHERE id=p_question AND direction='in' FOR UPDATE;
 IF q.analysis_gate IN ('pending','hr') THEN RAISE EXCEPTION 'Mandatory analysis has not cleared this question'; END IF;
 IF q.case_id IS NOT NULL OR q.rag_status='handed_to_hr' THEN RAISE EXCEPTION 'Question belongs to HR'; END IF;
 IF q.rag_status NOT IN ('pending','running') OR
  (q.rag_status='running' AND p_generation IS DISTINCT FROM q.rag_generation) THEN
  RAISE EXCEPTION 'Stale RAG job'; END IF;
 IF p_found IS NULL OR p_model IS NULL OR (p_found AND COALESCE(cardinality(p_chunks),0)=0) OR
  (NOT p_found AND COALESCE(cardinality(p_chunks),0)>0) THEN RAISE EXCEPTION 'Invalid RAG result'; END IF;
 IF NOT p_found THEN
  UPDATE messages SET rag_model_version=p_model,rag_lease_until=NULL,rag_last_error=NULL WHERE id=p_question;
  cid:=open_hr_case(q.employee_id,p_question,'no_answer','Подтверждённого ответа в доступных документах нет.');
  SELECT id INTO STRICT mid FROM messages WHERE deduplication_key='case:'||cid||':notice';
  RETURN mid;
 END IF;
 -- Lock document headers in a stable order; publisher also locks headers first.
 PERFORM d.id FROM documents d WHERE d.id IN
  (SELECT v.document_id FROM document_versions v JOIN document_chunks c ON c.version_id=v.id WHERE c.id=ANY(p_chunks))
  ORDER BY d.id FOR SHARE;
 mid:=queue_message(q.employee_id,'bot',p_text,'rag:'||p_question||':'||q.rag_generation,'rag',NULL,NULL,NULL,p_question);
 FOREACH chunk IN ARRAY COALESCE(p_chunks,ARRAY[]::bigint[]) LOOP
  r:=r+1;
  INSERT INTO answer_sources(message_id,chunk_id,rank) VALUES(mid,chunk,r);
 END LOOP;
 UPDATE messages SET rag_status=CASE WHEN p_found THEN 'answered' ELSE 'not_found' END,
  rag_model_version=p_model,rag_lease_until=NULL,rag_last_error=NULL WHERE id=p_question;
 RETURN mid;
END $$;

CREATE OR REPLACE FUNCTION cancel_plan(p_hr bigint,p_plan bigint,p_stop_employee boolean DEFAULT true) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE p employee_plans;
BEGIN
 SELECT * INTO STRICT p FROM employee_plans WHERE id=p_plan;
 PERFORM 1 FROM employees WHERE id=p.employee_id AND hr_id=p_hr
  AND EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'HR access denied'; END IF;
 SELECT * INTO STRICT p FROM employee_plans WHERE id=p_plan AND status='active' FOR UPDATE;
 IF EXISTS(SELECT 1 FROM outbox o JOIN messages m ON m.id=o.message_id JOIN plan_events e ON e.id=m.event_id
  WHERE e.plan_id=p_plan AND o.status IN ('sending','uncertain')) THEN
  RAISE EXCEPTION 'Resolve in-flight/uncertain deliveries before cancelling plan'; END IF;
 UPDATE plan_events SET status='cancelled',next_reminder_at=NULL
  WHERE plan_id=p_plan AND status IN ('scheduled','queued','awaiting_answer','awaiting_comment');
 UPDATE outbox o SET status='cancelled' FROM messages m JOIN plan_events e ON e.id=m.event_id
  WHERE o.message_id=m.id AND e.plan_id=p_plan AND o.status='pending' AND m.payload->>'role' IS DISTINCT FROM 'completion';
 UPDATE employee_plans SET status='cancelled',ended_at=clock_timestamp() WHERE id=p_plan;
 IF p_stop_employee THEN UPDATE employees SET lifecycle='stopped' WHERE id=p.employee_id; END IF;
 INSERT INTO audit_log(employee_id,actor_hr_id,action,details)
 VALUES(p.employee_id,p_hr,'plan_cancelled',jsonb_build_object('plan_id',p_plan));
END $$;


CREATE FUNCTION queue_question_ack(p_event bigint,p_question text) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e plan_events;a event_answers;q jsonb;ack text;
BEGIN
 SELECT * INTO STRICT e FROM plan_events WHERE id=p_event;
 SELECT * INTO STRICT a FROM event_answers WHERE event_id=p_event AND question_code=p_question;
 q:=survey_question(e.definition,p_question);ack:=q->>'reply_text';
 IF a.answer_type='single_choice' THEN SELECT COALESCE(o->>'reply_text',ack) INTO ack FROM jsonb_array_elements(q->'options') o WHERE o->>'id'=a.value#>>'{}'; END IF;
 IF a.state='accepted' AND length(btrim(ack))>0 THEN
  PERFORM queue_message(e.employee_id,'bot',ack,'answer-ack:'||p_event||':'||p_question,'notice',e.id,NULL,NULL,NULL,
   jsonb_build_object('role','completion','question_code',p_question));
 END IF;
END $$;
CREATE FUNCTION bind_unconfigured_analysis(p_model text,p_policy text,p_limit integer DEFAULT 100) RETURNS integer
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE j message_analysis;n integer:=0;
BEGIN
 IF p_model IS NULL OR p_policy IS NULL OR p_model='unconfigured' OR p_policy='unconfigured' OR p_limit NOT BETWEEN 1 AND 1000 THEN RAISE EXCEPTION 'Actual model and policy versions required'; END IF;
 FOR j IN SELECT * FROM message_analysis WHERE status='pending' AND (model_version='unconfigured' OR policy_version='unconfigured')
  ORDER BY id LIMIT p_limit FOR UPDATE SKIP LOCKED LOOP
  INSERT INTO message_analysis(message_id,employee_id,source_key,input_revision,model_version,policy_version,request,source_event_id,source_question_code,status,analyzed_at)
   VALUES(j.message_id,j.employee_id,j.source_key,j.input_revision,p_model,p_policy,j.request,j.source_event_id,j.source_question_code,'pending',NULL)
   ON CONFLICT(source_key,input_revision,model_version,policy_version) DO NOTHING;
  UPDATE message_analysis SET status='superseded',result=jsonb_build_object('administrative_rebind',true,'new_model_version',p_model,'new_policy_version',p_policy) WHERE id=j.id;
  n:=n+1;
 END LOOP;
 RETURN n;
END $$;


ALTER TABLE documents ADD COLUMN scope_location_tag text CHECK(scope_location_tag IN ('GrPP','GTP','Luzhniki'));
CREATE OR REPLACE FUNCTION source_is_accessible(p_employee bigint,p_chunk bigint) RETURNS boolean
 LANGUAGE sql STABLE SET search_path=korsik,pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM document_chunks c JOIN document_versions v ON v.id=c.version_id
 JOIN documents d ON d.id=v.document_id JOIN employees e ON e.id=p_employee
 WHERE c.id=p_chunk AND d.enabled AND v.is_current AND v.index_status='ready'
 AND v.valid_from<=statement_timestamp() AND (v.valid_until IS NULL OR v.valid_until>statement_timestamp())
 AND (d.scope_org_unit_id IS NULL OR d.scope_org_unit_id=e.org_unit_id)
 AND (d.scope_location_tag IS NULL OR d.scope_location_tag=e.location_tag))
$$;


CREATE FUNCTION guard_survey_progress() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE q jsonb;
BEGIN
 IF NEW.kind<>'survey' THEN RETURN NEW; END IF;
 IF NEW.current_question_code IS NOT NULL AND survey_question(NEW.definition,NEW.current_question_code) IS NULL THEN RAISE EXCEPTION 'Unknown current question code'; END IF;
 IF NEW.status IN ('queued','awaiting_answer','awaiting_comment') AND NEW.current_question_code IS NULL THEN RAISE EXCEPTION 'Interactive survey requires a current question'; END IF;
 IF NEW.status IN ('queued','awaiting_answer','awaiting_comment') AND NOT question_enabled(NEW.id,survey_question(NEW.definition,NEW.current_question_code)) THEN RAISE EXCEPTION 'Current question condition is false'; END IF;
 IF NEW.status='awaiting_comment' AND NOT EXISTS(SELECT 1 FROM event_answers WHERE event_id=NEW.id AND question_code=NEW.current_question_code AND state='awaiting_comment') THEN RAISE EXCEPTION 'Missing comment-required answer'; END IF;
 IF NEW.status='completed' THEN
  IF NEW.current_question_code IS NOT NULL THEN RAISE EXCEPTION 'Completed survey must release its question'; END IF;
  FOR q IN SELECT value FROM jsonb_array_elements(NEW.definition->'questions') LOOP
   IF question_enabled(NEW.id,q) AND NOT EXISTS(SELECT 1 FROM event_answers WHERE event_id=NEW.id AND question_code=q->>'code' AND state IN ('accepted','skipped')) THEN
    RAISE EXCEPTION 'Cannot complete survey with unfinished enabled questions'; END IF;
  END LOOP;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER plan_events_survey_progress BEFORE INSERT OR UPDATE ON plan_events FOR EACH ROW EXECUTE FUNCTION guard_survey_progress();


CREATE FUNCTION enqueue_legacy_feedback(p_employee bigint,p_event bigint) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e plan_events;mid bigint;raw_source text;body_text text;provider text;
BEGIN
 SELECT * INTO STRICT e FROM plan_events WHERE id=p_event AND employee_id=p_employee;
 IF e.kind NOT IN ('rating','feedback') THEN RETURN; END IF;
 body_text:=CASE WHEN e.kind='rating' THEN e.rating::text ELSE '' END;
 IF e.comment IS NOT NULL THEN body_text:=btrim(body_text||E'\n'||e.comment); END IF;
 raw_source:=NULLIF(current_setting('korsik.source_message_id',true),'');
 IF raw_source IS NOT NULL THEN
  mid:=raw_source::bigint;
  IF NOT EXISTS(SELECT 1 FROM messages WHERE id=mid AND employee_id=p_employee AND direction='in') THEN RAISE EXCEPTION 'Invalid legacy feedback source message'; END IF;
 ELSE
  INSERT INTO messages(employee_id,direction,author,body,event_id,payload)
  VALUES(p_employee,'in','employee',body_text,p_event,'{"kind":"action","text_origin":"employee"}') RETURNING id INTO mid;
 END IF;
 SELECT inbox_event_id INTO provider FROM messages WHERE id=mid;
 PERFORM enqueue_analysis(p_employee,mid,'legacy-answer:'||p_event,e.interaction_revision::int,
  jsonb_build_object('source','survey','employee_id',p_employee::text,'provider_event_id',provider,'survey_session_id',p_event::text,
   'question_id',e.code,'question',e.body,'score',e.rating,'text',body_text,'scale_direction',CASE WHEN e.kind='rating' THEN 'low_bad' END),p_event,NULL);
END $$;

GRANT SELECT,INSERT,UPDATE ON event_answers,pending_interactions,message_analysis TO korsik_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA korsik TO korsik_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA korsik FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA korsik TO korsik_app;
INSERT INTO schema_migrations VALUES(3,clock_timestamp(),'Typed surveys, calendar, delivery serialization, router context and durable HR analysis');
COMMIT;
