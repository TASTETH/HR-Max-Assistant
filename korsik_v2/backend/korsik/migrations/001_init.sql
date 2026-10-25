-- Корсик v2: initial schema. One HR, managers, employees, scenario events, surveys,
-- dialog/cases, outbox delivery, corpus versions, analytics.

CREATE TABLE users (
    id              bigserial PRIMARY KEY,
    login           text NOT NULL UNIQUE,
    password_hash   text NOT NULL,
    role            text NOT NULL CHECK (role IN ('hr','manager')),
    is_hr_head      boolean NOT NULL DEFAULT false,
    display_name    text NOT NULL,
    max_user_id     text UNIQUE,
    max_chat_id     text,
    max_linked_at   timestamptz,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    token_hash  text PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf        text NOT NULL,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE employees (
    id                  bigserial PRIMARY KEY,
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','active','paused','completed','deactivated')),
    full_name           text NOT NULL DEFAULT '',
    short_name          text NOT NULL DEFAULT '',      -- how Корсик addresses the person ("Х")
    phone               text NOT NULL DEFAULT '',
    email               text NOT NULL DEFAULT '',
    organization        text NOT NULL DEFAULT '',
    department          text NOT NULL DEFAULT '',
    position            text NOT NULL DEFAULT '',
    work_format         text NOT NULL DEFAULT 'office' CHECK (work_format IN ('office','production')),
    tags                text[] NOT NULL DEFAULT '{}',  -- GrPP | GTP | Luzhniki
    start_date          date,
    timezone            text NOT NULL DEFAULT 'Europe/Moscow',
    probation_end       date,
    address_form        text CHECK (address_form IN ('ty','vy')),
    it_access_confirmed boolean NOT NULL DEFAULT false,
    manager_user_id     bigint REFERENCES users(id),
    hr_user_id          bigint NOT NULL REFERENCES users(id),
    max_user_id         text UNIQUE,
    max_chat_id         text,
    linked_at           timestamptz,
    scenario_code       text,
    scenario_started_at timestamptz,
    preonboarding_state text NOT NULL DEFAULT 'none',  -- none|running|done
    dialog_mode         text NOT NULL DEFAULT 'auto' CHECK (dialog_mode IN ('auto','waiting_hr','hr_active')),
    dialog_revision     bigint NOT NULL DEFAULT 0,
    active_case_id      bigint,
    pending_input       jsonb,                           -- what the next free text means (survey text, clarification...)
    intro_text          text,
    intro_photo_ref     text,
    intro_approved_at   timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON employees(manager_user_id);

CREATE TABLE employee_documents (
    id            bigserial PRIMARY KEY,
    employee_id   bigint NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    filename      text NOT NULL,
    stored_name   text NOT NULL,
    mime          text NOT NULL,
    size_bytes    bigint NOT NULL,
    sha256        text NOT NULL,
    status        text NOT NULL DEFAULT 'uploaded' CHECK (status IN ('uploaded','processing','done','error')),
    error         text,
    pages         jsonb,
    uploaded_by   bigint NOT NULL REFERENCES users(id),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE field_suggestions (
    id            bigserial PRIMARY KEY,
    employee_id   bigint NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    document_id   bigint NOT NULL REFERENCES employee_documents(id) ON DELETE CASCADE,
    field         text NOT NULL,
    value         text NOT NULL,
    page          int,
    quote         text NOT NULL,
    method        text NOT NULL,                  -- regex | llm
    status        text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','accepted','rejected')),
    decided_by    bigint REFERENCES users(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, field, value)
);

CREATE TABLE invites (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('employee','manager')),
    employee_id   bigint REFERENCES employees(id) ON DELETE CASCADE,
    user_id       bigint REFERENCES users(id) ON DELETE CASCADE,
    token_hash    text NOT NULL UNIQUE,
    created_by    bigint NOT NULL REFERENCES users(id),
    expires_at    timestamptz NOT NULL,
    revoked_at    timestamptz,
    used_at       timestamptz,
    used_by_max   text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE materials (
    id            bigserial PRIMARY KEY,
    key           text NOT NULL,                  -- scenario slot, e.g. food_pdf, territory_map, adaptation_plan
    title         text NOT NULL,
    tags          text[] NOT NULL DEFAULT '{}',   -- empty = all tags
    employee_id   bigint REFERENCES employees(id) ON DELETE CASCADE,  -- personal material (adaptation plan)
    kind          text NOT NULL CHECK (kind IN ('file','image','link','text')),
    filename      text,
    stored_name   text,
    url           text,
    body          text,
    max_token     text,                           -- cached MAX upload token
    active        boolean NOT NULL DEFAULT true,
    uploaded_by   bigint REFERENCES users(id),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON materials(key);

CREATE TABLE scenario_versions (
    code          text PRIMARY KEY,
    source_sha256 text NOT NULL,
    definition    jsonb NOT NULL,
    loaded_at     timestamptz NOT NULL DEFAULT now()
);

-- One row per business event of one employee; unique key prevents duplicates.
CREATE TABLE events (
    id              bigserial PRIMARY KEY,
    employee_id     bigint NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    step_key        text NOT NULL,
    scenario_code   text NOT NULL,
    title           text NOT NULL,
    source_rows     text NOT NULL,
    due_at          timestamptz NOT NULL,
    status          text NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled','sending','sent','waiting_reply','done','skipped','cancelled','failed')),
    needs_permission boolean NOT NULL DEFAULT false,
    permitted_at    timestamptz,
    permitted_by    bigint REFERENCES users(id),
    disabled        boolean NOT NULL DEFAULT false,
    skip_reason     text,
    attempts        int NOT NULL DEFAULT 0,
    last_error      text,
    sent_at         timestamptz,
    is_test         boolean NOT NULL DEFAULT false,
    state           jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (employee_id, step_key)
);
CREATE INDEX ON events(status, due_at);

CREATE TABLE survey_runs (
    id              bigserial PRIMARY KEY,
    employee_id     bigint NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    event_id        bigint REFERENCES events(id) ON DELETE SET NULL,
    survey_key      text NOT NULL,
    status          text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','active','completed','expired','cancelled')),
    current_q       text,
    draft           jsonb NOT NULL DEFAULT '{}',   -- multi-select drafts {question_id: [option ids]}
    nonce           int NOT NULL DEFAULT 0,        -- bumps on every question, makes stale buttons detectable
    started_at      timestamptz,                   -- when MAX accepted the opening message
    reminders_sent  int NOT NULL DEFAULT 0,
    next_reminder_at timestamptz,
    completed_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (employee_id, survey_key)
);

CREATE TABLE survey_answers (
    id              bigserial PRIMARY KEY,
    run_id          bigint NOT NULL REFERENCES survey_runs(id) ON DELETE CASCADE,
    employee_id     bigint NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    question_id     text NOT NULL,
    question_text   text NOT NULL,
    qtype           text NOT NULL,
    option_ids      text[] NOT NULL DEFAULT '{}',
    option_labels   text[] NOT NULL DEFAULT '{}',
    score           int,
    text            text,
    comment         text,
    source_message_id bigint,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, question_id)
);

CREATE TABLE cases (
    id              bigserial PRIMARY KEY,
    employee_id     bigint NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    hr_user_id      bigint NOT NULL REFERENCES users(id),
    root_message_id bigint,
    reason          text NOT NULL CHECK (reason IN ('no_answer','personal_data','feedback_signal','human_request','service_error','material_missing')),
    priority        text NOT NULL CHECK (priority IN ('low','medium','high')),
    needs_review    boolean NOT NULL DEFAULT false,
    status          text NOT NULL DEFAULT 'new' CHECK (status IN ('new','in_progress','resolved')),
    topic           text NOT NULL DEFAULT 'other',
    question        text NOT NULL,
    context         jsonb NOT NULL DEFAULT '{}',
    dedupe_key      text UNIQUE,
    taken_at        timestamptz,
    resolved_at     timestamptz,
    resolved_by     bigint REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON cases(status);

CREATE TABLE messages (
    id              bigserial PRIMARY KEY,
    employee_id     bigint REFERENCES employees(id) ON DELETE CASCADE,
    user_id         bigint REFERENCES users(id) ON DELETE CASCADE,   -- service messages to HR / managers
    direction       text NOT NULL CHECK (direction IN ('in','out')),
    author_kind     text NOT NULL CHECK (author_kind IN ('employee','bot_script','bot_llm','bot_system','hr','manager')),
    author_user_id  bigint REFERENCES users(id),
    text            text NOT NULL DEFAULT '',
    payload         jsonb NOT NULL DEFAULT '{}',
    provider_message_id text,
    case_id         bigint REFERENCES cases(id) ON DELETE SET NULL,
    dialog_mode     text,
    dialog_revision bigint,
    request_id      text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON messages(employee_id, id);
CREATE INDEX ON messages(case_id);

-- Every webhook update is stored once; processing happens in the worker.
CREATE TABLE inbound_events (
    id              bigserial PRIMARY KEY,
    provider_event_id text NOT NULL UNIQUE,
    payload         jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'received' CHECK (status IN ('received','processed','ignored','failed')),
    error           text,
    request_id      text NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz
);
CREATE INDEX ON inbound_events(status, id);

-- Outgoing delivery queue. The sender re-checks dialog rights right before sending.
CREATE TABLE outbox (
    id              bigserial PRIMARY KEY,
    message_id      bigint REFERENCES messages(id) ON DELETE CASCADE,
    employee_id     bigint REFERENCES employees(id) ON DELETE CASCADE,
    user_id         bigint REFERENCES users(id) ON DELETE CASCADE,
    chat_id         text,
    body            jsonb NOT NULL,
    source          text NOT NULL CHECK (source IN ('scenario','llm','system','human','service')),
    requires_revision bigint,                       -- automatic messages die if dialog revision moved
    corpus_version_id bigint,
    cited_doc_ids   bigint[] NOT NULL DEFAULT '{}',
    survey_run_id   bigint,                          -- marks a survey opening message (reminder anchor)
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sending','sent','uncertain','failed','cancelled')),
    attempts        int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    external_id     text,
    last_error      text,
    sent_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON outbox(status, next_attempt_at);

CREATE TABLE notifications (
    id              bigserial PRIMARY KEY,
    recipient_user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind            text NOT NULL,           -- new_case | case_update | manager_meeting | digest
    employee_id     bigint REFERENCES employees(id) ON DELETE CASCADE,
    case_id         bigint REFERENCES cases(id) ON DELETE CASCADE,
    title           text NOT NULL,
    body            text NOT NULL,
    reason          text NOT NULL DEFAULT '',
    dedupe_key      text NOT NULL UNIQUE,
    channel_status  text NOT NULL DEFAULT 'pending'
                    CHECK (channel_status IN ('pending','queued','sent','failed','no_channel')),
    outbox_id       bigint REFERENCES outbox(id) ON DELETE SET NULL,
    read_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE analyses (
    id              bigserial PRIMARY KEY,
    employee_id     bigint NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    source_type     text NOT NULL CHECK (source_type IN ('message','survey_answer')),
    source_id       bigint NOT NULL,
    analyzer_version text NOT NULL,
    status          text NOT NULL CHECK (status IN ('ok','rule_only','failed')),
    sentiment       text NOT NULL CHECK (sentiment IN ('positive','neutral','negative','unknown')),
    topics          text[] NOT NULL DEFAULT '{}',
    signals         jsonb NOT NULL DEFAULT '[]',
    summary         text NOT NULL DEFAULT '',
    priority        text CHECK (priority IN ('low','medium','high')),
    requires_hr     boolean NOT NULL DEFAULT false,
    model           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_type, source_id, analyzer_version)
);

-- Background jobs (inbound processing, OCR, corpus builds, analysis...).
CREATE TABLE jobs (
    id              bigserial PRIMARY KEY,
    kind            text NOT NULL,
    payload         jsonb NOT NULL DEFAULT '{}',
    dedupe_key      text UNIQUE,
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')),
    run_at          timestamptz NOT NULL DEFAULT now(),
    attempts        int NOT NULL DEFAULT 0,
    locked_until    timestamptz,
    last_error      text,
    request_id      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX ON jobs(status, run_at);

-- Corporate knowledge base
CREATE TABLE corpus_documents (
    id              bigserial PRIMARY KEY,
    title           text NOT NULL,
    filename        text NOT NULL,
    stored_name     text NOT NULL,
    sha256          text NOT NULL,
    version_label   text NOT NULL DEFAULT '1',
    organizations   text[] NOT NULL DEFAULT '{}',  -- applicability; '*' = all group companies (explicit)
    tags            text[] NOT NULL DEFAULT '{}',  -- optional narrowing by scenario tag
    effective_from  date,
    replaces_id     bigint REFERENCES corpus_documents(id),
    status          text NOT NULL DEFAULT 'uploaded'
                    CHECK (status IN ('uploaded','processing','extracted','error','published','withdrawn','superseded')),
    error           text,
    pages           jsonb,
    uploaded_by     bigint REFERENCES users(id),
    published_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE corpus_versions (
    id              bigserial PRIMARY KEY,
    status          text NOT NULL DEFAULT 'building' CHECK (status IN ('building','ready','failed','active','archived')),
    document_ids    bigint[] NOT NULL,
    dense           boolean NOT NULL DEFAULT false,
    embed_model     text,
    error           text,
    reason          text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    activated_at    timestamptz
);
CREATE UNIQUE INDEX one_active_corpus ON corpus_versions((status)) WHERE status = 'active';

CREATE TABLE chunks (
    id              bigserial PRIMARY KEY,
    corpus_version_id bigint NOT NULL REFERENCES corpus_versions(id) ON DELETE CASCADE,
    document_id     bigint NOT NULL REFERENCES corpus_documents(id),
    ordinal         int NOT NULL,
    page            int NOT NULL,
    text            text NOT NULL,
    embedding       bytea
);
CREATE INDEX ON chunks(corpus_version_id);

CREATE TABLE digests (
    id              bigserial PRIMARY KEY,
    period_start    date NOT NULL,
    period_end      date NOT NULL,
    recipient_user_id bigint NOT NULL REFERENCES users(id),
    data            jsonb NOT NULL,
    text            text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (period_start, recipient_user_id)
);

CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    user_id     bigint REFERENCES users(id),
    action      text NOT NULL,
    object      text NOT NULL,
    details     jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);
