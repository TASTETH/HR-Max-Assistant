\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
-- All test changes are rolled back, including the temporary helper function.
CREATE FUNCTION korsik.test_reject(p_sql text,p_states text[]) RETURNS void LANGUAGE plpgsql AS $$
DECLARE caught boolean:=false;
BEGIN
 BEGIN
  EXECUTE p_sql;
 EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE=ANY(p_states) THEN caught:=true;
  ELSE RAISE EXCEPTION 'Unexpected SQLSTATE %: % for %',SQLSTATE,SQLERRM,p_sql; END IF;
 END;
 IF NOT caught THEN RAISE EXCEPTION 'Expected rejection, but statement succeeded: %',p_sql; END IF;
END $$;
DO $$
DECLARE e1 bigint; e2 bigint; h bigint; v bigint; p bigint; survey bigint; ev bigint;
 mid bigint; gen bigint; claimed record; n integer; token bytea; batch_count integer;
BEGIN
 SELECT id,hr_id INTO STRICT e1,h FROM employees WHERE external_employee_id='DEMO-001';
 SELECT id INTO STRICT e2 FROM employees WHERE external_employee_id='DEMO-002';
 SELECT id,version_id INTO STRICT p,v FROM employee_plans WHERE employee_id=e1 AND status='active';
 SELECT id INTO STRICT survey FROM plan_events WHERE plan_id=p AND code='day3';
 PERFORM test_reject('INSERT INTO employees(hr_id) VALUES(9223372036854775807)',ARRAY['23503']);
 SELECT id INTO STRICT ev FROM plan_events WHERE plan_id=p AND code='day14';
 PERFORM test_reject(format('UPDATE plan_events SET rating=11 WHERE id=%s',ev),ARRAY['23514']);
 PERFORM test_reject(format('UPDATE plan_events SET rating=0 WHERE id=%s',ev),ARRAY['23514']);
 PERFORM test_reject(format('UPDATE plan_events SET comment=NULL WHERE id=%s',survey),ARRAY['P0001']);
 PERFORM test_reject(format('UPDATE plan_events SET task_confirmed_at=now() WHERE id=%s',ev),ARRAY['23514']);
 PERFORM test_reject($q$SELECT activate_invitation(sha256(convert_to('DEMO-NOT-A-SECRET-1','UTF8')),'other-person','other-chat')$q$,ARRAY['P0001']);
 PERFORM test_reject($q$INSERT INTO max_inbox(event_id,payload) VALUES('demo-in-hr','{}')$q$,ARRAY['23505']);
 PERFORM test_reject($q$UPDATE max_inbox SET status='pending' WHERE event_id='demo-in-hr'$q$,ARRAY['P0001']);
 -- event_id remains globally unique even if the retransmission is dated another month.
 PERFORM test_reject($q$INSERT INTO max_inbox(event_id,payload,received_at)
  VALUES('demo-in-hr','{}',date_trunc('month',now())+interval '1 month')$q$,ARRAY['23505']);
 PERFORM test_reject(format('INSERT INTO messages(employee_id,direction,author,body,event_id) VALUES(%s,''in'',''employee'',''wrong owner'',%s)',e2,survey),ARRAY['23503']);
 PERFORM test_reject(format('SELECT answer_event(%s,%s,''rating'',''8'')',e2,survey),ARRAY['P0002']);
 PERFORM test_reject(format('SELECT assign_plan(%s,%s,%s)',h,e1,v),ARRAY['23505']);
 PERFORM test_reject(format('UPDATE employees SET lifecycle=''invited'' WHERE id=%s',e1),ARRAY['P0001']);
 PERFORM test_reject(format('UPDATE plan_events SET status=''queued'',completed_at=NULL WHERE id=%s',survey),ARRAY['P0001']);
 PERFORM test_reject(format('UPDATE template_events SET body=''changed'' WHERE version_id=%s',v),ARRAY['P0001']);
 PERFORM test_reject(format('UPDATE plan_versions SET published_at=NULL WHERE id=%s',v),ARRAY['P0001']);
 PERFORM test_reject(format('UPDATE employees SET timezone=''Not/AZone'' WHERE id=%s',e1),ARRAY['P0001']);
 PERFORM test_reject(format('UPDATE hr_cases SET status=''closed'',closed_at=now() WHERE employee_id=%s',e1),ARRAY['P0001']);
 PERFORM test_reject(format('INSERT INTO messages(employee_id,direction,author,body,case_id) SELECT %s,''in'',''employee'',''wrong case'',id FROM hr_cases WHERE employee_id=%s',e2,e1),ARRAY['23503']);

 -- Scheduler: force one future task to become due; a second tick creates nothing twice.
 SELECT id INTO STRICT ev FROM plan_events WHERE plan_id=p AND code='task';
 UPDATE plan_events SET scheduled_at=clock_timestamp()-interval '1 minute',deadline_at=clock_timestamp()+interval '2 days' WHERE id=ev;
 PERFORM prepare_due_events(100);
 IF (SELECT status FROM plan_events WHERE id=ev)<>'queued' THEN RAISE EXCEPTION 'Due task not queued'; END IF;
 PERFORM prepare_due_events(100);
 IF (SELECT count(*) FROM messages WHERE event_id=ev AND deduplication_key='event:'||ev)<>1 THEN
  RAISE EXCEPTION 'Scheduler duplicate'; END IF;
 -- Future stage remains untouched.
 IF (SELECT status FROM plan_events WHERE plan_id=p AND code='month1')<>'scheduled' THEN
  RAISE EXCEPTION 'Future event selected too early'; END IF;

 -- Sending: deterministic external failure -> backoff -> new fenced lease -> success.
 LOOP
 batch_count:=0;
 FOR claimed IN SELECT * FROM claim_outbox(100) LOOP
  batch_count:=batch_count+1;
  IF EXISTS(SELECT 1 FROM messages WHERE id=claimed.message_id AND event_id=ev) THEN
   mid:=claimed.message_id;gen:=claimed.generation;
  ELSE
   PERFORM finish_send(claimed.message_id,claimed.generation,'test-other-'||claimed.message_id);
  END IF;
 END LOOP;
 EXIT WHEN mid IS NOT NULL OR batch_count=0;
 END LOOP;
 IF mid IS NULL THEN RAISE EXCEPTION 'Outbox did not claim task'; END IF;
 PERFORM finish_send(mid,gen,NULL,'HTTP 429: explicitly rejected, not accepted',false);
 IF NOT EXISTS(SELECT 1 FROM outbox WHERE message_id=mid AND status='pending' AND attempts=1 AND next_attempt_at>clock_timestamp()) THEN
  RAISE EXCEPTION 'Backoff missing'; END IF;
 UPDATE outbox SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE message_id=mid;
 SELECT * INTO STRICT claimed FROM claim_outbox(100) WHERE message_id=mid;
 PERFORM test_reject(format('SELECT finish_send(%s,%s,''stale'')',mid,gen),ARRAY['P0001']);
 PERFORM finish_send(mid,claimed.generation,'test-task-'||mid);
 IF (SELECT status FROM plan_events WHERE id=ev)<>'awaiting_answer' THEN RAISE EXCEPTION 'Task not awaiting confirmation'; END IF;
 PERFORM answer_event(e1,ev,'task_confirm','yes');
 -- Provider can deliver before the backend records the API acknowledgement.
 PERFORM confirm_delivery('test-task-'||mid,clock_timestamp()-interval '10 seconds');
 PERFORM confirm_delivery('test-task-'||mid,clock_timestamp());
 IF (SELECT status FROM outbox WHERE message_id=mid)<>'delivered' THEN RAISE EXCEPTION 'Receipt missing'; END IF;

 -- Lease expiry must become uncertain, not blindly retry an ambiguous external send.
 mid:=queue_message(e1,'bot','Тест неопределённого результата','test:uncertain','notice');
 SELECT * INTO STRICT claimed FROM claim_outbox(100) WHERE message_id=mid;
 UPDATE outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE message_id=mid;
 PERFORM recover_leases();
 IF (SELECT status FROM outbox WHERE message_id=mid)<>'uncertain' THEN RAISE EXCEPTION 'Expired lease not recovered'; END IF;

 IF event_time(DATE '2026-01-31','Europe/Moscow',1,0,TIME '09:00') IS DISTINCT FROM TIMESTAMPTZ '2026-02-28 06:00:00+00' THEN
  RAISE EXCEPTION 'Calendar-month computation failed'; END IF;
 IF NOT EXISTS(SELECT 1 FROM plan_events WHERE employee_id=e1 AND rating=7 AND comment IS NOT NULL AND status='completed') OR
  NOT EXISTS(SELECT 1 FROM plan_events WHERE employee_id=e2 AND rating=9 AND comment IS NULL AND status='completed') THEN
  RAISE EXCEPTION 'Rating/comment rule failed'; END IF;
 -- Validate no accidental identical indexes. FK support UNIQUEs with additional columns are intentional.
 IF EXISTS(SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
  JOIN pg_namespace ns ON ns.oid=c.relnamespace WHERE ns.nspname='korsik'
  GROUP BY i.indrelid,i.indclass::text,i.indkey::text,i.indoption::text,coalesce(i.indpred::text,''),coalesce(i.indexprs::text,'')
  HAVING count(*)>1) THEN RAISE EXCEPTION 'Duplicate physical index definitions'; END IF;
 RAISE NOTICE 'All SQL invariant checks passed';
END $$;
ROLLBACK;
