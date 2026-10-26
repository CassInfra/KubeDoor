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
--      Istio 管理:istio_group / istio_group_cluster / istio_group_vs_override / istio_cluster_state /
--      istio_vs / istio_vs_route / istio_vs_cluster / istio_vs_route_domain / istio_vs_snapshot
--   4. 账号与权限:auth_users / auth_user_clusters / auth_sessions / auth_settings,
--      操作审计 audit_logs(按 ts 保留 180 天,RETENTION_AUDIT_DAYS 可调)
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

-- 集群别名:AI 助手(企业微信机器人等)按别名识别用户说的是哪个集群,在 Web 的 Agent 管理页编辑
ALTER TABLE k8s_agent_status ADD COLUMN IF NOT EXISTS aliases text[] NOT NULL DEFAULT '{}';

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
    max_unavailable   text,                         -- Deployment strategy.rollingUpdate.maxUnavailable (整数或百分比)
    max_surge         text,                         -- Deployment strategy.rollingUpdate.maxSurge (整数或百分比)
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
    ADD COLUMN IF NOT EXISTS max_unavailable text,
    ADD COLUMN IF NOT EXISTS max_surge text,
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
-- 6~8. Istio VirtualService 管理(设计见 istio_route/README.md)
--   Istio 组 = 一套业务的集群(共用的测试集群 + 各项目的生产集群);一个集群可以属于多个组(如多个项目共用的测试集群),
--   同一个 VS 只能属于一个组:用组的包含 / 忽略规则和单个 VS 的手动忽略 / 单独纳入(istio_group_vs_override)划分;
--   组标准 VS 是花折保存的那一份:规则、默认路由、gateways 全组共用,hosts 和规则里的域名条件按集群各自维护;
--   每个 VS 自己选择部署到组内哪些集群(istio_vs_cluster);
--   istio_vs_snapshot 是采集到的集群现状(按集群,各组共用,只读),只用于对比、纳管和状态判断,不直接修改。
-- =============================================================================
-- 旧版(VS 按名称全局唯一、按单集群采集)的三张表,功能未正式使用,数据直接删除
DROP TABLE IF EXISTS k8s_cluster, vs_http_routes, vs_global;

CREATE TABLE IF NOT EXISTS istio_group (
    id               SERIAL PRIMARY KEY,
    name             varchar(100) NOT NULL,
    description      text NOT NULL DEFAULT '',
    include_patterns jsonb NOT NULL DEFAULT '[]',      -- 包含的 VS:[{type: glob|regex, pattern}],为空 = 全部
    ignore_patterns  jsonb NOT NULL DEFAULT '[]',      -- 忽略的 VS,优先于包含
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_istio_group_name UNIQUE (name)
);
ALTER TABLE istio_group ADD COLUMN IF NOT EXISTS include_patterns jsonb NOT NULL DEFAULT '[]';
ALTER TABLE istio_group ADD COLUMN IF NOT EXISTS ignore_patterns jsonb NOT NULL DEFAULT '[]';

DROP TRIGGER IF EXISTS trg_istio_group_updated ON istio_group;
CREATE TRIGGER trg_istio_group_updated BEFORE UPDATE ON istio_group
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 组成员:一个集群可以属于多个组
CREATE TABLE IF NOT EXISTS istio_group_cluster (
    group_id   integer NOT NULL REFERENCES istio_group(id) ON DELETE CASCADE,
    k8s_name   varchar(255) NOT NULL,
    env_type   varchar(10) NOT NULL DEFAULT 'test',     -- test 测试 / prod 生产(按列筛选、覆盖检查)
    remark     varchar(255) NOT NULL DEFAULT '',
    sort_order integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT istio_group_cluster_pkey PRIMARY KEY (group_id, k8s_name),
    CONSTRAINT ck_istio_group_cluster_env CHECK (env_type IN ('test', 'prod'))
);

-- 单个 VS 在组里的手动决定,优先于组的包含 / 忽略规则
CREATE TABLE IF NOT EXISTS istio_group_vs_override (
    group_id   integer NOT NULL REFERENCES istio_group(id) ON DELETE CASCADE,
    namespace  varchar(255) NOT NULL,
    name       varchar(255) NOT NULL,
    action     varchar(10) NOT NULL,                   -- ignore 手动忽略 / include 单独纳入(组规则排除的取消忽略)
    created_by varchar(255) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, namespace, name),
    CONSTRAINT ck_istio_group_vs_override_action CHECK (action IN ('ignore', 'include'))
);

-- 采集状态按集群:现状各组共用,集群不在任何组里时一并删除
CREATE TABLE IF NOT EXISTS istio_cluster_state (
    k8s_name     varchar(255) PRIMARY KEY,
    collected_at timestamptz,                           -- 最近一次采集现状
    collected_by varchar(255) NOT NULL DEFAULT '',
    vs_count     integer NOT NULL DEFAULT 0              -- 集群里的 VS 总数(不分组)
);

