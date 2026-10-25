CREATE TABLE IF NOT EXISTS kubedoor_ai_connections (
    env text PRIMARY KEY, encrypted_config bytea NOT NULL, context text NOT NULL,
    revision integer NOT NULL DEFAULT 1, test_status text NOT NULL DEFAULT 'success',
    tested_at timestamptz NOT NULL DEFAULT now(), updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kubedoor_ai_sessions (
    id uuid PRIMARY KEY, username text NOT NULL, title text NOT NULL,
    idempotency_key text, created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(username, idempotency_key),
    channel text NOT NULL DEFAULT 'web', channel_key text
);
ALTER TABLE kubedoor_ai_sessions ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'web';
ALTER TABLE kubedoor_ai_sessions ADD COLUMN IF NOT EXISTS channel_key text;
CREATE INDEX IF NOT EXISTS kubedoor_ai_sessions_channel ON kubedoor_ai_sessions(channel, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS kubedoor_ai_sessions_channel_key ON kubedoor_ai_sessions(channel, channel_key) WHERE channel_key IS NOT NULL;
-- 已删除 LangGraph 检查点的时间(企微里再也不会继续的旧对话,wecom.py 定期清理);会话和消息记录保留,不能再继续
ALTER TABLE kubedoor_ai_sessions ADD COLUMN IF NOT EXISTS checkpoint_purged_at timestamptz;
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
-- 本轮模型用量（runtime.UsageMiddleware）：调用次数、输入/输出/缓存命中/推理 token、最近一次输入和模型窗口
ALTER TABLE kubedoor_ai_runs ADD COLUMN IF NOT EXISTS usage jsonb;
-- 这一轮的用户消息在检查点里的 id 是 user-<run id>，重新生成 / 编辑后重发靠它找到要删掉的那一轮；之前的旧 run 没有
ALTER TABLE kubedoor_ai_runs ADD COLUMN IF NOT EXISTS rewindable boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS kubedoor_ai_messages (
    id uuid PRIMARY KEY, session_id uuid NOT NULL REFERENCES kubedoor_ai_sessions(id) ON DELETE CASCADE,
    run_id uuid REFERENCES kubedoor_ai_runs(id) ON DELETE SET NULL,
    role text NOT NULL, content text NOT NULL, scope jsonb, tools jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- 被重新生成 / 编辑后重发替换掉的那一轮消息：保留用于追溯，不再显示
ALTER TABLE kubedoor_ai_messages ADD COLUMN IF NOT EXISTS replaced_at timestamptz;
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


CREATE TABLE IF NOT EXISTS kubedoor_ai_wecom_config (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    enabled boolean NOT NULL DEFAULT false,
    encrypted_config bytea NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- 未绑定的企微成员按这个花折账号的权限使用机器人;为空时必须先绑定
ALTER TABLE kubedoor_ai_wecom_config ADD COLUMN IF NOT EXISTS unbound_username text
    CHECK (unbound_username IS NULL OR char_length(unbound_username) BETWEEN 1 AND 200);
-- 未绑定成员的默认集群;为空 = 「无」,成员要先切换集群才能提问
ALTER TABLE kubedoor_ai_wecom_config ADD COLUMN IF NOT EXISTS unbound_default_env text
    CHECK (unbound_default_env IS NULL OR char_length(unbound_default_env) BETWEEN 1 AND 255);
-- 企微对话空闲多少分钟后，新消息自动进入新对话;0 = 关闭
ALTER TABLE kubedoor_ai_wecom_config ADD COLUMN IF NOT EXISTS session_idle_minutes integer NOT NULL DEFAULT 120
    CHECK (session_idle_minutes BETWEEN 0 AND 10080);
CREATE TABLE IF NOT EXISTS kubedoor_ai_wecom_users (
    wecom_userid text PRIMARY KEY,
    username text,
    default_env text,
    current_env text,
    session_id uuid REFERENCES kubedoor_ai_sessions(id) ON DELETE SET NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (char_length(wecom_userid) BETWEEN 1 AND 256),
    CHECK (username IS NULL OR char_length(username) BETWEEN 1 AND 200)
);
CREATE INDEX IF NOT EXISTS kubedoor_ai_wecom_users_username ON kubedoor_ai_wecom_users(username);
-- session_id 是成员的当前对话;previous_session_id 是上一个对话,「继续上次」切回去
ALTER TABLE kubedoor_ai_wecom_users ADD COLUMN IF NOT EXISTS previous_session_id uuid
    REFERENCES kubedoor_ai_sessions(id) ON DELETE SET NULL;
CREATE TABLE IF NOT EXISTS kubedoor_ai_wecom_inbox (
    msgid text PRIMARY KEY,
    wecom_userid text NOT NULL,
    kind text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kubedoor_ai_wecom_approvals (
    task_id text PRIMARY KEY,
    action_id uuid NOT NULL REFERENCES kubedoor_ai_actions(id) ON DELETE CASCADE,
    run_id uuid NOT NULL REFERENCES kubedoor_ai_runs(id) ON DELETE CASCADE,
    wecom_userid text NOT NULL,
    username text NOT NULL,
    session_id uuid NOT NULL REFERENCES kubedoor_ai_sessions(id) ON DELETE CASCADE,
    resume_after_seq bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(action_id)
);
