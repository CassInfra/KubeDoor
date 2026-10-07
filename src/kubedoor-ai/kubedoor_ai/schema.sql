CREATE TABLE IF NOT EXISTS kubedoor_ai_connections (
    env text PRIMARY KEY, encrypted_config bytea NOT NULL, context text NOT NULL,
    revision integer NOT NULL DEFAULT 1, test_status text NOT NULL DEFAULT 'success',
    tested_at timestamptz NOT NULL DEFAULT now(), updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kubedoor_ai_sessions (
    id uuid PRIMARY KEY, username text NOT NULL, title text NOT NULL,
    idempotency_key text, created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(username, idempotency_key)
);
CREATE TABLE IF NOT EXISTS kubedoor_ai_runs (
    id uuid PRIMARY KEY, session_id uuid NOT NULL REFERENCES kubedoor_ai_sessions(id) ON DELETE CASCADE,
    username text NOT NULL, permission text NOT NULL, status text NOT NULL,
    scope jsonb NOT NULL, skill_ids jsonb NOT NULL DEFAULT '[]', origin text NOT NULL DEFAULT 'chat',
    idempotency_key text, cancel_requested boolean NOT NULL DEFAULT false,
    event_seq bigint NOT NULL DEFAULT 0, error text,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(session_id, idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS kubedoor_ai_one_active_run ON kubedoor_ai_runs(session_id)
    WHERE status IN ('running', 'waiting_approval');
CREATE TABLE IF NOT EXISTS kubedoor_ai_messages (
    id uuid PRIMARY KEY, session_id uuid NOT NULL REFERENCES kubedoor_ai_sessions(id) ON DELETE CASCADE,
    run_id uuid REFERENCES kubedoor_ai_runs(id) ON DELETE SET NULL,
    role text NOT NULL, content text NOT NULL, scope jsonb, tools jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kubedoor_ai_events (
    run_id uuid NOT NULL REFERENCES kubedoor_ai_runs(id) ON DELETE CASCADE,
    seq bigint NOT NULL, type text NOT NULL, data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(run_id, seq)
);
CREATE TABLE IF NOT EXISTS kubedoor_ai_actions (
    id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES kubedoor_ai_runs(id) ON DELETE CASCADE,
    call_id text NOT NULL UNIQUE, operation text NOT NULL, source text NOT NULL,
    arguments jsonb NOT NULL, preview jsonb NOT NULL, read_only boolean NOT NULL,
    connection_revision integer, state text NOT NULL, interrupt_id text,
    result jsonb, created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '30 minutes',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kubedoor_ai_sessions_owner ON kubedoor_ai_sessions(username, updated_at DESC);
CREATE INDEX IF NOT EXISTS kubedoor_ai_messages_session ON kubedoor_ai_messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS kubedoor_ai_actions_run ON kubedoor_ai_actions(run_id, created_at);

CREATE TABLE IF NOT EXISTS kubedoor_ai_memories (
    id uuid PRIMARY KEY, title text NOT NULL, content text NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by text NOT NULL, updated_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (char_length(title) BETWEEN 1 AND 200),
    CHECK (char_length(content) BETWEEN 1 AND 20000)
);
CREATE INDEX IF NOT EXISTS kubedoor_ai_memories_updated ON kubedoor_ai_memories(updated_at DESC,id);
ALTER TABLE kubedoor_ai_messages ADD COLUMN IF NOT EXISTS memories jsonb NOT NULL DEFAULT '[]';
