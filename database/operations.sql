\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = korsik, pg_catalog;

CREATE FUNCTION validate_timezone() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_timezone_names WHERE name=NEW.timezone) THEN
  RAISE EXCEPTION 'Unknown IANA timezone: %',NEW.timezone;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER employees_timezone BEFORE INSERT OR UPDATE OF timezone ON employees
 FOR EACH ROW EXECUTE FUNCTION validate_timezone();
CREATE TRIGGER plans_timezone BEFORE INSERT OR UPDATE OF timezone ON employee_plans
 FOR EACH ROW EXECUTE FUNCTION validate_timezone();

CREATE FUNCTION guard_status() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a text; b text; allowed boolean := false;
BEGIN
 IF TG_TABLE_NAME='employees' THEN
  a:=OLD.lifecycle; b:=NEW.lifecycle;
  allowed := (a,b) IN (('invited','registering'),('registering','hr_review'),
   ('hr_review','registering'),('hr_review','ready'),('ready','adapting'),
   ('adapting','completed'),('invited','stopped'),('registering','stopped'),
   ('hr_review','stopped'),('ready','stopped'),('adapting','stopped'));
 ELSIF TG_TABLE_NAME='plan_events' THEN
  a:=OLD.status;b:=NEW.status;
  allowed := (a,b) IN (('scheduled','queued'),('scheduled','cancelled'),('scheduled','skipped'),
   ('queued','awaiting_answer'),('queued','completed'),('queued','overdue'),('queued','cancelled'),
   ('awaiting_answer','awaiting_comment'),('awaiting_answer','completed'),
   ('awaiting_answer','overdue'),('awaiting_answer','cancelled'),('awaiting_answer','skipped'),
   ('awaiting_comment','completed'),('awaiting_comment','overdue'),('awaiting_comment','cancelled'));
 ELSIF TG_TABLE_NAME='hr_cases' THEN
  a:=OLD.status;b:=NEW.status;
  allowed := (a,b) IN (('awaiting_hr','answered'),('answered','queued'),
   ('queued','sent'),('sent','delivered'),('delivered','closed'));
 ELSIF TG_TABLE_NAME='employee_plans' THEN
  a:=OLD.status;b:=NEW.status;
  allowed := a='active' AND b IN ('completed','cancelled');
  IF NEW.version_id<>OLD.version_id OR NEW.employee_id<>OLD.employee_id THEN
   RAISE EXCEPTION 'Plan owner and template version are immutable';
  END IF;
 END IF;
 IF a IS DISTINCT FROM b AND NOT allowed THEN
  RAISE EXCEPTION 'Invalid transition in %: % -> %',TG_TABLE_NAME,a,b;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER employees_status BEFORE UPDATE ON employees FOR EACH ROW EXECUTE FUNCTION guard_status();
CREATE TRIGGER plan_events_status BEFORE UPDATE ON plan_events FOR EACH ROW EXECUTE FUNCTION guard_status();
CREATE TRIGGER hr_cases_status BEFORE UPDATE ON hr_cases FOR EACH ROW EXECUTE FUNCTION guard_status();
CREATE TRIGGER employee_plans_status BEFORE UPDATE ON employee_plans FOR EACH ROW EXECUTE FUNCTION guard_status();

CREATE FUNCTION immutable_published_version() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 IF OLD.published_at IS NOT NULL THEN RAISE EXCEPTION 'Published version is immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF NEW.published_at IS NOT NULL THEN
  IF NOT EXISTS(SELECT 1 FROM template_events WHERE version_id=NEW.id) THEN
   RAISE EXCEPTION 'Empty plan version';
  END IF;
  IF EXISTS(SELECT 1 FROM template_events e JOIN template_events d ON d.id=e.depends_on_id
   WHERE e.version_id=NEW.id AND d.position>=e.position) THEN
   RAISE EXCEPTION 'Dependency must reference an earlier position';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER plan_versions_immutable BEFORE UPDATE OR DELETE ON plan_versions
 FOR EACH ROW EXECUTE FUNCTION immutable_published_version();
CREATE FUNCTION guard_template_event() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE v bigint; published timestamptz;
BEGIN
 IF TG_OP='UPDATE' AND NEW.version_id<>OLD.version_id THEN
  RAISE EXCEPTION 'Cannot move a template event between versions';
 END IF;
 IF TG_OP='DELETE' THEN v:=OLD.version_id; ELSE v:=NEW.version_id; END IF;
 SELECT published_at INTO published FROM plan_versions WHERE id=v FOR UPDATE;
 IF published IS NOT NULL THEN RAISE EXCEPTION 'Published events are immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER template_events_immutable BEFORE INSERT OR UPDATE OR DELETE ON template_events
 FOR EACH ROW EXECUTE FUNCTION guard_template_event();

CREATE FUNCTION guard_event_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status IN ('completed','overdue','skipped','cancelled') AND
  (NEW.rating,NEW.comment,NEW.task_confirmed_at,NEW.answered_at,NEW.completed_at)
  IS DISTINCT FROM (OLD.rating,OLD.comment,OLD.task_confirmed_at,OLD.answered_at,OLD.completed_at) THEN
  RAISE EXCEPTION 'Terminal event response is immutable';
 END IF;
 IF (NEW.plan_id,NEW.employee_id,NEW.version_id,NEW.template_event_id,NEW.code,NEW.position,NEW.kind)
  IS DISTINCT FROM (OLD.plan_id,OLD.employee_id,OLD.version_id,OLD.template_event_id,OLD.code,OLD.position,OLD.kind) THEN
  RAISE EXCEPTION 'Event identity and origin are immutable';
 END IF;
 IF OLD.status<>'scheduled' AND
  (NEW.body,NEW.offset_months,NEW.offset_days,NEW.local_time,NEW.response_hours,NEW.required,
   NEW.reminder_hours,NEW.max_reminders,NEW.scheduled_at,NEW.deadline_at,NEW.depends_on_id)
  IS DISTINCT FROM
  (OLD.body,OLD.offset_months,OLD.offset_days,OLD.local_time,OLD.response_hours,OLD.required,
   OLD.reminder_hours,OLD.max_reminders,OLD.scheduled_at,OLD.deadline_at,OLD.depends_on_id) THEN
  RAISE EXCEPTION 'Queued event configuration is immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER plan_events_history BEFORE UPDATE ON plan_events FOR EACH ROW EXECUTE FUNCTION guard_event_history();