-- 组标准 VS 的共用部分;JSON 列未建模的字段原样保存,下发时原样带回(采集 → 下发不丢配置)
CREATE TABLE IF NOT EXISTS istio_vs (
    id                 SERIAL PRIMARY KEY,
    group_id           integer NOT NULL REFERENCES istio_group(id) ON DELETE CASCADE,
    namespace          varchar(255) NOT NULL,
    name               varchar(255) NOT NULL,
    gateways           jsonb NOT NULL DEFAULT '[]',
    df_forward_type    varchar(20),                     -- 默认路由(最后一条无 match 的规则):route / delegate
    df_forward_detail  jsonb,
    df_forward_timeout varchar(50),
    df_extra           jsonb,                           -- 默认路由里其他字段
    extra_spec         jsonb,                           -- spec 里 hosts / gateways / http 之外的字段
    updated_by         varchar(255) NOT NULL DEFAULT '',
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_istio_vs UNIQUE (group_id, namespace, name),
    CONSTRAINT ck_istio_vs_df_type CHECK (df_forward_type IS NULL OR df_forward_type IN ('route', 'delegate'))
);

DROP TRIGGER IF EXISTS trg_istio_vs_updated ON istio_vs;
CREATE TRIGGER trg_istio_vs_updated BEFORE UPDATE ON istio_vs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 组标准的 http 规则(不含默认路由),priority 越小越靠前
CREATE TABLE IF NOT EXISTS istio_vs_route (
    id             SERIAL PRIMARY KEY,
    vs_id          integer NOT NULL REFERENCES istio_vs(id) ON DELETE CASCADE,
    priority       integer NOT NULL,
    name           varchar(255),                        -- 用了重写就要有名称(访问日志展示),否则为空
    match_rules    jsonb,
    rewrite_rules  jsonb,
    forward_type   varchar(20),                         -- route / delegate;为空时转发方式在 extra 里(如 redirect)
    forward_detail jsonb,
    timeout        varchar(50),
    extra          jsonb,                               -- 其他字段(retries、corsPolicy 等)
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_istio_vs_route_fwd_type CHECK (forward_type IS NULL OR forward_type IN ('route', 'delegate'))
);
CREATE INDEX IF NOT EXISTS idx_istio_vs_route_vs ON istio_vs_route (vs_id, priority);

DROP TRIGGER IF EXISTS trg_istio_vs_route_updated ON istio_vs_route;
CREATE TRIGGER trg_istio_vs_route_updated BEFORE UPDATE ON istio_vs_route
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- VS 部署到哪些集群:hosts 各集群独立;applied_* 是最近一次下发(纳管时记为当时的集群现状)。
-- 集群必须是 VS 所在组的成员,移出组时由程序一并删除
CREATE TABLE IF NOT EXISTS istio_vs_cluster (
    vs_id        integer NOT NULL REFERENCES istio_vs(id) ON DELETE CASCADE,
    k8s_name     varchar(255) NOT NULL,
    hosts        jsonb NOT NULL DEFAULT '[]',
    applied_hash varchar(64),
    applied_at   timestamptz,
    applied_by   varchar(255) NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (vs_id, k8s_name)
);
CREATE INDEX IF NOT EXISTS idx_istio_vs_cluster_k8s ON istio_vs_cluster (k8s_name);

DROP TRIGGER IF EXISTS trg_istio_vs_cluster_updated ON istio_vs_cluster;
CREATE TRIGGER trg_istio_vs_cluster_updated BEFORE UPDATE ON istio_vs_cluster
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 规则里的域名条件(match 的 authority / headers.host)和 hosts 一样按集群保存:组标准规则的 match 是模板,
-- 域名条件的位置写成 {"$domains": true};这里是各集群在每个域名块里要匹配的域名,下发时每个域名展开成一个 match 条目。
-- 不再部署到某集群(删 istio_vs_cluster 的行)时一并删除
CREATE TABLE IF NOT EXISTS istio_vs_route_domain (
    route_id   integer NOT NULL REFERENCES istio_vs_route(id) ON DELETE CASCADE,
    vs_id      integer NOT NULL,
    k8s_name   varchar(255) NOT NULL,
    domains    jsonb NOT NULL DEFAULT '[]',                -- [[{field: authority|host, op, value}, ...], ...] 按域名块顺序
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (route_id, k8s_name),
    CONSTRAINT fk_istio_vs_route_domain_binding FOREIGN KEY (vs_id, k8s_name)
        REFERENCES istio_vs_cluster(vs_id, k8s_name) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_istio_vs_route_domain_binding ON istio_vs_route_domain (vs_id, k8s_name);

DROP TRIGGER IF EXISTS trg_istio_vs_route_domain_updated ON istio_vs_route_domain;
CREATE TRIGGER trg_istio_vs_route_domain_updated BEFORE UPDATE ON istio_vs_route_domain
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 集群现状(各组共用):每次采集整体替换该集群的行,集群不在任何组里时一并删除
CREATE TABLE IF NOT EXISTS istio_vs_snapshot (
    k8s_name         varchar(255) NOT NULL,
    namespace        varchar(255) NOT NULL,
    name             varchar(255) NOT NULL,
    spec             jsonb NOT NULL,
    resource_version varchar(64) NOT NULL DEFAULT '',
    collected_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (k8s_name, namespace, name)
);

-- 升级:早先一个集群只属于一个组(istio_group_cluster 主键是 k8s_name,采集状态也记在里面,
-- istio_vs_cluster / istio_vs_snapshot 外键引用它)。改成可以属于多个组,采集状态挪到 istio_cluster_state,数据保留
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'istio_group_cluster'
                 AND column_name = 'collected_at') THEN
        INSERT INTO istio_cluster_state (k8s_name, collected_at, collected_by, vs_count)
            SELECT k8s_name, collected_at, collected_by, vs_count FROM istio_group_cluster
            ON CONFLICT (k8s_name) DO NOTHING;
        ALTER TABLE istio_vs_cluster DROP CONSTRAINT IF EXISTS istio_vs_cluster_k8s_name_fkey;
        ALTER TABLE istio_vs_snapshot DROP CONSTRAINT IF EXISTS istio_vs_snapshot_k8s_name_fkey;
        ALTER TABLE istio_group_cluster DROP CONSTRAINT istio_group_cluster_pkey;
        ALTER TABLE istio_group_cluster ADD CONSTRAINT istio_group_cluster_pkey PRIMARY KEY (group_id, k8s_name);
        ALTER TABLE istio_group_cluster DROP COLUMN collected_at, DROP COLUMN collected_by, DROP COLUMN vs_count;
    END IF;
