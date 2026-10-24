\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
CREATE FUNCTION korsik.test_reject(p_sql text,p_states text[]) RETURNS void LANGUAGE plpgsql AS $$
DECLARE caught boolean:=false;
BEGIN
 BEGIN EXECUTE p_sql;
 EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE=ANY(p_states) THEN caught:=true;
  ELSE RAISE EXCEPTION 'Unexpected SQLSTATE %: %',SQLSTATE,SQLERRM; END IF;
 END;
 IF NOT caught THEN RAISE EXCEPTION 'Expected rejection: %',p_sql; END IF;
END $$;
DO $$
DECLARE e bigint; e2 bigint; h bigint; other_hr bigint; p bigint; ev bigint; q bigint; noanswer bigint;
 source_id bigint; mid bigint; cid bigint; unit_id bigint; job messages; delivery record; batch_count integer;
BEGIN
 SELECT id,hr_id INTO STRICT e,h FROM employees WHERE external_employee_id='DEMO-001';
 SELECT id INTO STRICT e2 FROM employees WHERE external_employee_id='DEMO-002';
 SELECT id INTO STRICT p FROM employee_plans WHERE employee_id=e AND status='active';
 SELECT min(id) INTO source_id FROM document_chunks;
 PERFORM test_reject(format('UPDATE document_chunks SET content=''changed'' WHERE id=%s',source_id),ARRAY['P0001']);
 PERFORM test_reject(format('UPDATE document_versions SET object_uri=''changed'' WHERE id=(SELECT version_id FROM document_chunks WHERE id=%s)',source_id),ARRAY['P0001']);
 -- Rating 7 remains incomplete until a comment; unrelated question never consumes it.
 SELECT id INTO STRICT ev FROM plan_events WHERE plan_id=p AND code='day14';
 UPDATE plan_events SET scheduled_at=now()-interval '1 minute',deadline_at=now()+interval '3 days' WHERE id=ev;
 PERFORM prepare_due_events(100);
 LOOP
 batch_count:=0;
 FOR delivery IN SELECT * FROM claim_outbox(100) LOOP
  batch_count:=batch_count+1;
  PERFORM finish_send(delivery.message_id,delivery.generation,'flow-test-ext-'||delivery.message_id);
 END LOOP;
 EXIT WHEN batch_count=0;
 END LOOP;
 IF answer_event(e,ev,'rating','7')<>'awaiting_comment' THEN RAISE EXCEPTION 'Low rating lost comment context'; END IF;
 INSERT INTO messages(employee_id,direction,author,body,rag_status)
 VALUES(e,'in','employee','Как связаться с наставником?','pending') RETURNING id INTO q;
 mid:=queue_rag_answer(q,'Смотрите демонстрационную памятку.',ARRAY[source_id],'flow-model',true);
 IF (SELECT status FROM plan_events WHERE id=ev)<>'awaiting_comment' THEN RAISE EXCEPTION 'Question consumed survey'; END IF;
 PERFORM answer_event(e,ev,'comment','Не хватает информации.');
 IF (SELECT status FROM plan_events WHERE id=ev)<>'completed' THEN RAISE EXCEPTION 'Comment did not complete survey'; END IF;

 INSERT INTO messages(employee_id,direction,author,body,rag_status)
 VALUES(e,'in','employee','Неизвестный вопрос','pending') RETURNING id INTO noanswer;
 PERFORM queue_rag_answer(noanswer,'Не нашёл ответа в доступных документах.',ARRAY[]::bigint[],'flow-model',false);
 IF NOT EXISTS(SELECT 1 FROM hr_cases WHERE root_message_id=noanswer AND reason='no_answer') THEN RAISE EXCEPTION 'No-answer state missing'; END IF;

 INSERT INTO messages(employee_id,direction,author,body,rag_status)
 VALUES(e,'in','employee','Вопрос при недоступном поиске','pending') RETURNING id INTO q;
 SELECT * INTO STRICT job FROM claim_rag_question();
 IF job.id<>q THEN RAISE EXCEPTION 'Unexpected RAG claim'; END IF;
 PERFORM fail_rag_question(q,job.rag_generation,'SEARCH_UNAVAILABLE');
 IF (SELECT rag_status FROM messages WHERE id=q)<>'error' THEN RAISE EXCEPTION 'Error confused with no answer'; END IF;
 UPDATE messages SET rag_next_attempt_at=now()-interval '1 second' WHERE id=q;
 SELECT * INTO STRICT job FROM claim_rag_question();
 UPDATE messages SET rag_lease_until=now()-interval '1 second' WHERE id=q;
 PERFORM recover_rag_leases();
 IF (SELECT rag_status FROM messages WHERE id=q)<>'error' THEN RAISE EXCEPTION 'RAG lease stuck'; END IF;
 PERFORM test_reject(format('SELECT queue_rag_answer(%s,''stale'',ARRAY[%s]::bigint[],''model'',true,%s)',q,source_id,job.rag_generation-1),ARRAY['P0001']);

 -- Document access and currency are validated by the database, not just Qdrant.
 INSERT INTO org_units(code,name) VALUES('FLOW-OTHER','Другой отдел') RETURNING id INTO unit_id;
 UPDATE employees SET org_unit_id=unit_id WHERE id=e2;
 INSERT INTO messages(employee_id,direction,author,body,rag_status)
 VALUES(e2,'in','employee','Вопрос вне области доступа','pending') RETURNING id INTO q;
 PERFORM test_reject(format('SELECT queue_rag_answer(%s,''forbidden'',ARRAY[%s]::bigint[],''model'',true)',q,source_id),ARRAY['P0001']);
 UPDATE document_versions SET is_current=false WHERE id=(SELECT version_id FROM document_chunks WHERE id=source_id);
 INSERT INTO messages(employee_id,direction,author,body,rag_status)
 VALUES(e,'in','employee','Вопрос об устаревшем документе','pending') RETURNING id INTO q;
 PERFORM test_reject(format('SELECT queue_rag_answer(%s,''outdated'',ARRAY[%s]::bigint[],''model'',true)',q,source_id),ARRAY['P0001']);
 -- Existing historical citations remain intact; old queued answer is cancelled on claim.
 PERFORM claim_outbox(100);
 IF (SELECT status FROM outbox WHERE message_id=mid)<>'cancelled' THEN RAISE EXCEPTION 'Outdated queued RAG answer not cancelled'; END IF;

 -- Assigned HR and the author of the HR reply cannot be changed to another person.
 INSERT INTO hr_users(auth_subject,display_name) VALUES('flow-other-hr','Другой HR') RETURNING id INTO other_hr;
 cid:=open_hr_case(e,q,'manual','Проверка прав');
 PERFORM test_reject(format('SELECT prepare_hr_reply(%s,%s,''wrong HR'')',other_hr,cid),ARRAY['P0001']);
 PERFORM test_reject(format('INSERT INTO messages(employee_id,direction,author,author_hr_id,body,deduplication_key,case_id)
  VALUES(%s,''out'',''hr'',%s,''wrong HR'',''flow-wrong-hr'',%s)',e,other_hr,cid),ARRAY['23503']);
 PERFORM test_reject(format('SELECT queue_rag_answer(%s,''parallel'',ARRAY[]::bigint[],''model'',false)',q),ARRAY['P0001']);
 IF open_hr_case(e,q,'manual','same root')<>cid THEN RAISE EXCEPTION 'Duplicate HR case'; END IF;
 RAISE NOTICE 'All additional flow checks passed';
END $$;
ROLLBACK;