CREATE FUNCTION guard_invitation_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.activated_at IS NOT NULL OR OLD.revoked_at IS NOT NULL THEN
  RAISE EXCEPTION 'Consumed/revoked invitation is immutable';
 END IF;
 IF (NEW.employee_id,NEW.token_hash,NEW.created_at,NEW.expires_at)
  IS DISTINCT FROM (OLD.employee_id,OLD.token_hash,OLD.created_at,OLD.expires_at) THEN
  RAISE EXCEPTION 'Issue a new invitation instead of rewriting it';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER invitations_history BEFORE UPDATE ON invitations FOR EACH ROW EXECUTE FUNCTION guard_invitation_history();

CREATE FUNCTION validate_case_owner() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE h bigint;
BEGIN
 SELECT hr_id INTO STRICT h FROM employees WHERE id=NEW.employee_id FOR UPDATE;
 IF NEW.hr_id<>h THEN RAISE EXCEPTION 'Case must go to the assigned HR'; END IF;
 PERFORM 1 FROM messages WHERE id=NEW.root_message_id AND employee_id=NEW.employee_id AND direction='in';
 IF NOT FOUND THEN RAISE EXCEPTION 'Case root must be an incoming message of this employee'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER hr_cases_assigned_owner BEFORE INSERT ON hr_cases FOR EACH ROW EXECUTE FUNCTION validate_case_owner();

CREATE FUNCTION validate_plan_origin() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e employees;
BEGIN
 SELECT * INTO STRICT e FROM employees WHERE id=NEW.employee_id FOR UPDATE;
 IF e.hr_id<>NEW.assigned_by OR e.lifecycle NOT IN ('ready','adapting') OR
  NOT EXISTS(SELECT 1 FROM hr_users WHERE id=NEW.assigned_by AND enabled) THEN
  RAISE EXCEPTION 'Plan must be assigned by the enabled, assigned HR'; END IF;
 PERFORM 1 FROM plan_versions WHERE id=NEW.version_id AND published_at IS NOT NULL FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Only published versions can be assigned'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER employee_plans_published_origin BEFORE INSERT ON employee_plans
 FOR EACH ROW EXECUTE FUNCTION validate_plan_origin();

CREATE FUNCTION guard_message_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.employee_id,NEW.direction,NEW.author,NEW.author_hr_id,NEW.body,NEW.deduplication_key,
   NEW.inbox_event_id,NEW.reply_to_id,NEW.event_id,NEW.created_at)
  IS DISTINCT FROM
  (OLD.employee_id,OLD.direction,OLD.author,OLD.author_hr_id,OLD.body,OLD.deduplication_key,
   OLD.inbox_event_id,OLD.reply_to_id,OLD.event_id,OLD.created_at) THEN
  RAISE EXCEPTION 'Persisted message identity, authorship, text and reply context are immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER messages_history BEFORE UPDATE ON messages FOR EACH ROW EXECUTE FUNCTION guard_message_history();

CREATE FUNCTION guard_document_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.indexed_at IS NOT NULL THEN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Indexed document version is immutable'; END IF;
  IF (NEW.document_id,NEW.version_no,NEW.object_uri,NEW.sha256,NEW.valid_from,NEW.indexed_at)
   IS DISTINCT FROM (OLD.document_id,OLD.version_no,OLD.object_uri,OLD.sha256,OLD.valid_from,OLD.indexed_at) THEN
   RAISE EXCEPTION 'Indexed content identity is immutable; create a new version';
  END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER document_versions_history BEFORE UPDATE OR DELETE ON document_versions
 FOR EACH ROW EXECUTE FUNCTION guard_document_version();

CREATE FUNCTION guard_document_chunk() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE v bigint; indexed timestamptz;
BEGIN
 IF TG_OP='UPDATE' AND NEW.version_id<>OLD.version_id THEN RAISE EXCEPTION 'Cannot move chunk between versions'; END IF;
 IF TG_OP='DELETE' THEN v:=OLD.version_id; ELSE v:=NEW.version_id; END IF;
 SELECT indexed_at INTO indexed FROM document_versions WHERE id=v FOR UPDATE;
 IF indexed IS NOT NULL THEN RAISE EXCEPTION 'Indexed chunks are immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER document_chunks_history BEFORE INSERT OR UPDATE OR DELETE ON document_chunks
 FOR EACH ROW EXECUTE FUNCTION guard_document_chunk();

CREATE FUNCTION guard_inbox_result() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.event_id<>OLD.event_id OR NEW.received_at<>OLD.received_at THEN
  RAISE EXCEPTION 'Provider event identity is immutable'; END IF;
 IF OLD.status='done' AND
  (NEW.status,NEW.employee_id,NEW.result,NEW.generation) IS DISTINCT FROM
  (OLD.status,OLD.employee_id,OLD.result,OLD.generation) THEN
  RAISE EXCEPTION 'Completed webhook cannot be reapplied'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER max_inbox_result_history BEFORE UPDATE ON max_inbox FOR EACH ROW EXECUTE FUNCTION guard_inbox_result();