END $$;
DROP INDEX IF EXISTS idx_istio_group_cluster_group;  -- 主键 (group_id, k8s_name) 已覆盖
CREATE INDEX IF NOT EXISTS idx_istio_group_cluster_k8s ON istio_group_cluster (k8s_name);

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

-- =============================================================================
-- 10. 账号与权限(详见 auth/README.md)
--
--   auth_users          本地 / LDAP 账号与全局权限 role:
--                         admin 管理员 / rw 读写 / read 只读 / none 无 / agent 仅供 agent 连接
--   auth_user_clusters  按集群单独授权(rw / read / none),优先于全局权限;管理员和 Agent 账号没有
--   auth_sessions       登录会话。只存 token 的 SHA-256,token 本身只在浏览器 Cookie 里
--   auth_settings       认证相关设置(LDAP 配置等,JSON;LDAP 绑定密码加密存储)
-- =============================================================================
CREATE TABLE IF NOT EXISTS auth_users (
    id                   serial PRIMARY KEY,
    username             text        NOT NULL,
    source               text        NOT NULL DEFAULT 'local',
    role                 text        NOT NULL DEFAULT 'read',
    password_hash        text,                                -- 仅本地账号(scrypt)
    display_name         text        NOT NULL DEFAULT '',
    email                text        NOT NULL DEFAULT '',
    enabled              boolean     NOT NULL DEFAULT true,
    builtin              boolean     NOT NULL DEFAULT false,  -- 内置账号不能删除
    must_change_password boolean     NOT NULL DEFAULT false,  -- 下次登录必须先改口令
    password_default     boolean     NOT NULL DEFAULT false,  -- 仍在使用出厂口令
    password_changed_at  timestamptz,
    last_login_at        timestamptz,
    last_login_ip        text        NOT NULL DEFAULT '',
    comment              text        NOT NULL DEFAULT '',
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_auth_users_source CHECK (source IN ('local', 'ldap')),
    CONSTRAINT ck_auth_users_role CHECK (role IN ('admin', 'rw', 'read', 'none', 'agent'))
);
-- 用户名不区分大小写唯一
CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_users_username ON auth_users (lower(username));

CREATE TABLE IF NOT EXISTS auth_user_clusters (
    user_id    integer     NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    env        text        NOT NULL,
    permission text        NOT NULL,
    updated_by text        NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, env),
    CONSTRAINT ck_auth_user_clusters_perm CHECK (permission IN ('rw', 'read', 'none'))
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash   text        PRIMARY KEY,
    user_id      integer     NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    remember     boolean     NOT NULL DEFAULT false,
    ip           text        NOT NULL DEFAULT '',
    user_agent   text        NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions (expires_at);

CREATE TABLE IF NOT EXISTS auth_settings (
    key        text        PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_by text        NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- =============================================================================
-- 11. audit_logs —— 操作审计:登录、账号与权限变更、所有写操作(只追加,按 ts 过期清理)
-- =============================================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    id       bigserial   PRIMARY KEY,
    ts       timestamptz NOT NULL DEFAULT now(),
    username text        NOT NULL DEFAULT '',
    action   text        NOT NULL,
    env      text        NOT NULL DEFAULT '',
    target   text        NOT NULL DEFAULT '',
    detail   jsonb,
    ip       text        NOT NULL DEFAULT '',
    success  boolean     NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_ts ON audit_logs (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_ts ON audit_logs (lower(username), ts DESC);
