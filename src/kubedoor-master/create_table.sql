CREATE DATABASE IF NOT EXISTS kubedoor ENGINE=Atomic;

CREATE TABLE IF NOT EXISTS kubedoor.k8s_agent_status
    (
        `env` String,
        `collect` Bool DEFAULT false,
        `peak_hours` String,
        `admission` Bool DEFAULT false,
        `admission_namespace` String,
        `nms_not_confirm` Bool DEFAULT false,
        `scheduler` Bool DEFAULT false
    )
    ENGINE = MergeTree
    PRIMARY KEY tuple(env)
    SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS kubedoor.k8s_res_control
    (
        env String,
        namespace String,
        deployment String,
        pod_count_init UInt8 DEFAULT 0 COMMENT '初始化pod数,仅首次记录该值,从当前pod数复制',
        pod_count UInt8 DEFAULT 0 COMMENT '当前pod数,第三优先级',
        pod_count_manual Int16 DEFAULT -1 COMMENT '手动填写需要的pod数,第一优先级,webhook拦截时读取到-1,则取pod_count_ai',
        p95_pod_cpu_pct Float32 DEFAULT -1,
        p95_pod_mem_pct Float32 DEFAULT -1,
        request_cpu_m Int32 DEFAULT -1 COMMENT '取自高峰期的p95 CPU负载,最小1,新建服务时,该值默认为-1,webhook拦截时读取到-1,则不改变改字段,并通知。',
        request_mem_mb Int32 DEFAULT -1 COMMENT '取自高峰期的p95 内存使用量,最小1,新建服务时,该值默认为-1,webhook拦截时读取到-1,则不改变改字段,并通知。',
        limit_cpu_m Int32 DEFAULT -1 COMMENT '初始化的时候取当前pod的limit cpu,没有则为-1,webhook拦截时读取到-1,则不改变改字段,并通知。',
        limit_mem_mb Int32 DEFAULT -1 COMMENT '初始化的时候取当前pod的limit mem,没有则为-1,webhook拦截时读取到-1,则不改变改字段,并通知。',
        update DateTime('Asia/Shanghai'),
        pod_mem_saved_mb Float32 DEFAULT -1,
        pod_qps Float32 DEFAULT -1,
        pod_g1gc_qps Float32 DEFAULT -1,
        pod_count_ai Int16 DEFAULT -1 COMMENT 'AI计算后需要的pod数,第二优先级,webhook拦截时读取到-1,则取pod_count',
        pod_qps_ai Float32 DEFAULT -1,
        pod_load_ai Float32 DEFAULT -1,
        pod_g1gc_qps_ai Float32 DEFAULT -1,
        update_ai DateTime('Asia/Shanghai')
    )
    ENGINE = MergeTree
    PRIMARY KEY (env,namespace,deployment)
    ORDER BY (env,namespace,deployment)
    SETTINGS index_granularity = 8192;


CREATE TABLE IF NOT EXISTS kubedoor.k8s_resources
    (
        date DateTime('Asia/Shanghai'),
        env String,
        namespace String,
        deployment String,
        pod_count UInt8,
        p95_pod_load Float32,
        p95_pod_cpu_pct Float32,
        p95_pod_wss_mb Float32,
        p95_pod_wss_pct Float32,
        limit_pod_cpu_m Float32,
        limit_pod_mem_mb Float32,
        request_pod_cpu_m Float32,
        request_pod_mem_mb Float32,
        p95_pod_qps Float32,
        p95_pod_g1gc_qps Float32,
        pod_jvm_max_mb Float32
    )
    ENGINE = MergeTree
    PARTITION BY toYYYYMM(date)
    PRIMARY KEY (date,env,namespace,deployment)
    ORDER BY (date,env,namespace,deployment)
    TTL toDateTime(date) + toIntervalDay(365)
    SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS kubedoor.k8s_pod_alert_days
    (
        `fingerprint` String,
        `alert_status` String,
        `send_resolved` Bool DEFAULT true,
        `count_firing` UInt32,
        `count_resolved` Int32,
        `start_time` DateTime('Asia/Shanghai'),
        `end_time` Nullable(DateTime('Asia/Shanghai')) DEFAULT NULL,
        `severity` String,
        `alert_group` String,
        `alert_name` String,
        `env` String,
        `namespace` String,
        `container` String,
        `pod` String,
        `description` String,
        `operate` String
    )
    ENGINE = MergeTree
    PARTITION BY toYYYYMMDD(start_time)
    PRIMARY KEY (start_time,
      fingerprint,
      severity,
      env,
      alert_group,
      alert_name)
    TTL toDateTime(start_time) + toIntervalDay(365)
    SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS k8s_events (
    eventUid String,                    -- 事件唯一标识符（来自metadata.uid）
    eventStatus LowCardinality(String), -- 事件状态（ADDED/MODIFIED/DELETED）
    level LowCardinality(String),       -- 事件级别（Normal/Warning）
    count UInt32,                       -- 事件发生次数
    kind LowCardinality(String),        -- 涉及对象的类型
    k8s String,                         -- K8S集群名称
    namespace LowCardinality(String),   -- 命名空间
    name String,                        -- 涉及对象的名称
    reason LowCardinality(String),      -- 事件原因
    message String,                     -- 事件详细消息
    firstTimestamp DateTime('Asia/Shanghai'), -- 首次发生时间（北京时间）
    lastTimestamp DateTime('Asia/Shanghai'),  -- 最后发生时间（北京时间）
    reportingComponent String,          -- 报告组件
    reportingInstance String,           -- 报告实例
    createdAt DateTime('Asia/Shanghai') DEFAULT now('Asia/Shanghai') -- 记录创建时间
)
ENGINE = ReplacingMergeTree(lastTimestamp)
PARTITION BY (k8s, toYYYYMM(lastTimestamp))
ORDER BY (eventUid, k8s, namespace)
PRIMARY KEY (eventUid)
TTL lastTimestamp + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;

CREATE INDEX IF NOT EXISTS idx_k8s_namespace_time ON k8s_events (k8s, namespace, lastTimestamp) TYPE minmax GRANULARITY 1;
CREATE INDEX IF NOT EXISTS idx_level_kind ON k8s_events (level, kind) TYPE set(100) GRANULARITY 1;
CREATE INDEX IF NOT EXISTS idx_reason ON k8s_events (reason) TYPE bloom_filter(0.01) GRANULARITY 1;