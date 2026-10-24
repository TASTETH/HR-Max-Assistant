\set ON_ERROR_STOP on
-- Run after seed_demo.sql + seed_survey_demo.sql. Simulated API, no MAX calls.
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
SET LOCAL korsik.analysis_model_version='demo-no-model';
SET LOCAL korsik.analysis_policy_version='demo-no-policy';
DO $$
DECLARE emp bigint;ev plan_events;q jsonb;answer_type text;v jsonb;item record;n integer;i integer;
BEGIN
 SELECT id INTO STRICT emp FROM employees WHERE external_employee_id='DEMO-001';
 PERFORM prepare_due_events(100);
 FOR i IN 1..100 LOOP
  LOOP
   n:=0;
   FOR item IN SELECT * FROM claim_outbox(100) LOOP
    n:=n+1;PERFORM finish_send(item.message_id,item.generation,'walkthrough-survey-'||item.message_id);
   END LOOP;
   EXIT WHEN n=0;
  END LOOP;
  SELECT s.* INTO STRICT ev FROM plan_events s JOIN employee_plans p ON p.id=s.plan_id
   WHERE s.employee_id=emp AND p.status='active' AND s.code='survey9';
  EXIT WHEN ev.status='completed';
  IF ev.status NOT IN ('awaiting_answer','awaiting_comment') THEN RAISE EXCEPTION 'Survey not due/available'; END IF;
  q:=survey_question(ev.definition,ev.current_question_code);answer_type:=q->>'type';
  IF ev.status='awaiting_comment' THEN answer_type:='comment';v:='"Демонстрационный комментарий к ответу."';
  ELSIF answer_type='rating' THEN v:=CASE WHEN ev.current_question_code='load' THEN '7'::jsonb ELSE '9'::jsonb END;
  ELSIF answer_type='boolean' THEN v:='false';
  ELSIF answer_type='single_choice' THEN v:=CASE WHEN ev.current_question_code='help' THEN '"other"'::jsonb ELSE '"leave"'::jsonb END;
  ELSIF answer_type='multi_choice' THEN v:='["mentor","hr"]';
  ELSIF answer_type='attachment' THEN v:='{"asset_ref":"demo:photo-not-an-actual-upload","kind":"photo"}';
  ELSE v:='"Демонстрационная текстовая обратная связь."'; END IF;
  PERFORM answer_survey_question(emp,ev.id,ev.current_question_code,answer_type,v,ev.interaction_revision);
 END LOOP;
 IF ev.status<>'completed' THEN RAISE EXCEPTION 'Demo iteration limit exceeded'; END IF;
END $$;
SELECT a.event_id,a.question_code,a.answer_type,a.state,a.rating,a.value,a.comment
FROM event_answers a JOIN plan_events e ON e.id=a.event_id ORDER BY e.id,a.question_code;
SELECT id,source_key,input_revision,model_version,policy_version,status FROM message_analysis ORDER BY id;
COMMIT;
