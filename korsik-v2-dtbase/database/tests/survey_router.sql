\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
SET LOCAL korsik.analysis_model_version='hr-contract-v1';
SET LOCAL korsik.analysis_policy_version='policy-contract-v1';
CREATE FUNCTION korsik.test_reject(p_sql text,p_states text[]) RETURNS void LANGUAGE plpgsql AS $$
DECLARE caught boolean:=false;
BEGIN
 BEGIN EXECUTE p_sql;
 EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE=ANY(p_states) THEN caught:=true;ELSE RAISE EXCEPTION 'Unexpected state %: % for %',SQLSTATE,SQLERRM,p_sql; END IF;
 END;
 IF NOT caught THEN RAISE EXCEPTION 'Expected rejection: %',p_sql; END IF;
END $$;
DO $$
DECLARE emp bigint;other_emp bigint;ev bigint;hr bigint;plan bigint;ver bigint;rev bigint;m bigint;pid bigint;context_rev bigint;
 a event_answers;e plan_events;job message_analysis;cid bigint;result jsonb;item record;round_count integer;
BEGIN
 SELECT id,hr_id INTO STRICT emp,hr FROM employees WHERE external_employee_id='DEMO-001';
 SELECT id INTO STRICT other_emp FROM employees WHERE external_employee_id='DEMO-002';
 SELECT id,version_id INTO STRICT plan,ver FROM employee_plans WHERE employee_id=emp AND status='active';
 SELECT id INTO STRICT ev FROM plan_events WHERE plan_id=plan AND code='survey9';
 IF jsonb_array_length((SELECT definition->'questions' FROM plan_events WHERE id=ev))<>9 OR
  jsonb_array_length((SELECT definition->'questions' FROM plan_events WHERE plan_id=plan AND code='final12'))<>12 THEN RAISE EXCEPTION 'Lost question definitions'; END IF;
 IF (SELECT body FROM plan_events WHERE id=ev) NOT LIKE '%GrPP%' OR (SELECT definition ? 'variants' FROM plan_events WHERE id=ev) THEN RAISE EXCEPTION 'Audience snapshot unresolved'; END IF;
 IF workday_date(DATE '2026-10-23',3,'{"weekdays":[1,2,3,4,5],"holidays":["2026-10-26"],"extra_workdays":["2026-10-24"]}')<>DATE '2026-10-27' THEN RAISE EXCEPTION 'Workday calendar error'; END IF;
 IF scheduled_event_time(DATE '2026-01-01',DATE '2026-03-31','Europe/Moscow','{"weekdays":[1,2,3,4,5],"holidays":[],"extra_workdays":[]}',
  'probation_end_date',NULL,0,0,TIME '09:00') IS DISTINCT FROM TIMESTAMPTZ '2026-03-31 06:00+00' THEN RAISE EXCEPTION 'Probation anchor wrong'; END IF;
 PERFORM test_reject(format('UPDATE plan_events SET definition=''{}'' WHERE id=%s',ev),ARRAY['P0001']);
 PERFORM test_reject(format('UPDATE employees SET location_tag=''UNKNOWN'' WHERE id=%s',emp),ARRAY['23514']);
 PERFORM test_reject(format('UPDATE employee_plans SET work_calendar=''{}'' WHERE id=%s',plan),ARRAY['P0001']);
 PERFORM reschedule_plan(hr,plan,CURRENT_DATE-2,'Europe/Moscow');
 PERFORM prepare_due_events(100);
 IF (SELECT count(*) FROM plan_events WHERE employee_id=emp AND kind<>'info' AND status IN ('queued','awaiting_answer','awaiting_comment'))<>1 THEN RAISE EXCEPTION 'Multiple interactive forms'; END IF;
 IF (SELECT status FROM plan_events WHERE plan_id=plan AND code='site_material')<>'queued' THEN RAISE EXCEPTION 'Info blocked by survey'; END IF;
 -- Drain one-per-employee leases. No HTTP call, all acknowledgements are simulated.
 LOOP
  round_count:=0;
  FOR item IN SELECT * FROM claim_outbox(100) LOOP
   round_count:=round_count+1;PERFORM finish_send(item.message_id,item.generation,'survey-test-'||item.message_id);
  END LOOP;
  EXIT WHEN round_count=0;
 END LOOP;
 SELECT * INTO STRICT e FROM plan_events WHERE id=ev;
 IF e.status<>'awaiting_answer' OR e.current_question_code<>'load' OR
  e.deadline_at NOT BETWEEN clock_timestamp()+interval '71 hours 59 minutes' AND clock_timestamp()+interval '72 hours 1 minute' THEN
  RAISE EXCEPTION 'Answer window did not start at API acceptance'; END IF;
 PERFORM test_reject(format('SELECT answer_survey_question(%s,%s,''load'',''rating'',''7'',%s)',other_emp,ev,e.interaction_revision),ARRAY['P0002']);
 PERFORM test_reject(format('SELECT answer_survey_question(%s,%s,''load'',''rating'',''11'',%s)',emp,ev,e.interaction_revision),ARRAY['23514']);
 PERFORM test_reject(format('UPDATE plan_events SET status=''completed'',completed_at=clock_timestamp(),current_question_code=NULL WHERE id=%s',ev),ARRAY['P0001']);
 rev:=e.interaction_revision;
 PERFORM answer_survey_question(emp,ev,'load','rating','7',rev);
 IF NOT EXISTS(SELECT 1 FROM event_answers WHERE event_id=ev AND question_code='load' AND rating=7 AND state='awaiting_comment') THEN RAISE EXCEPTION 'Low score was not preserved'; END IF;
 IF NOT EXISTS(SELECT 1 FROM message_analysis WHERE source_key='answer:'||ev||':load' AND status='pending') THEN RAISE EXCEPTION 'Signal waits for comment'; END IF;
 PERFORM test_reject(format('SELECT answer_survey_question(%s,%s,''load'',''rating'',''9'',%s)',emp,ev,rev),ARRAY['P0001']);
 -- A separate question neither consumes nor cancels the pending comment.
 INSERT INTO messages(employee_id,direction,author,body,rag_status) VALUES(emp,'in','employee','Как оформить отпуск?','pending') RETURNING id INTO m;
 IF (SELECT current_question_code FROM plan_events WHERE id=ev)<>'load' THEN RAISE EXCEPTION 'RAG changed survey'; END IF;
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'load','comment','"Нужны инструкции."',rev);
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'tasks_clear','boolean','false',rev);
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'tasks_clear','comment','"Не хватает постановки задачи."',rev);
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM test_reject(format('SELECT answer_survey_question(%s,%s,''help'',''single_choice'',''"bad"'',%s)',emp,ev,rev),ARRAY['P0001']);
 PERFORM answer_survey_question(emp,ev,'help','single_choice','"other"',rev);
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'help','comment','"Свой вариант: технический наставник."',rev);
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM toggle_survey_option(emp,ev,'contacts','mentor',rev);
 IF EXISTS(SELECT 1 FROM message_analysis WHERE source_key='answer:'||ev||':contacts') THEN RAISE EXCEPTION 'Draft analyzed'; END IF;
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM test_reject(format('SELECT toggle_survey_option(%s,%s,''contacts'',''none'',%s)',emp,ev,rev),ARRAY['P0001']);
 PERFORM toggle_survey_option(emp,ev,'contacts','hr',rev);
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'contacts','multi_choice','["hr","mentor"]',rev);
 IF (SELECT value FROM event_answers WHERE event_id=ev AND question_code='contacts')<>'["mentor","hr"]' THEN RAISE EXCEPTION 'Multiselect order not canonical'; END IF;
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'feedback','text','"Помогли коллеги."',rev);
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'photo','attachment','{"asset_ref":"demo:photo-employee-1","kind":"photo"}',rev);
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'expectations','rating','9',rev);
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'stay','single_choice','"leave"',rev);
 IF NOT EXISTS(SELECT 1 FROM message_analysis WHERE source_key='answer:'||ev||':stay' AND request->>'text'='Нет, задумываюсь об уходе') THEN RAISE EXCEPTION 'Exact selected label lost or analysis delayed'; END IF;
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'stay','comment','"Хочу обсудить условия с HR."',rev);
 SELECT interaction_revision INTO rev FROM plan_events WHERE id=ev;
 PERFORM answer_survey_question(emp,ev,'improve','text','"Больше информации в первый день."',rev);
 IF (SELECT status FROM plan_events WHERE id=ev)<>'completed' OR (SELECT count(*) FROM event_answers WHERE event_id=ev AND state='accepted')<>9 THEN RAISE EXCEPTION 'Survey not completed'; END IF;
 PERFORM test_reject(format('UPDATE event_answers SET rating=8 WHERE event_id=%s AND question_code=''load''',ev),ARRAY['P0001']);
 IF NOT EXISTS(SELECT 1 FROM messages m JOIN outbox o ON o.message_id=m.id WHERE m.event_id=ev AND m.payload->>'role'='completion' AND o.status='pending') THEN RAISE EXCEPTION 'Completion cancelled'; END IF;
 -- Old prompt must never be claimed; completion remains sendable.
 LOOP
  round_count:=0;
  FOR item IN SELECT * FROM claim_outbox(100) LOOP
   round_count:=round_count+1;
   IF item.plan_event_id=ev AND item.payload->>'role'='prompt' THEN RAISE EXCEPTION 'Stale prompt claimed'; END IF;
   PERFORM finish_send(item.message_id,item.generation,'survey-completed-'||item.message_id);
  END LOOP;
  EXIT WHEN round_count=0;
 END LOOP;
 -- Durable analysis: expired generation rejected, old input revision superseded.
 SELECT * INTO STRICT job FROM claim_message_analysis('hr-contract-v1','policy-contract-v1');
 UPDATE message_analysis SET lease_until=clock_timestamp()-interval '1 second' WHERE id=job.id;
 PERFORM recover_analysis_leases();
 PERFORM test_reject(format('SELECT apply_hr_analysis(%s,%s,''{"status":"ok","requires_hr":false,"priority":"medium","full_result":{}}'')',job.id,job.generation),ARRAY['P0001']);
 UPDATE message_analysis SET next_attempt_at=clock_timestamp()-interval '1 year' WHERE id=job.id;
 SELECT * INTO STRICT job FROM claim_message_analysis('hr-contract-v1','policy-contract-v1');
 result:='{"status":"ok","requires_hr":false,"priority":"medium","ticket":null,"full_result":{"quotes":[]}}';
 PERFORM apply_hr_analysis(job.id,job.generation,result);
 IF (SELECT status FROM message_analysis WHERE id=job.id)<>'superseded' THEN RAISE EXCEPTION 'Old answer revision escalated'; END IF;

 -- A current low rating with a medium/non-escalating policy result creates no case.
 SELECT id INTO m FROM message_analysis WHERE source_key='answer:'||ev||':load' AND input_revision=1;
 UPDATE message_analysis SET next_attempt_at=clock_timestamp()+interval '1 hour'
  WHERE status='pending' AND model_version='hr-contract-v1' AND id<>m;
 SELECT * INTO STRICT job FROM claim_message_analysis('hr-contract-v1','policy-contract-v1');
 IF job.id<>m THEN RAISE EXCEPTION 'Wrong current answer job'; END IF;
 cid:=apply_hr_analysis(job.id,job.generation,'{"status":"ok","requires_hr":false,"priority":"medium","full_result":{"escalate_medium":false}}');
 IF cid IS NOT NULL OR EXISTS(SELECT 1 FROM hr_cases WHERE source_key='answer:'||ev||':load') THEN RAISE EXCEPTION 'Low rating auto-escalated without policy'; END IF;
 -- no_data + TicketDraft is still a real request for human involvement.
 INSERT INTO messages(employee_id,direction,author,body,payload) VALUES(emp,'in','employee','','{"kind":"action"}') RETURNING id INTO m;
 PERFORM enqueue_analysis(emp,m,'message:'||m,0,jsonb_build_object('source','hr_request','employee_id',emp::text,'text',''),NULL,NULL,false,'ticket-test','policy-test');
 SELECT * INTO STRICT job FROM claim_message_analysis('ticket-test','policy-test');
 cid:=apply_hr_analysis(job.id,job.generation,'{"status":"no_data","requires_hr":false,"ticket":{},"category":"explicit_hr","priority":"high","full_result":{"status":"no_data","ticket":{}}}');
 IF cid IS NULL OR (SELECT hr_id FROM hr_cases WHERE id=cid)<>hr THEN RAISE EXCEPTION 'Ticket lost on no_data'; END IF;
 -- Clarification keeps original inbox/result intact and resolves once.
 INSERT INTO max_inbox(event_id,employee_id,payload,status,result) VALUES('v2-clarification',emp,'{}','done','{"route":"clarification"}');
 INSERT INTO messages(employee_id,direction,author,body,inbox_event_id) VALUES(emp,'in','employee','Нет','v2-clarification') RETURNING id INTO m;
 SELECT context_revision INTO context_rev FROM employees WHERE id=emp;
 pid:=create_pending_interaction(emp,m,context_rev,'[{"code":"knowledge","label":"Задать вопрос","route":"rag_question"},{"code":"hr","label":"Передать HR","route":"hr_request"}]',clock_timestamp()+interval '1 hour');
 PERFORM resolve_pending_interaction(emp,pid,'knowledge',context_rev);
 PERFORM test_reject(format('SELECT resolve_pending_interaction(%s,%s,''knowledge'',%s)',emp,pid,context_rev),ARRAY['P0001']);
 IF (SELECT i.result FROM max_inbox i WHERE i.event_id='v2-clarification')<>'{"route":"clarification"}' THEN RAISE EXCEPTION 'Original inbox result rewritten'; END IF;
 -- Mixed feedback blocks automated RAG until the required analysis decision.
 INSERT INTO messages(employee_id,direction,author,body,rag_status)
 VALUES(emp,'in','employee','Мне не выдали доступ. Что делать?','pending') RETURNING id INTO m;
 PERFORM enqueue_analysis(emp,m,'message:'||m,0,jsonb_build_object('source','message','employee_id',emp::text,'text','Мне не выдали доступ. Что делать?'),NULL,NULL,true,'mixed-test','policy-test');
 PERFORM test_reject(format('SELECT queue_rag_answer(%s,''premature'',ARRAY[]::bigint[],''model'',false)',m),ARRAY['P0001']);
 SELECT * INTO STRICT job FROM claim_message_analysis('mixed-test','policy-test');
 PERFORM apply_hr_analysis(job.id,job.generation,'{"status":"ok","requires_hr":false,"priority":"low","full_result":{}}');
 IF (SELECT analysis_gate FROM messages WHERE id=m)<>'clear' THEN RAISE EXCEPTION 'Analysis did not release mixed question'; END IF;
 -- Receipt-independent closure records the real basis without fabricating delivery.
 SELECT c.id INTO STRICT cid FROM hr_cases c JOIN messages msg ON msg.case_id=c.id AND msg.author='hr'
  WHERE c.employee_id=emp AND c.status='sent' LIMIT 1;
 PERFORM test_reject(format('SELECT close_hr_case(%s,%s,''api_accepted'','''')',hr,cid),ARRAY['P0001']);
 PERFORM close_hr_case(hr,cid,'api_accepted','HR подтвердил завершение обработки без receipt провайдера.');
 IF NOT EXISTS(SELECT 1 FROM hr_cases WHERE id=cid AND status='closed' AND closure_basis='api_accepted') OR
  EXISTS(SELECT 1 FROM outbox o JOIN messages msg ON msg.id=o.message_id WHERE msg.case_id=cid AND o.delivered_at IS NOT NULL) THEN
  RAISE EXCEPTION 'Closure fabricated delivery'; END IF;
 -- Historical expired queued form: test fixture, not a production UPDATE bypass.
 INSERT INTO plan_events(plan_id,employee_id,version_id,template_event_id,code,position,kind,body,definition,
  offset_months,offset_days,local_time,response_hours,required,reminder_hours,max_reminders,schedule_anchor,
  workday_no,send_window_hours,scheduled_at,send_until_at,status,current_question_code)
 SELECT plan_id,employee_id,version_id,template_event_id,'expired_fixture',1,kind,body,definition,
  offset_months,offset_days,local_time,response_hours,false,reminder_hours,max_reminders,schedule_anchor,
  workday_no,1,clock_timestamp()-interval '2 hours',clock_timestamp()-interval '1 hour','queued','load'
 FROM plan_events WHERE id=ev;
 PERFORM reschedule_plan(hr,plan,CURRENT_DATE-2,'Europe/Moscow',CURRENT_DATE);
 SELECT id INTO ev FROM plan_events WHERE plan_id=plan AND code='final12';
 PERFORM configure_event(hr,ev,72,2,'00:00');
 PERFORM prepare_due_events(100);
 IF NOT EXISTS(SELECT 1 FROM plan_events WHERE plan_id=plan AND code='expired_fixture' AND status='overdue') OR
  (SELECT status FROM plan_events WHERE id=ev)<>'queued' THEN RAISE EXCEPTION 'Expired form did not release dialog'; END IF;

 RAISE NOTICE 'V2 survey/router/calendar/analysis checks passed';
END $$;
ROLLBACK;