CREATE FUNCTION validate_outbox_message() RETURNS trigger LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE m messages;
BEGIN
 SELECT * INTO m FROM messages WHERE id=NEW.message_id;
 IF NOT FOUND THEN RETURN NEW; END IF; -- Let the FK report an unknown message.
 IF m.direction<>'out' OR (NEW.purpose='hr_reply' AND m.author<>'hr') OR
  (NEW.purpose IN ('event','reminder') AND m.event_id IS NULL) OR
  (NEW.purpose='rag' AND (m.author<>'bot' OR m.reply_to_id IS NULL)) THEN
  RAISE EXCEPTION 'Message does not match outbox purpose'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER outbox_message_kind BEFORE INSERT OR UPDATE OF message_id,purpose ON outbox
 FOR EACH ROW EXECUTE FUNCTION validate_outbox_message();

CREATE FUNCTION activate_invitation(p_hash bytea,p_account text,p_chat text)
 RETURNS bigint LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE inv invitations; emp employees; eid bigint;
BEGIN
 IF p_account IS NULL OR btrim(p_account)='' OR p_chat IS NULL OR btrim(p_chat)='' THEN
  RAISE EXCEPTION 'MAX account and chat are required';
 END IF;
 SELECT employee_id INTO eid FROM invitations WHERE token_hash=p_hash;
 IF eid IS NULL THEN RAISE EXCEPTION 'Invalid invitation'; END IF;
 SELECT * INTO STRICT emp FROM employees WHERE id=eid FOR UPDATE;
 SELECT * INTO STRICT inv FROM invitations WHERE token_hash=p_hash FOR UPDATE;
 IF inv.activated_at IS NOT NULL OR inv.revoked_at IS NOT NULL OR
  inv.expires_at<=clock_timestamp() OR emp.lifecycle<>'invited' OR emp.max_account_id IS NOT NULL THEN
  RAISE EXCEPTION 'Invitation cannot be activated';
 END IF;
 UPDATE employees SET max_account_id=p_account,max_chat_id=p_chat,lifecycle='registering' WHERE id=eid;
 UPDATE invitations SET activated_at=clock_timestamp(),activated_account_id=p_account WHERE id=inv.id;
 INSERT INTO audit_log(employee_id,action) VALUES(eid,'invitation_activated');
 RETURN eid;
END $$;

CREATE FUNCTION save_registration(p_employee bigint,p_revision integer,p_field text,p_value text)
 RETURNS integer LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e employees;
BEGIN
 SELECT * INTO STRICT e FROM employees WHERE id=p_employee FOR UPDATE;
 IF e.lifecycle<>'registering' OR e.registration_revision<>p_revision THEN
  RAISE EXCEPTION 'Registration state changed; reload current step';
 END IF;
 IF p_field='full_name' THEN
  IF p_value IS NULL OR btrim(p_value)='' THEN RAISE EXCEPTION 'Full name required'; END IF;
  UPDATE employees SET full_name=btrim(p_value),registration_step=CASE WHEN phone IS NULL THEN 'phone' ELSE 'confirm' END,
   registration_revision=registration_revision+1 WHERE id=p_employee;
 ELSIF p_field='phone' THEN
  IF e.full_name IS NULL OR p_value IS NULL THEN RAISE EXCEPTION 'Name and phone required'; END IF;
  UPDATE employees SET phone=p_value,registration_step='confirm',registration_revision=registration_revision+1 WHERE id=p_employee;
 ELSIF p_field='confirm' THEN
  IF e.registration_step<>'confirm' OR p_value IS DISTINCT FROM 'yes' THEN
   RAISE EXCEPTION 'Explicit confirmation required';
  END IF;
  UPDATE employees SET submitted_at=clock_timestamp(),registration_step='submitted',
   lifecycle='hr_review',registration_revision=registration_revision+1 WHERE id=p_employee;
 ELSE RAISE EXCEPTION 'Unsupported registration field';
 END IF;
 RETURN p_revision+1;
END $$;

CREATE FUNCTION verify_registration(p_hr bigint,p_employee bigint,p_unit bigint,
 p_job text,p_start date,p_timezone text) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e employees;
BEGIN
 SELECT * INTO STRICT e FROM employees WHERE id=p_employee FOR UPDATE;
 IF e.hr_id<>p_hr OR e.lifecycle<>'hr_review' OR
  NOT EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) THEN
  RAISE EXCEPTION 'Not assigned HR or questionnaire not submitted';
 END IF;
 IF p_unit IS NULL OR p_job IS NULL OR btrim(p_job)='' OR p_start IS NULL OR p_timezone IS NULL THEN
  RAISE EXCEPTION 'HR fields required';
 END IF;
 UPDATE employees SET org_unit_id=p_unit,job_title=p_job,start_date=p_start,timezone=p_timezone,
  hr_verified_at=clock_timestamp(),lifecycle='ready' WHERE id=p_employee;
 INSERT INTO audit_log(employee_id,actor_hr_id,action) VALUES(p_employee,p_hr,'hr_verified');
END $$;

CREATE FUNCTION event_time(p_date date,p_timezone text,p_months integer,p_days integer,p_time time)
 RETURNS timestamptz LANGUAGE sql STABLE STRICT AS $$
 SELECT ((p_date + make_interval(months=>p_months,days=>p_days))::date+p_time) AT TIME ZONE p_timezone
$$;

CREATE FUNCTION assign_plan(p_hr bigint,p_employee bigint,p_version bigint)
 RETURNS bigint LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE e employees; pid bigint;
