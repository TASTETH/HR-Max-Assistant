\set ON_ERROR_STOP on
SET search_path=korsik,pg_catalog;
-- Replace demonstration values using bound backend parameters in real code.
\set hr_id 1
\set employee_id 1
\set answer_id 1
SELECT id,full_name,lifecycle FROM employees WHERE hr_id=:hr_id AND id>0 ORDER BY id LIMIT 50;
SELECT id,full_name FROM employees WHERE hr_id=:hr_id AND lifecycle='hr_review' ORDER BY id LIMIT 50;
SELECT id FROM employees WHERE max_account_id='demo-max-account-1';
SELECT id FROM employees WHERE max_chat_id='demo-max-chat-1';
SELECT employee_id,expires_at FROM invitations WHERE token_hash=sha256(convert_to('DEMO-NOT-A-SECRET-1','UTF8'));
SELECT * FROM employee_plans WHERE employee_id=:employee_id AND status='active';
SELECT id,employee_id,scheduled_at FROM plan_events
 WHERE status='scheduled' AND scheduled_at<=now() ORDER BY scheduled_at,id LIMIT 100;
SELECT id,employee_id,deadline_at FROM plan_events
 WHERE status IN ('awaiting_answer','awaiting_comment') AND deadline_at<=now() ORDER BY deadline_at,id;
SELECT message_id FROM outbox WHERE status='pending' AND next_attempt_at<=now() ORDER BY next_attempt_at,message_id LIMIT 50;
-- First page. The caller records the last row's created_at AND id.
SELECT id,created_at,author,body FROM messages WHERE employee_id=:employee_id
 ORDER BY created_at DESC,id DESC LIMIT 50;
-- Next page: substitute exact cursor values from the last row; no large OFFSET.
SELECT id,created_at,author,body FROM messages WHERE employee_id=:employee_id
 AND (created_at,id)<(TIMESTAMPTZ '2026-10-24 12:00:00+00',9223372036854775807::bigint)
 ORDER BY created_at DESC,id DESC LIMIT 50;
SELECT * FROM hr_cases WHERE hr_id=:hr_id AND status<>'closed' ORDER BY created_at,id LIMIT 50;
SELECT employee_id,rating,comment,answered_at FROM plan_events WHERE rating<8
 AND answered_at>=date_trunc('month',now()) AND answered_at<date_trunc('month',now())+interval '1 month'
 ORDER BY answered_at,id;
SELECT * FROM hr_dashboard WHERE hr_id=:hr_id ORDER BY employee_id;
-- Historical provenance deliberately does not require is_current=true.
SELECT s.rank,d.title,v.version_no,v.object_uri,c.locator,c.content
 FROM answer_sources s JOIN document_chunks c ON c.id=s.chunk_id
 JOIN document_versions v ON v.id=c.version_id JOIN documents d ON d.id=v.document_id
 JOIN messages m ON m.id=s.message_id JOIN employees e ON e.id=m.employee_id
 WHERE s.message_id=:answer_id AND e.hr_id=:hr_id AND d.enabled
 AND (d.scope_org_unit_id IS NULL OR d.scope_org_unit_id=e.org_unit_id)
 AND (d.scope_location_tag IS NULL OR d.scope_location_tag=e.location_tag) ORDER BY s.rank;
-- Sources suitable for NEW employee answers; revalidate before queue/send too.
SELECT c.id,c.qdrant_point_id,c.locator FROM document_chunks c WHERE source_is_accessible(:employee_id,c.id);
-- Observed plan is workload-dependent: small demo tables commonly use Seq Scan.
EXPLAIN (ANALYZE,BUFFERS) SELECT id FROM messages WHERE employee_id=:employee_id
 AND created_at>=TIMESTAMPTZ '2026-10-01 00:00:00+00' AND created_at<TIMESTAMPTZ '2026-11-01 00:00:00+00'
 ORDER BY created_at DESC,id DESC LIMIT 50;
-- Queue and error monitors; return no credentials or personal message content.
SELECT status,count(*),min(next_attempt_at) oldest_retry FROM outbox GROUP BY status;
SELECT status,count(*),min(received_at) oldest_received FROM max_inbox GROUP BY status;
SELECT lifecycle,count(*) FROM employees GROUP BY lifecycle;
SELECT rag_status,count(*) FROM messages WHERE direction='in' GROUP BY rag_status;

-- V2 delivery and survey diagnostics.
SELECT id,employee_id,status,scheduled_at,send_until_at,deadline_at,current_question_code,interaction_revision
 FROM plan_events WHERE status IN ('queued','awaiting_answer','awaiting_comment') ORDER BY scheduled_at,id;
SELECT event_id,question_code,rating,comment FROM event_answers WHERE rating<8 ORDER BY answered_at,event_id,question_code;
SELECT id,source_key,input_revision,model_version,policy_version,status,category,last_error FROM message_analysis ORDER BY id;
SELECT status,count(*) FROM pending_interactions GROUP BY status;
