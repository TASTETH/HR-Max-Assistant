\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
SET LOCAL TIME ZONE 'UTC';
DO $$
DECLARE h bigint; unit_id bigint; e1 bigint; e2 bigint; t bigint; v bigint;
 p1 bigint; p2 bigint; s bigint; root_id bigint; case_id bigint; hr_reply bigint;
 doc_id bigint; dv bigint; chunk_id bigint; q bigint; item record; batch_count integer;
BEGIN
 INSERT INTO hr_users(auth_subject,display_name) VALUES('demo-hr-subject','Марина Примерова') RETURNING id INTO h;
 INSERT INTO org_units(code,name) VALUES('DEMO','Демонстрационное подразделение') RETURNING id INTO unit_id;
 INSERT INTO employees(hr_id,external_employee_id) VALUES(h,'DEMO-001') RETURNING id INTO e1;
 INSERT INTO employees(hr_id,external_employee_id) VALUES(h,'DEMO-002') RETURNING id INTO e2;
 INSERT INTO invitations(employee_id,token_hash,expires_at) VALUES
 (e1,sha256(convert_to('DEMO-NOT-A-SECRET-1','UTF8')),clock_timestamp()+interval '2 days'),
 (e2,sha256(convert_to('DEMO-NOT-A-SECRET-2','UTF8')),clock_timestamp()+interval '2 days');
 PERFORM activate_invitation(sha256(convert_to('DEMO-NOT-A-SECRET-1','UTF8')),'demo-max-account-1','demo-max-chat-1');
 PERFORM activate_invitation(sha256(convert_to('DEMO-NOT-A-SECRET-2','UTF8')),'demo-max-account-2','demo-max-chat-2');
 PERFORM save_registration(e1,0,'full_name','Алексей Тестов');
 PERFORM save_registration(e1,1,'phone','+79990000001');
 PERFORM save_registration(e1,2,'confirm','yes');
 PERFORM save_registration(e2,0,'full_name','Ирина Примерова');
 PERFORM save_registration(e2,1,'phone','+79990000002');
 PERFORM save_registration(e2,2,'confirm','yes');
 PERFORM verify_registration(h,e1,unit_id,'Инженер',CURRENT_DATE,'Europe/Moscow');
 PERFORM verify_registration(h,e2,unit_id,'Инженер',CURRENT_DATE,'Asia/Yekaterinburg');
 INSERT INTO plan_templates(code,name,suggested_job_title) VALUES('DEMO','Базовая адаптация','Инженер') RETURNING id INTO t;
 INSERT INTO plan_versions(template_id,version_no,created_by) VALUES(t,1,h) RETURNING id INTO v;
 INSERT INTO template_events(version_id,code,position,kind,body,offset_days,required) VALUES
 (v,'welcome',1,'info','Добро пожаловать! Это демонстрация Корсика.',0,true),
 (v,'task',2,'task','Подтвердите знакомство с рабочим местом.',1,true),
 (v,'day3',3,'rating','Как проходят первые дни? Оцените от 1 до 10.',2,false),
 (v,'day14',4,'rating','Как проходит адаптация? Оцените от 1 до 10.',13,false);
 INSERT INTO template_events(version_id,code,position,kind,body,offset_months,required)
 VALUES(v,'month1',5,'feedback','Поделитесь впечатлениями за календарный месяц.',1,true);
 UPDATE template_events SET max_reminders=0 WHERE version_id=v AND kind='info';
 UPDATE plan_versions SET published_at=clock_timestamp() WHERE id=v;
 p1:=assign_plan(h,e1,v);p2:=assign_plan(h,e2,v);
 -- Compress demo timing only before queueing. Real timing comes from event_time().
 UPDATE plan_events SET scheduled_at=clock_timestamp()-interval '1 hour',
  deadline_at=clock_timestamp()+interval '3 days' WHERE plan_id IN (p1,p2) AND code IN ('welcome','day3');
 PERFORM prepare_due_events(100);
 LOOP
 batch_count:=0;
 FOR item IN SELECT * FROM claim_outbox(100) LOOP
  batch_count:=batch_count+1;
  PERFORM finish_send(item.message_id,item.generation,'demo-ext-'||item.message_id);
 END LOOP;
 EXIT WHEN batch_count=0;
 END LOOP;
 SELECT id INTO s FROM plan_events WHERE plan_id=p1 AND code='day3';
 PERFORM answer_event(e1,s,'rating','7');
 PERFORM answer_event(e1,s,'comment','Нужны более подробные инструкции по первому заданию.');
 SELECT id INTO s FROM plan_events WHERE plan_id=p2 AND code='day3';
 PERFORM answer_event(e2,s,'rating','9');

 INSERT INTO max_inbox(event_id,employee_id,payload,status,result)
 VALUES('demo-in-hr',e1,'{"type":"message"}','done','{"route":"hr"}');
 INSERT INTO messages(employee_id,direction,author,body,inbox_event_id)
 VALUES(e1,'in','employee','Хочу обсудить с HR условия наставничества.','demo-in-hr') RETURNING id INTO root_id;
 case_id:=open_hr_case(e1,root_id,'explicit_human','Сотрудник попросил связать с закреплённым HR.');
 hr_reply:=prepare_hr_reply(h,case_id,'Здравствуйте! Давайте обсудим наставничество завтра в 10:00.');
 -- HR reply remains pending: no real MAX integration and no network call.

 INSERT INTO documents(code,title,scope_org_unit_id)
 VALUES('DEMO-GUIDE','Вымышленная памятка для демонстрации',unit_id) RETURNING id INTO doc_id;
 INSERT INTO document_versions(document_id,version_no,object_uri,sha256,valid_from)
 VALUES(doc_id,1,'demo://documents/guide-v1.txt',sha256(convert_to('demo-guide-v1','UTF8')),
  clock_timestamp()-interval '1 day') RETURNING id INTO dv;
 INSERT INTO document_chunks(version_id,ordinal,locator,content,qdrant_point_id)
 VALUES(dv,1,'Раздел 1','В демонстрационном примере с вопросами о наставнике можно обратиться к закреплённому HR.',
 '00000000-0000-4000-8000-000000000001') RETURNING id INTO chunk_id;
 INSERT INTO document_chunks(version_id,ordinal,locator,content,qdrant_point_id)
 VALUES(dv,2,'Раздел 2','Все сведения этой памятки вымышлены.',
 '00000000-0000-4000-8000-000000000002');
 UPDATE document_versions SET is_current=true,index_status='ready',indexed_at=clock_timestamp() WHERE id=dv;
 INSERT INTO max_inbox(event_id,employee_id,payload,status,result)
 VALUES('demo-in-rag',e2,'{"type":"message"}','done','{"route":"rag"}');
 INSERT INTO messages(employee_id,direction,author,body,inbox_event_id,rag_status)
 VALUES(e2,'in','employee','К кому обратиться по вопросу наставника?','demo-in-rag','pending') RETURNING id INTO q;
 PERFORM queue_rag_answer(q,'Можно обратиться к закреплённому HR. Источник: демонстрационная памятка, раздел 1.',
 ARRAY[chunk_id],'demo-local-llm-1',true);
 INSERT INTO message_analysis(message_id,model_version,topic,sentiment,problem_level,confidence)
 VALUES(root_id,'demo-analysis-1','наставничество','neutral',1,0.82);
END $$;
COMMIT;