BEGIN
 SELECT * INTO STRICT e FROM employees WHERE id=p_employee FOR UPDATE;
 IF e.hr_id<>p_hr OR e.lifecycle NOT IN ('ready','adapting') OR e.max_account_id IS NULL OR
  NOT EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) THEN RAISE EXCEPTION 'Plan assignment denied'; END IF;
 PERFORM 1 FROM plan_versions WHERE id=p_version AND published_at IS NOT NULL FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Published version required'; END IF;
 INSERT INTO employee_plans(employee_id,version_id,assigned_by,start_date,timezone)
 VALUES(p_employee,p_version,p_hr,e.start_date,e.timezone) RETURNING id INTO pid;
 INSERT INTO plan_events(plan_id,employee_id,version_id,template_event_id,code,position,kind,body,
  offset_months,offset_days,local_time,response_hours,required,reminder_hours,max_reminders,scheduled_at,deadline_at)
 SELECT pid,p_employee,p_version,t.id,t.code,t.position,t.kind,t.body,
  t.offset_months,t.offset_days,t.local_time,t.response_hours,t.required,t.reminder_hours,t.max_reminders,
  x.run_at,x.run_at+make_interval(hours=>t.response_hours)
 FROM template_events t CROSS JOIN LATERAL
 (SELECT event_time(e.start_date,e.timezone,t.offset_months,t.offset_days,t.local_time) run_at) x
 WHERE t.version_id=p_version ORDER BY t.position;
 UPDATE plan_events target SET depends_on_id=dep.id
 FROM template_events src,plan_events dep
 WHERE target.plan_id=pid AND src.id=target.template_event_id AND
 dep.plan_id=pid AND dep.template_event_id=src.depends_on_id;
 UPDATE employees SET lifecycle='adapting' WHERE id=p_employee;
 INSERT INTO audit_log(employee_id,actor_hr_id,action,details)
 VALUES(p_employee,p_hr,'plan_assigned',jsonb_build_object('plan_id',pid,'version_id',p_version));
 RETURN pid;
END $$;

CREATE FUNCTION queue_message(p_employee bigint,p_author text,p_body text,p_key text,
 p_purpose text,p_event bigint DEFAULT NULL,p_case bigint DEFAULT NULL,
 p_hr bigint DEFAULT NULL,p_reply bigint DEFAULT NULL) RETURNS bigint
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE mid bigint; m messages;
BEGIN
 INSERT INTO messages(employee_id,direction,author,author_hr_id,body,deduplication_key,event_id,case_id,reply_to_id)
 VALUES(p_employee,'out',p_author,p_hr,p_body,p_key,p_event,p_case,p_reply)
 ON CONFLICT(deduplication_key) DO NOTHING RETURNING id INTO mid;
 IF mid IS NULL THEN
  SELECT * INTO STRICT m FROM messages WHERE deduplication_key=p_key;
  IF (m.employee_id,m.author,m.author_hr_id,m.body,m.event_id,m.case_id,m.reply_to_id)
   IS DISTINCT FROM (p_employee,p_author,p_hr,p_body,p_event,p_case,p_reply) THEN
   RAISE EXCEPTION 'Idempotency key reused with different content';
  END IF;
  mid:=m.id;
 END IF;
 INSERT INTO outbox(message_id,purpose) VALUES(mid,p_purpose) ON CONFLICT DO NOTHING;
 RETURN mid;
END $$;

-- Short transaction only. The employee row is the per-employee mutex.
CREATE FUNCTION prepare_due_events(p_limit integer DEFAULT 100) RETURNS integer
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE emp record; ev plan_events; n integer:=0;
BEGIN
 IF p_limit NOT BETWEEN 1 AND 1000 THEN RAISE EXCEPTION 'Batch size 1..1000 required'; END IF;
 FOR emp IN SELECT e.id FROM employees e WHERE e.lifecycle='adapting' AND EXISTS
  (SELECT 1 FROM plan_events s JOIN employee_plans p ON p.id=s.plan_id
   WHERE s.employee_id=e.id AND p.status='active' AND
    ((s.status='scheduled' AND s.scheduled_at<=clock_timestamp()) OR
     (s.status IN ('queued','awaiting_answer','awaiting_comment') AND s.deadline_at<=clock_timestamp()) OR
     (s.status IN ('awaiting_answer','awaiting_comment') AND s.next_reminder_at<=clock_timestamp()
      AND s.reminders_sent<s.max_reminders)))
  ORDER BY e.id LIMIT p_limit FOR UPDATE OF e SKIP LOCKED
 LOOP
  FOR ev IN SELECT s.* FROM plan_events s JOIN employee_plans p ON p.id=s.plan_id
   WHERE s.employee_id=emp.id AND p.status='active' AND s.status IN
    ('scheduled','queued','awaiting_answer','awaiting_comment')
   ORDER BY s.position FOR UPDATE OF s SKIP LOCKED
  LOOP
   IF ev.deadline_at<=clock_timestamp() THEN
    UPDATE plan_events SET status=CASE WHEN status='scheduled' THEN 'skipped' ELSE 'overdue' END,
     next_reminder_at=NULL WHERE id=ev.id;
    UPDATE outbox o SET status='cancelled' FROM messages m WHERE m.id=o.message_id AND m.event_id=ev.id AND o.status='pending';
   ELSIF ev.status='scheduled' AND ev.scheduled_at<=clock_timestamp() AND
    (ev.depends_on_id IS NULL OR EXISTS(SELECT 1 FROM plan_events d WHERE d.id=ev.depends_on_id AND d.status='completed')) THEN
    PERFORM queue_message(emp.id,'bot',ev.body,'event:'||ev.id,'event',ev.id);
    UPDATE plan_events SET status='queued' WHERE id=ev.id;
    n:=n+1;
   ELSIF ev.status IN ('awaiting_answer','awaiting_comment') AND ev.next_reminder_at<=clock_timestamp()
    AND ev.reminders_sent<ev.max_reminders THEN
    PERFORM queue_message(emp.id,'bot','Напоминаю об этапе: '||ev.body,
     'reminder:'||ev.id||':'||(ev.reminders_sent+1),'reminder',ev.id);
    UPDATE plan_events SET reminders_sent=reminders_sent+1,
     next_reminder_at=clock_timestamp()+make_interval(hours=>reminder_hours) WHERE id=ev.id;
    n:=n+1;
   END IF;
  END LOOP;
 END LOOP;
 RETURN n;
END $$;

