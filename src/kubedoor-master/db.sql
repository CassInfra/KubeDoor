-- =============================================================================
-- KubeDoor PostgreSQL 表结构
--
-- master 每次启动都会整份执行(见 k8s_event/pg_event_client.py 的
-- init_pg_tables_async),所以每条语句都必须幂等(IF NOT EXISTS)。
--
-- 设计要点:
--   1. 全部是原生普通表,不依赖任何扩展,也不需要超级用户 ——
--      官方镜像、云上托管 PG 都能直接用,数据库属主账号即可建表。
--   2. 三张只追加的大表,过期数据由 master 后台任务每天清理
--      (func_manager/data_retention.py,保留天数可用 RETENTION_*_DAYS 调整):
--      - k8s_resources      : 按 date           保留 365 天
--      - k8s_pod_alert_days : 按 start_time     保留 365 天
--      - k8s_events         : 按 lasttimestamp  保留 90 天
--   3. 配置/管控表:k8s_agent_status / k8s_res_control / alert_silences
--      Istio 路由表:vs_global / vs_http_routes / k8s_cluster
--
-- 时区:统一使用 timestamptz(UTC 存储),应用层按 Asia/Shanghai 展示。
-- =============================================================================

-- =============================================================================
-- 1. k8s_agent_status —— agent 配置表(每 env 一行,极小,普通表)
-- =============================================================================
CREATE TABLE IF NOT EXISTS k8s_agent_status (
    env                 text PRIMARY KEY,
    collect             boolean NOT NULL DEFAULT false,
    peak_hours          text    NOT NULL DEFAULT '',
    admission           boolean NOT NULL DEFAULT false,
    admission_namespace text    NOT NULL DEFAULT '',
    nms_not_confirm     boolean NOT NULL DEFAULT false,
    scheduler           boolean NOT NULL DEFAULT false
);

-- =============================================================================
-- 2. k8s_res_control —— 资源管控表(env×namespace×deployment,热路径 UPDATE)
--    准入 webhook 每次部署都会按主键实时查询
-- =============================================================================
CREATE TABLE IF NOT EXISTS k8s_res_control (
    env               text     NOT NULL,
    namespace         text     NOT NULL,
    deployment        text     NOT NULL,
    pod_count_init    smallint NOT NULL DEFAULT 0,   -- 初始化pod数,仅首次记录
    pod_count         smallint NOT NULL DEFAULT 0,   -- 当前pod数,第三优先级
    pod_count_manual  smallint NOT NULL DEFAULT -1,  -- 手动pod数,第一优先级
    p95_pod_cpu_pct   real     NOT NULL DEFAULT -1,
    p95_pod_mem_pct   real     NOT NULL DEFAULT -1,
    p95_pod_heap_pct  real CHECK (p95_pod_heap_pct >= 0), -- 选定高峰日的堆内存使用率P95(%)，只读；缺失为NULL
    p95_pod_g1e_pct   real CHECK (p95_pod_g1e_pct >= 0), -- 选定高峰日G1 Eden占用率P95(%)，只读；缺失为NULL
    request_cpu_m     integer  NOT NULL DEFAULT -1,  -- 高峰期p95 CPU,最小1
    request_mem_mb    integer  NOT NULL DEFAULT -1,  -- 高峰期p95 内存,最小1
    limit_cpu_m       integer  NOT NULL DEFAULT -1,
    limit_mem_mb      integer  NOT NULL DEFAULT -1,
    -- JVM 参数按精确字节保存；NULL 表示尚未采集，0 保留 JVM 显式默认值语义。
    jvm_xms_bytes     bigint CHECK (jvm_xms_bytes BETWEEN 0 AND 9007199254740991),
    jvm_xmx_bytes     bigint CHECK (jvm_xmx_bytes BETWEEN 0 AND 9007199254740991),
    jvm_xss_bytes     bigint CHECK (jvm_xss_bytes BETWEEN 0 AND 9007199254740991),
    jvm_max_metaspace_bytes bigint CHECK (jvm_max_metaspace_bytes BETWEEN 0 AND 9007199254740991),
    "update"          timestamptz,
    pod_mem_saved_mb  real     NOT NULL DEFAULT -1,
    pod_qps           real     NOT NULL DEFAULT -1,
    pod_g1gc_qps      real     NOT NULL DEFAULT -1,
    pod_count_ai      smallint NOT NULL DEFAULT -1,  -- AI计算pod数,第二优先级
    pod_qps_ai        real     NOT NULL DEFAULT -1,
    pod_load_ai       real     NOT NULL DEFAULT -1,
    pod_g1gc_qps_ai   real     NOT NULL DEFAULT -1,
    update_ai         timestamptz,
    PRIMARY KEY (env, namespace, deployment)
);

