BEGIN;
SELECT pg_advisory_xact_lock(hashtext('korsik.transport.install'));
CREATE TABLE IF NOT EXISTS korsik_router.transport_outbox (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 deduplication_key text NOT NULL UNIQUE,
 kind text NOT NULL CHECK(kind IN ('message','callback')),
 chat_id text,
 callback_id text,
 body jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','accepted','uncertain','dead')),
 attempts integer NOT NULL DEFAULT 0,
 generation bigint NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 lease_until timestamptz,
 last_error text,
 external_message_id text,
 CHECK((status='sending')=(lease_until IS NOT NULL)),
 CHECK((kind='message' AND chat_id IS NOT NULL) OR (kind='callback' AND callback_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS transport_pending ON korsik_router.transport_outbox(next_attempt_at,id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS korsik_router.delivery_parts (
 message_id bigint NOT NULL REFERENCES korsik.messages(id),
 part integer NOT NULL CHECK(part>=0),
 body jsonb NOT NULL,
 external_message_id text UNIQUE,
 PRIMARY KEY(message_id,part)
);
CREATE TABLE IF NOT EXISTS korsik_router.rate_slots (
 key text PRIMARY KEY,
 next_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS korsik_router.hr_routes (
 hr_id bigint PRIMARY KEY REFERENCES korsik.hr_users(id),
 max_chat_id text NOT NULL
);
CREATE INDEX IF NOT EXISTS document_chunks_russian_unicode ON korsik.document_chunks
 USING gin(to_tsvector('russian'::regconfig,lower(content COLLATE "pg_c_utf8")));
INSERT INTO korsik.schema_migrations(version,description) VALUES(3,'MAX transport, delivery parts and document search') ON CONFLICT DO NOTHING;
GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA korsik_router TO korsik_app;
REVOKE UPDATE ON korsik_router.hr_results FROM korsik_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA korsik_router TO korsik_app;
COMMIT;