CREATE FUNCTION answer_event(p_employee bigint,p_event bigint,p_type text,p_value text)
 RETURNS text LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE ev plan_events; r integer;
BEGIN
 PERFORM 1 FROM employees WHERE id=p_employee FOR UPDATE;
 SELECT * INTO STRICT ev FROM plan_events WHERE id=p_event AND employee_id=p_employee FOR UPDATE;
 IF ev.status NOT IN ('awaiting_answer','awaiting_comment') OR ev.deadline_at<=clock_timestamp() THEN
  RAISE EXCEPTION 'Event is not awaiting this response';
 END IF;
 IF ev.kind='rating' AND p_type='rating' AND ev.status='awaiting_answer' THEN
  r:=p_value::integer;
  IF r IS NULL OR r NOT BETWEEN 1 AND 10 THEN RAISE EXCEPTION 'Rating 1..10 required'; END IF;
  UPDATE plan_events SET rating=r,answered_at=clock_timestamp(),
   status=CASE WHEN r<8 THEN 'awaiting_comment' ELSE 'completed' END,
   completed_at=CASE WHEN r>=8 THEN clock_timestamp() END,
   next_reminder_at=CASE WHEN r<8 THEN next_reminder_at END WHERE id=p_event;
  IF r<8 THEN PERFORM queue_message(p_employee,'bot','Расскажите, пожалуйста, что можно улучшить.',
   'comment-prompt:'||p_event,'notice',p_event); END IF;
 ELSIF (ev.kind='rating' AND ev.status='awaiting_comment' AND p_type='comment') OR
  (ev.kind='feedback' AND p_type='feedback') THEN
  IF p_value IS NULL OR btrim(p_value)='' THEN RAISE EXCEPTION 'Nonempty comment required'; END IF;
  UPDATE plan_events SET comment=btrim(p_value),answered_at=COALESCE(answered_at,clock_timestamp()),
   status='completed',completed_at=clock_timestamp(),next_reminder_at=NULL WHERE id=p_event;
 ELSIF ev.kind='task' AND p_type='task_confirm' AND p_value='yes' THEN
  UPDATE plan_events SET task_confirmed_at=clock_timestamp(),answered_at=clock_timestamp(),
   status='completed',completed_at=clock_timestamp(),next_reminder_at=NULL WHERE id=p_event;
 ELSE RAISE EXCEPTION 'Response type does not match event'; END IF;
 UPDATE outbox o SET status='cancelled' FROM messages m WHERE m.id=o.message_id
  AND m.event_id=p_event AND o.purpose='reminder' AND o.status='pending';
 SELECT status INTO ev.status FROM plan_events WHERE id=p_event;
 RETURN ev.status;
END $$;

CREATE FUNCTION open_hr_case(p_employee bigint,p_message bigint,p_reason text,p_context text)
 RETURNS bigint LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE h bigint; cid bigint; m messages;
BEGIN
 SELECT hr_id INTO STRICT h FROM employees WHERE id=p_employee FOR UPDATE;
 SELECT * INTO STRICT m FROM messages WHERE id=p_message AND employee_id=p_employee AND direction='in' FOR UPDATE;
 IF m.case_id IS NOT NULL THEN RETURN m.case_id; END IF;
 -- Do not race an already-started network call. Retry transfer after reconciliation.
 IF EXISTS(SELECT 1 FROM outbox o JOIN messages x ON x.id=o.message_id
  WHERE x.reply_to_id=p_message AND o.purpose='rag' AND o.status IN ('sending','uncertain')) THEN
  RAISE EXCEPTION 'RAG delivery is in flight or uncertain; retry after reconciliation';
 END IF;
 INSERT INTO hr_cases(employee_id,hr_id,root_message_id,reason,context_summary)
 VALUES(p_employee,h,p_message,p_reason,p_context) RETURNING id INTO cid;
 UPDATE messages SET case_id=cid,rag_status='handed_to_hr',rag_lease_until=NULL WHERE id=p_message;
 UPDATE outbox o SET status='cancelled' FROM messages x WHERE x.id=o.message_id
  AND x.reply_to_id=p_message AND o.purpose='rag' AND o.status='pending';
 PERFORM queue_message(p_employee,'bot','Передал ваш вопрос закреплённому HR. Вернусь с ответом.',
  'case:'||cid||':notice','notice',NULL,cid,NULL,p_message);
 INSERT INTO audit_log(employee_id,action,details) VALUES(p_employee,'case_opened',jsonb_build_object('case_id',cid));
 RETURN cid;
END $$;

CREATE FUNCTION prepare_hr_reply(p_hr bigint,p_case bigint,p_text text) RETURNS bigint
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE c hr_cases; mid bigint;
BEGIN
 SELECT * INTO STRICT c FROM hr_cases WHERE id=p_case;
 PERFORM 1 FROM employees WHERE id=c.employee_id AND hr_id=p_hr
  AND EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) FOR UPDATE;
 IF NOT FOUND OR c.hr_id<>p_hr OR NOT EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) THEN
  RAISE EXCEPTION 'HR access denied'; END IF;
 SELECT * INTO STRICT c FROM hr_cases WHERE id=p_case FOR UPDATE;
 IF c.status<>'awaiting_hr' THEN RAISE EXCEPTION 'Case already answered'; END IF;
 -- Both transitions are validated; the final queued state is visible after COMMIT.
 UPDATE hr_cases SET status='answered' WHERE id=p_case;
 mid:=queue_message(c.employee_id,'hr',p_text,'case:'||p_case||':reply','hr_reply',NULL,p_case,p_hr,c.root_message_id);
 UPDATE hr_cases SET status='queued' WHERE id=p_case;
 INSERT INTO audit_log(employee_id,actor_hr_id,action,details)
 VALUES(c.employee_id,p_hr,'hr_reply_queued',jsonb_build_object('case_id',p_case,'message_id',mid));
 RETURN mid;
END $$;