-- 已有数据库升级：master 启动执行，重复运行不会修改已有管控值。
-- 旧库可能保留 p80_pod_heap_mb（MiB）和 p90_pod_heap_pct（P90）；P95另建列，不改名或复用旧值。
ALTER TABLE k8s_res_control
    ADD COLUMN IF NOT EXISTS jvm_xms_bytes bigint CHECK (jvm_xms_bytes BETWEEN 0 AND 9007199254740991),
    ADD COLUMN IF NOT EXISTS jvm_xmx_bytes bigint CHECK (jvm_xmx_bytes BETWEEN 0 AND 9007199254740991),
    ADD COLUMN IF NOT EXISTS jvm_xss_bytes bigint CHECK (jvm_xss_bytes BETWEEN 0 AND 9007199254740991),
    ADD COLUMN IF NOT EXISTS jvm_max_metaspace_bytes bigint CHECK (jvm_max_metaspace_bytes BETWEEN 0 AND 9007199254740991),
    ADD COLUMN IF NOT EXISTS p95_pod_heap_pct real CHECK (p95_pod_heap_pct >= 0),
    ADD COLUMN IF NOT EXISTS p95_pod_g1e_pct real CHECK (p95_pod_g1e_pct >= 0);

-- =============================================================================
-- 3. k8s_resources —— 每日高峰时段资源快照(每个服务每天一行,只追加)
--    每天 01:00 由 kubedoor-collect 触发,用 PromQL 从时序库聚合后写入
-- =============================================================================
CREATE TABLE IF NOT EXISTS k8s_resources (
    date              timestamptz NOT NULL,
    env               text        NOT NULL,
    namespace         text        NOT NULL,
    deployment        text        NOT NULL,
    pod_count         smallint    NOT NULL,
    p95_pod_load      real        NOT NULL,
    p95_pod_cpu_pct   real        NOT NULL,
    p95_pod_wss_mb    real        NOT NULL,
    p95_pod_wss_pct   real        NOT NULL,
    limit_pod_cpu_m   real        NOT NULL,
    limit_pod_mem_mb  real        NOT NULL,
    request_pod_cpu_m real        NOT NULL,
    request_pod_mem_mb real       NOT NULL,
    p95_pod_qps       real        NOT NULL,
    p95_pod_g1gc_qps  real        NOT NULL,
    pod_jvm_max_mb    real        NOT NULL,
    p95_pod_heap_pct  real CHECK (p95_pod_heap_pct >= 0), -- 每日高峰窗口堆内存使用率P95(%)，缺失为NULL
    p95_pod_g1e_pct   real CHECK (p95_pod_g1e_pct >= 0) -- 每日高峰窗口G1 Eden占用率P95(%)，缺失为NULL
);

ALTER TABLE k8s_resources
    -- 保留旧版 p80_pod_heap_mb / p90_pod_heap_pct；旧历史记录的P95保持NULL。
    ADD COLUMN IF NOT EXISTS p95_pod_heap_pct real CHECK (p95_pod_heap_pct >= 0),
    ADD COLUMN IF NOT EXISTS p95_pod_g1e_pct real CHECK (p95_pod_g1e_pct >= 0);

-- 按时间范围查询(Grafana 看板)与过期清理
CREATE INDEX IF NOT EXISTS idx_k8s_resources_date
    ON k8s_resources (date DESC);
-- 常用查询维度索引(env + 时间)
CREATE INDEX IF NOT EXISTS idx_k8s_resources_env_date
    ON k8s_resources (env, date DESC);

-- =============================================================================
-- 4. k8s_pod_alert_days —— Pod 告警明细(追加 + 计数 UPDATE)
--    kubedoor-alarm 按「当天 + fingerprint」先查后改/插;
--    silenced / silence_id 是告警屏蔽标记:命中屏蔽规则时通知被拦截,但记录照常入库
-- =============================================================================
CREATE TABLE IF NOT EXISTS k8s_pod_alert_days (
    fingerprint     text        NOT NULL,
    alert_status    text        NOT NULL DEFAULT '',
    send_resolved   boolean     NOT NULL DEFAULT true,
    count_firing    integer     NOT NULL DEFAULT 0,
    count_resolved  integer     NOT NULL DEFAULT 0,
    start_time      timestamptz NOT NULL,
    end_time        timestamptz,
    severity        text        NOT NULL DEFAULT '',
    alert_group     text        NOT NULL DEFAULT '',
    alert_name      text        NOT NULL DEFAULT '',
    env             text        NOT NULL DEFAULT '',
    namespace       text        NOT NULL DEFAULT '',
    container       text        NOT NULL DEFAULT '',
    pod             text        NOT NULL DEFAULT '',
    description     text        NOT NULL DEFAULT '',
    operate         text        NOT NULL DEFAULT '',
    silenced        boolean     NOT NULL DEFAULT false,  -- 最近一次告警是否被屏蔽
    silence_id      integer                               -- 命中的屏蔽规则 ID,未命中为 NULL
);

