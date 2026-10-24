\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
SET LOCAL TIME ZONE 'UTC';
-- Pure simulation of API success/receipt. No MAX or LLM calls are made.
SELECT e.external_employee_id,e.full_name,p.id plan_id,s.code,s.status,s.rating,s.comment
 FROM employees e JOIN employee_plans p ON p.employee_id=e.id JOIN plan_events s ON s.plan_id=p.id
 ORDER BY e.id,s.position;
SELECT c.id,c.status,m.author,m.body,o.status delivery_status
 FROM hr_cases c JOIN messages m ON m.case_id=c.id AND m.author='hr'
 JOIN outbox o ON o.message_id=m.id;
DO $$
DECLARE item record; batch_count integer; hr_message record;
BEGIN
 LOOP
 batch_count:=0;
 FOR item IN SELECT * FROM claim_outbox(100) LOOP
  batch_count:=batch_count+1;
  PERFORM finish_send(item.message_id,item.generation,'walkthrough-max-'||item.message_id);
 END LOOP;
 EXIT WHEN batch_count=0;
 END LOOP;
 FOR hr_message IN SELECT o.external_message_id FROM outbox o JOIN messages m ON m.id=o.message_id
  JOIN employees e ON e.id=m.employee_id WHERE m.author='hr' AND e.external_employee_id='DEMO-001'
 LOOP
  PERFORM confirm_delivery(hr_message.external_message_id,clock_timestamp());
 END LOOP;
 -- Scoped explicit HR close only after verified delivery.
 PERFORM 1 FROM employees WHERE external_employee_id='DEMO-001' FOR UPDATE;
 FOR hr_message IN SELECT c.id,c.hr_id FROM hr_cases c JOIN employees e ON e.id=c.employee_id JOIN hr_users h ON h.id=c.hr_id
  WHERE e.external_employee_id='DEMO-001' AND e.hr_id=c.hr_id AND h.enabled AND c.status='delivered' LOOP
  PERFORM close_hr_case(hr_message.hr_id,hr_message.id,'verified_delivery');
 END LOOP;
END $$;
SELECT c.id,c.status,m.author,o.status delivery_status,o.accepted_at,o.delivered_at
 FROM hr_cases c JOIN messages m ON m.case_id=c.id AND m.author='hr'
 JOIN outbox o ON o.message_id=m.id;
SELECT m.id answer_id,d.title,v.version_no,c.locator
 FROM messages m JOIN answer_sources s ON s.message_id=m.id
 JOIN document_chunks c ON c.id=s.chunk_id JOIN document_versions v ON v.id=c.version_id
 JOIN documents d ON d.id=v.document_id ORDER BY m.id,s.rank;
COMMIT;