CREATE FUNCTION source_is_accessible(p_employee bigint,p_chunk bigint) RETURNS boolean
 LANGUAGE sql STABLE SET search_path=korsik,pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM document_chunks c JOIN document_versions v ON v.id=c.version_id
 JOIN documents d ON d.id=v.document_id JOIN employees e ON e.id=p_employee
 WHERE c.id=p_chunk AND d.enabled AND v.is_current AND v.index_status='ready'
 AND v.valid_from<=statement_timestamp() AND (v.valid_until IS NULL OR v.valid_until>statement_timestamp())
 AND (d.scope_org_unit_id IS NULL OR d.scope_org_unit_id=e.org_unit_id))
$$;
CREATE FUNCTION validate_answer_source() RETURNS trigger LANGUAGE plpgsql
 SET search_path=korsik,pg_catalog AS $$
DECLARE e bigint;
BEGIN
 SELECT employee_id INTO STRICT e FROM messages WHERE id=NEW.message_id AND direction='out' AND author='bot';
 IF NOT source_is_accessible(e,NEW.chunk_id) THEN RAISE EXCEPTION 'Source is outdated or outside employee access'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER answer_sources_access BEFORE INSERT OR UPDATE ON answer_sources
 FOR EACH ROW EXECUTE FUNCTION validate_answer_source();

CREATE FUNCTION queue_rag_answer(p_question bigint,p_text text,p_chunks bigint[],p_model text,p_found boolean,p_generation bigint DEFAULT NULL)
 RETURNS bigint LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE q messages; mid bigint; chunk bigint; r integer:=0;
BEGIN
 SELECT * INTO STRICT q FROM messages WHERE id=p_question;
 PERFORM 1 FROM employees WHERE id=q.employee_id FOR UPDATE;
 SELECT * INTO STRICT q FROM messages WHERE id=p_question AND direction='in' FOR UPDATE;
 IF q.case_id IS NOT NULL OR q.rag_status='handed_to_hr' THEN RAISE EXCEPTION 'Question belongs to HR'; END IF;
 IF q.rag_status NOT IN ('pending','running') OR
  (q.rag_status='running' AND p_generation IS DISTINCT FROM q.rag_generation) THEN
  RAISE EXCEPTION 'Stale RAG job'; END IF;
 IF p_found IS NULL OR p_model IS NULL OR (p_found AND COALESCE(cardinality(p_chunks),0)=0) OR
  (NOT p_found AND COALESCE(cardinality(p_chunks),0)>0) THEN RAISE EXCEPTION 'Invalid RAG result'; END IF;
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

CREATE FUNCTION claim_rag_question() RETURNS SETOF messages
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE emp record; q messages;
BEGIN
 FOR emp IN SELECT e.id FROM employees e WHERE EXISTS
  (SELECT 1 FROM messages m WHERE m.employee_id=e.id AND m.rag_status IN ('pending','error')
   AND m.rag_attempts<8 AND m.rag_next_attempt_at<=clock_timestamp() AND m.case_id IS NULL)
  ORDER BY e.id FOR UPDATE OF e SKIP LOCKED
 LOOP
  SELECT * INTO q FROM messages WHERE employee_id=emp.id AND rag_status IN ('pending','error')
   AND rag_attempts<8 AND rag_next_attempt_at<=clock_timestamp() AND case_id IS NULL
   ORDER BY rag_next_attempt_at,id LIMIT 1 FOR UPDATE SKIP LOCKED;
  IF FOUND THEN
   RETURN QUERY UPDATE messages SET rag_status='running',rag_attempts=rag_attempts+1,
    rag_generation=rag_generation+1,rag_lease_until=clock_timestamp()+interval '5 minutes'
    WHERE id=q.id RETURNING *;
   RETURN;
  END IF;
 END LOOP;
END $$;

CREATE FUNCTION fail_rag_question(p_question bigint,p_generation bigint,p_error_code text) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE eid bigint;
BEGIN
 SELECT employee_id INTO STRICT eid FROM messages WHERE id=p_question;
 PERFORM 1 FROM employees WHERE id=eid FOR UPDATE;
 UPDATE messages SET rag_status='error',rag_lease_until=NULL,rag_last_error=left(p_error_code,100),
  rag_next_attempt_at=clock_timestamp()+interval '1 minute'
 WHERE id=p_question AND rag_status='running' AND rag_generation=p_generation;
 IF NOT FOUND THEN RAISE EXCEPTION 'Stale RAG job'; END IF;
END $$;

CREATE FUNCTION recover_rag_leases() RETURNS integer
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE n integer;
BEGIN
 UPDATE messages SET rag_status='error',rag_lease_until=NULL,rag_last_error='RAG_LEASE_EXPIRED',
  rag_next_attempt_at=clock_timestamp()+interval '1 minute'
 WHERE rag_status='running' AND rag_lease_until<clock_timestamp();
 GET DIAGNOSTICS n=ROW_COUNT;
 RETURN n;
END $$;