-- start_time 打头的唯一索引同时承担按时间范围查询与过期清理
CREATE UNIQUE INDEX IF NOT EXISTS uq_pod_alert_start_fp
    ON k8s_pod_alert_days (start_time, fingerprint);
CREATE INDEX IF NOT EXISTS idx_pod_alert_env_start
    ON k8s_pod_alert_days (env, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_pod_alert_fingerprint
    ON k8s_pod_alert_days (fingerprint);

CREATE INDEX IF NOT EXISTS idx_pod_alert_silenced
    ON k8s_pod_alert_days (silenced, start_time DESC) WHERE silenced;

-- =============================================================================
-- 5. k8s_events —— K8S 事件(数据量最大的一张表)
--    同一个事件在 K8S 里重复发生时,是在原 Event 对象上累加 count、刷新
--    lastTimestamp,metadata.uid 不变。这里以 (eventuid, k8s) 唯一,写入走
--    INSERT ... ON CONFLICT DO UPDATE 原地更新,一个事件始终只有一行。
-- =============================================================================
CREATE TABLE IF NOT EXISTS k8s_events (
    eventuid            text        NOT NULL,   -- 事件唯一标识(metadata.uid)
    eventstatus         text        NOT NULL,   -- ADDED/MODIFIED/DELETED
    level               text        NOT NULL,   -- Normal/Warning/已告警
    count               integer     NOT NULL DEFAULT 0,
    kind                text        NOT NULL DEFAULT '',
    k8s                 text        NOT NULL DEFAULT '',
    namespace           text        NOT NULL DEFAULT '',
    name                text        NOT NULL DEFAULT '',
    reason              text        NOT NULL DEFAULT '',
    message             text        NOT NULL DEFAULT '',
    firsttimestamp      timestamptz NOT NULL,
    lasttimestamp       timestamptz NOT NULL,
    reportingcomponent  text        NOT NULL DEFAULT '',
    reportinginstance   text        NOT NULL DEFAULT '',
    createdat           timestamptz NOT NULL DEFAULT now()
);

-- 去重唯一键,即 upsert 的 ON CONFLICT 目标。eventuid 放前面,
-- 告警流程按 eventuid 单独更新 level 时也能走这个索引。
CREATE UNIQUE INDEX IF NOT EXISTS uq_k8s_events_uid_k8s
    ON k8s_events (eventuid, k8s);
-- 按时间范围查询与过期清理
CREATE INDEX IF NOT EXISTS idx_k8s_events_lasttimestamp
    ON k8s_events (lasttimestamp DESC);
CREATE INDEX IF NOT EXISTS idx_k8s_events_k8s_ns_ts
    ON k8s_events (k8s, namespace, lasttimestamp DESC);
CREATE INDEX IF NOT EXISTS idx_k8s_events_level_kind
    ON k8s_events (level, kind);
CREATE INDEX IF NOT EXISTS idx_k8s_events_reason
    ON k8s_events (reason);

-- =============================================================================
-- 通用:updated_at 自动维护触发器函数
-- =============================================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- 6. Istio VirtualService —— 全局配置表
-- =============================================================================
CREATE TABLE IF NOT EXISTS vs_global (
    id                 SERIAL PRIMARY KEY,
    name               varchar(255) NOT NULL,
    namespace          varchar(255) NOT NULL DEFAULT 'default',
    gateways           text,                    -- JSON 数组
    hosts              text NOT NULL,           -- JSON 数组
    protocol           varchar(50) DEFAULT 'http',
    df_forward_type    varchar(20),             -- 'route' 或 'delegate'
    df_forward_detail  text,
    df_forward_timeout varchar(50),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_vs_global_name_ns UNIQUE (name, namespace),
    CONSTRAINT ck_vs_global_fwd_type CHECK (df_forward_type IS NULL OR df_forward_type IN ('route', 'delegate'))
);
CREATE INDEX IF NOT EXISTS idx_vs_global_name_ns ON vs_global (name, namespace);

DROP TRIGGER IF EXISTS trg_vs_global_updated ON vs_global;
CREATE TRIGGER trg_vs_global_updated BEFORE UPDATE ON vs_global
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- 7. Istio HTTP 路由规则表
-- =============================================================================
CREATE TABLE IF NOT EXISTS vs_http_routes (
    id             SERIAL PRIMARY KEY,
    vs_global_id   integer NOT NULL REFERENCES vs_global(id) ON DELETE CASCADE,
    name           varchar(255),
    priority       integer DEFAULT 0,           -- 数字越小优先级越高
    match_rules    text,                        -- JSON
    rewrite_rules  text,                        -- JSON
    forward_type   varchar(20) NOT NULL,
    forward_detail text NOT NULL,               -- JSON
    timeout        varchar(50),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_vs_routes_fwd_type CHECK (forward_type IN ('route', 'delegate'))
);
CREATE INDEX IF NOT EXISTS idx_vs_routes_vs_global_id ON vs_http_routes (vs_global_id);
CREATE INDEX IF NOT EXISTS idx_vs_routes_priority ON vs_http_routes (vs_global_id, priority);

DROP TRIGGER IF EXISTS trg_vs_routes_updated ON vs_http_routes;
CREATE TRIGGER trg_vs_routes_updated BEFORE UPDATE ON vs_http_routes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- 8. K8S 集群关联表
-- =============================================================================
CREATE TABLE IF NOT EXISTS k8s_cluster (
    id         SERIAL PRIMARY KEY,
    k8s_name   varchar(255) NOT NULL,
    vs_id      integer NOT NULL REFERENCES vs_global(id) ON DELETE CASCADE,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_k8s_cluster_name_vs UNIQUE (k8s_name, vs_id)
);
CREATE INDEX IF NOT EXISTS idx_k8s_cluster_name ON k8s_cluster (k8s_name);
CREATE INDEX IF NOT EXISTS idx_k8s_cluster_vs_id ON k8s_cluster (vs_id);

DROP TRIGGER IF EXISTS trg_k8s_cluster_updated ON k8s_cluster;
CREATE TRIGGER trg_k8s_cluster_updated BEFORE UPDATE ON k8s_cluster
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- 9. alert_silences —— 告警屏蔽规则(Alertmanager Silence 风格)
--
--   matchers 为 JSONB 数组,每项 {"key":"env","op":"=","value":"prod-hz"};
--   同一规则内多个 matcher 为 AND,多条规则之间为 OR(任一命中即屏蔽)。
--   op 取值: '='(等于) '!='(不等于) '=~'(正则全匹配) '!~'(正则不匹配)
--
--   状态不落库,由时间字段实时推导(见 func_manager/silence_api.py SILENCE_STATUS_SQL):
--     revoked_at IS NOT NULL              → revoked  已解除
--     now() < starts_at                   → pending  待生效
--     ends_at IS NOT NULL AND now()>=ends_at → expired  已过期
--     其余                                 → active   生效中
--   ends_at 为 NULL 表示长期有效(不自动过期)。
-- =============================================================================
CREATE TABLE IF NOT EXISTS alert_silences (
    id            SERIAL PRIMARY KEY,
    matchers      jsonb       NOT NULL,
    starts_at     timestamptz NOT NULL DEFAULT now(),
    ends_at       timestamptz,                    -- NULL = 长期有效
    comment       text        NOT NULL DEFAULT '',
    created_by    text        NOT NULL DEFAULT '',
    revoked_at    timestamptz,                    -- 非 NULL = 已手动解除
    revoked_by    text        NOT NULL DEFAULT '',
    match_count   bigint      NOT NULL DEFAULT 0, -- 累计屏蔽掉的告警条数
    last_match_at timestamptz,                    -- 最近一次命中时间
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_alert_silences_window
        CHECK (ends_at IS NULL OR ends_at > starts_at),
    CONSTRAINT ck_alert_silences_matchers
        CHECK (jsonb_typeof(matchers) = 'array' AND jsonb_array_length(matchers) > 0)
);

-- alarm 侧热路径:每 SILENCE_CACHE_TTL 秒拉取一次"未解除且未过期"的规则
CREATE INDEX IF NOT EXISTS idx_alert_silences_live
    ON alert_silences (ends_at) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alert_silences_created
    ON alert_silences (created_at DESC);

DROP TRIGGER IF EXISTS trg_alert_silences_updated ON alert_silences;
CREATE TRIGGER trg_alert_silences_updated BEFORE UPDATE ON alert_silences
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
