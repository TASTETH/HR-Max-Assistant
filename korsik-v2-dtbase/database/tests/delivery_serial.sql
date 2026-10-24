\set ON_ERROR_STOP on
-- Dedicated test database only. This test intentionally COMMITs between claims.
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
SELECT queue_message(id,'bot','Delivery fixture 1','delivery-test:one','notice') FROM employees WHERE external_employee_id='DEMO-002';
SELECT queue_message(id,'bot','Delivery fixture 2','delivery-test:two','notice') FROM employees WHERE external_employee_id='DEMO-002';
CREATE TEMP TABLE delivery_claimed AS SELECT * FROM claim_outbox(100);
DO $$ BEGIN
 IF (SELECT count(*) FROM delivery_claimed)=0 THEN RAISE EXCEPTION 'No delivery fixture claimed'; END IF;
 IF EXISTS(SELECT employee_id FROM delivery_claimed GROUP BY employee_id HAVING count(*)>1) THEN RAISE EXCEPTION 'Batch claims two messages for employee'; END IF;
END $$;
COMMIT;
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
DO $$ DECLARE item record; BEGIN
 IF EXISTS(SELECT 1 FROM claim_outbox(100) x JOIN delivery_claimed c ON c.employee_id=x.employee_id) THEN
  RAISE EXCEPTION 'COMMIT released delivery serialization'; END IF;
 FOR item IN SELECT * FROM delivery_claimed LOOP
  PERFORM finish_send(item.message_id,item.generation,NULL,'TEST_ONLY_NO_NETWORK',true);
 END LOOP;
 IF EXISTS(SELECT 1 FROM claim_outbox(100) x JOIN delivery_claimed c ON c.employee_id=x.employee_id) THEN
  RAISE EXCEPTION 'Uncertain did not block later messages'; END IF;
END $$;
-- No network call occurred. Restore test leases with a new generation.
UPDATE outbox SET status='pending',generation=generation+1,lease_until=NULL,next_attempt_at=clock_timestamp()
 WHERE message_id IN (SELECT message_id FROM delivery_claimed);
DROP TABLE delivery_claimed;
CREATE TEMP TABLE global_delivery_claim AS SELECT * FROM claim_outbox(100,true);
DO $$ BEGIN IF (SELECT count(*) FROM global_delivery_claim)<>1 THEN RAISE EXCEPTION 'Global serial mode must claim exactly one'; END IF; END $$;
COMMIT;
BEGIN;
SET LOCAL search_path=korsik,pg_catalog;
DO $$ BEGIN IF EXISTS(SELECT 1 FROM claim_outbox(100,true)) THEN RAISE EXCEPTION 'Global barrier lost after COMMIT'; END IF; END $$;
UPDATE outbox SET status='pending',generation=generation+1,lease_until=NULL,next_attempt_at=clock_timestamp()
 WHERE message_id IN (SELECT message_id FROM global_delivery_claim);
DROP TABLE global_delivery_claim;
COMMIT;