-- Claim across employees. No network calls until this function's transaction commits.
CREATE FUNCTION claim_outbox(p_limit integer DEFAULT 50)
 RETURNS TABLE(message_id bigint,generation bigint,body text,max_chat_id text)
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE emp record; item record; count_claimed integer:=0;
BEGIN
 IF p_limit NOT BETWEEN 1 AND 1000 THEN RAISE EXCEPTION 'Batch size 1..1000 required'; END IF;
 FOR emp IN SELECT e.id,e.max_chat_id FROM employees e WHERE e.max_chat_id IS NOT NULL AND EXISTS
  (SELECT 1 FROM messages m JOIN outbox o ON o.message_id=m.id WHERE m.employee_id=e.id
   AND o.status='pending' AND o.next_attempt_at<=clock_timestamp())
  -- Serialize messages within one dialog across concurrent sender workers.
  AND NOT EXISTS(SELECT 1 FROM messages m JOIN outbox o ON o.message_id=m.id
   WHERE m.employee_id=e.id AND o.status='sending')
  ORDER BY e.id LIMIT p_limit FOR UPDATE OF e SKIP LOCKED
 LOOP
  FOR item IN SELECT o.*,m.body,m.reply_to_id,m.event_id,m.case_id FROM outbox o JOIN messages m ON m.id=o.message_id
   WHERE m.employee_id=emp.id AND o.status='pending' AND o.next_attempt_at<=clock_timestamp()
   ORDER BY o.next_attempt_at,o.message_id LIMIT p_limit FOR UPDATE OF o SKIP LOCKED
  LOOP
   IF count_claimed>=p_limit THEN RETURN; END IF;
   IF item.purpose='rag' AND
    (EXISTS(SELECT 1 FROM messages q WHERE q.id=item.reply_to_id AND q.case_id IS NOT NULL) OR
     EXISTS(SELECT 1 FROM answer_sources s WHERE s.message_id=item.message_id AND NOT source_is_accessible(emp.id,s.chunk_id))) THEN
    UPDATE outbox SET status='cancelled' WHERE outbox.message_id=item.message_id;
    UPDATE messages SET rag_status='pending',rag_lease_until=NULL,rag_next_attempt_at=clock_timestamp()
     WHERE id=item.reply_to_id AND case_id IS NULL AND rag_status='answered';
    CONTINUE;
   END IF;
   IF item.purpose IN ('event','reminder') AND NOT EXISTS
    (SELECT 1 FROM plan_events v JOIN employee_plans p ON p.id=v.plan_id WHERE v.id=item.event_id
     AND p.status='active' AND v.status IN ('queued','awaiting_answer','awaiting_comment') AND v.deadline_at>clock_timestamp()) THEN
    UPDATE outbox SET status='cancelled' WHERE outbox.message_id=item.message_id;
    CONTINUE;
   END IF;
   UPDATE outbox SET status='sending',attempts=attempts+1,generation=outbox.generation+1,
    lease_until=clock_timestamp()+interval '2 minutes' WHERE outbox.message_id=item.message_id
    RETURNING outbox.generation INTO generation;
   message_id:=item.message_id;body:=item.body;max_chat_id:=emp.max_chat_id;
   count_claimed:=count_claimed+1;RETURN NEXT;
  END LOOP;
 END LOOP;
END $$;

CREATE FUNCTION configure_event(p_hr bigint,p_event bigint,p_response_hours integer,p_max_reminders integer,p_local_time time)
 RETURNS void LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE ev plan_events; p employee_plans; run_at timestamptz;
BEGIN
 SELECT * INTO STRICT ev FROM plan_events WHERE id=p_event;
 PERFORM 1 FROM employees WHERE id=ev.employee_id AND hr_id=p_hr
  AND EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'HR access denied'; END IF;
 SELECT * INTO STRICT ev FROM plan_events WHERE id=p_event AND status='scheduled' FOR UPDATE;
 SELECT * INTO STRICT p FROM employee_plans WHERE id=ev.plan_id AND status='active';
 run_at:=event_time(p.start_date,p.timezone,ev.offset_months,ev.offset_days,p_local_time);
 UPDATE plan_events SET response_hours=p_response_hours,max_reminders=p_max_reminders,
  local_time=p_local_time,scheduled_at=run_at,deadline_at=run_at+make_interval(hours=>p_response_hours) WHERE id=p_event;
 INSERT INTO audit_log(employee_id,actor_hr_id,action,details)
 VALUES(ev.employee_id,p_hr,'event_configured',jsonb_build_object('event_id',p_event));
END $$;

CREATE FUNCTION finish_send(p_message bigint,p_generation bigint,p_external text,
 p_error text DEFAULT NULL,p_uncertain boolean DEFAULT false) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE m messages; o outbox; ev plan_events;
BEGIN
 SELECT * INTO STRICT m FROM messages WHERE id=p_message;
 PERFORM 1 FROM employees WHERE id=m.employee_id FOR UPDATE;
 SELECT * INTO STRICT o FROM outbox WHERE message_id=p_message FOR UPDATE;
 IF o.status<>'sending' OR o.generation<>p_generation THEN RAISE EXCEPTION 'Stale delivery lease'; END IF;
 IF p_error IS NOT NULL THEN
  UPDATE outbox SET status=CASE WHEN p_uncertain THEN 'uncertain' WHEN attempts>=8 THEN 'dead' ELSE 'pending' END,
   last_error=left(p_error,2000),lease_until=NULL,
   next_attempt_at=clock_timestamp()+make_interval(secs=>LEAST(3600,10*(2^LEAST(attempts,8))::int))
   WHERE message_id=p_message;
  RETURN;
 END IF;
 IF p_external IS NULL OR btrim(p_external)='' THEN RAISE EXCEPTION 'External message ID required'; END IF;
 UPDATE outbox SET status='accepted',external_message_id=p_external,accepted_at=clock_timestamp(),
  lease_until=NULL,last_error=NULL WHERE message_id=p_message;
 IF o.purpose='event' THEN
  SELECT * INTO STRICT ev FROM plan_events WHERE id=m.event_id FOR UPDATE;
  IF ev.status='queued' THEN
   UPDATE plan_events SET status=CASE WHEN kind='info' THEN 'completed' ELSE 'awaiting_answer' END,
    completed_at=CASE WHEN kind='info' THEN clock_timestamp() END,
    next_reminder_at=CASE WHEN kind<>'info' AND max_reminders>0 THEN clock_timestamp()+make_interval(hours=>reminder_hours) END
    WHERE id=ev.id;
  END IF;
 ELSIF o.purpose='hr_reply' THEN UPDATE hr_cases SET status='sent' WHERE id=m.case_id AND status='queued'; END IF;
END $$;

CREATE FUNCTION confirm_delivery(p_external text,p_delivered_at timestamptz) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE mid bigint; m messages;
BEGIN
 SELECT message_id INTO STRICT mid FROM outbox WHERE external_message_id=p_external;
 SELECT * INTO STRICT m FROM messages WHERE id=mid;
 PERFORM 1 FROM employees WHERE id=m.employee_id FOR UPDATE;
 UPDATE outbox SET status='delivered',delivered_at=p_delivered_at
  WHERE message_id=mid AND status='accepted';
 IF FOUND THEN
  UPDATE hr_cases SET status='delivered' WHERE id=m.case_id AND status='sent'
   AND EXISTS(SELECT 1 FROM outbox WHERE message_id=mid AND purpose='hr_reply');
 END IF;
END $$;

CREATE FUNCTION recover_leases() RETURNS TABLE(inbox_recovered integer,outbox_uncertain integer)
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
BEGIN
 UPDATE max_inbox SET status=CASE WHEN attempts>=8 THEN 'dead' ELSE 'pending' END,
  lease_until=NULL,last_error='Expired processing lease',next_attempt_at=clock_timestamp()
 WHERE status='processing' AND lease_until<clock_timestamp();
 GET DIAGNOSTICS inbox_recovered=ROW_COUNT;
 -- An expired send may have reached MAX. Do not automatically repeat an ambiguous call.
 UPDATE outbox SET status='uncertain',lease_until=NULL,last_error='Expired sending lease; reconcile with MAX'
 WHERE status='sending' AND lease_until<clock_timestamp();
 GET DIAGNOSTICS outbox_uncertain=ROW_COUNT;
 RETURN NEXT;
END $$;

CREATE FUNCTION reschedule_plan(p_hr bigint,p_plan bigint,p_start date,p_timezone text) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE p employee_plans;
BEGIN
 SELECT * INTO STRICT p FROM employee_plans WHERE id=p_plan;
 PERFORM 1 FROM employees WHERE id=p.employee_id AND hr_id=p_hr
  AND EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'HR access denied'; END IF;
 SELECT * INTO STRICT p FROM employee_plans WHERE id=p_plan AND status='active' FOR UPDATE;
 UPDATE employee_plans SET start_date=p_start,timezone=p_timezone WHERE id=p_plan;
 UPDATE employees SET start_date=p_start,timezone=p_timezone WHERE id=p.employee_id;
 -- Only never-queued events move. Already sent/queued prompts and their deadlines stay historical.
 UPDATE plan_events SET scheduled_at=event_time(p_start,p_timezone,offset_months,offset_days,local_time),
  deadline_at=event_time(p_start,p_timezone,offset_months,offset_days,local_time)+make_interval(hours=>response_hours)
  WHERE plan_id=p_plan AND status='scheduled';
 INSERT INTO audit_log(employee_id,actor_hr_id,action,details)
 VALUES(p.employee_id,p_hr,'plan_rescheduled',jsonb_build_object('plan_id',p_plan,'start_date',p_start,'timezone',p_timezone));
END $$;

CREATE FUNCTION cancel_plan(p_hr bigint,p_plan bigint,p_stop_employee boolean DEFAULT true) RETURNS void
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
  WHERE o.message_id=m.id AND e.plan_id=p_plan AND o.status='pending';
 UPDATE employee_plans SET status='cancelled',ended_at=clock_timestamp() WHERE id=p_plan;
 IF p_stop_employee THEN UPDATE employees SET lifecycle='stopped' WHERE id=p.employee_id; END IF;
 INSERT INTO audit_log(employee_id,actor_hr_id,action,details)
 VALUES(p.employee_id,p_hr,'plan_cancelled',jsonb_build_object('plan_id',p_plan));
END $$;

CREATE FUNCTION complete_plan(p_hr bigint,p_plan bigint) RETURNS void
 LANGUAGE plpgsql SET search_path=korsik,pg_catalog AS $$
DECLARE p employee_plans;
BEGIN
 SELECT * INTO STRICT p FROM employee_plans WHERE id=p_plan;
 PERFORM 1 FROM employees WHERE id=p.employee_id AND hr_id=p_hr
  AND EXISTS(SELECT 1 FROM hr_users WHERE id=p_hr AND enabled) FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'HR access denied'; END IF;
 SELECT * INTO STRICT p FROM employee_plans WHERE id=p_plan AND status='active' FOR UPDATE;
 IF EXISTS(SELECT 1 FROM plan_events WHERE plan_id=p_plan AND
  (status NOT IN ('completed','overdue','skipped','cancelled') OR (required AND status<>'completed'))) THEN
  RAISE EXCEPTION 'Unresolved events or incomplete required event'; END IF;
 UPDATE employee_plans SET status='completed',ended_at=clock_timestamp() WHERE id=p_plan;
 UPDATE employees SET lifecycle='completed' WHERE id=p.employee_id;
 INSERT INTO audit_log(employee_id,actor_hr_id,action) VALUES(p.employee_id,p_hr,'plan_completed');
END $$;

CREATE VIEW hr_dashboard AS
 SELECT e.hr_id,e.id employee_id,e.full_name,e.lifecycle,
 EXISTS(SELECT 1 FROM hr_cases c WHERE c.employee_id=e.id AND c.hr_id=e.hr_id AND c.status='awaiting_hr') requires_reply,
 p.id plan_id,
 count(s.id) FILTER (WHERE s.required) required_total,
 count(s.id) FILTER (WHERE s.required AND s.status='completed') required_completed,
 count(s.id) FILTER (WHERE s.status IN ('overdue','skipped')) missed
 FROM employees e LEFT JOIN employee_plans p ON p.employee_id=e.id AND p.status='active'
 LEFT JOIN plan_events s ON s.plan_id=p.id GROUP BY e.id,p.id;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA korsik FROM PUBLIC;
INSERT INTO schema_migrations VALUES(2,clock_timestamp(),'Transactional operations and guards');
COMMIT;
