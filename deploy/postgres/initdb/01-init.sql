-- =============================================================================
-- KubeDoor PostgreSQL 首次初始化
--
-- 只在数据目录为空(容器第一次启动)时执行一次。已有数据的卷不会再跑。
-- 业务表结构不在这里建 —— kubedoor-master 启动时会自动执行 db.sql 建表,
-- 这里只调内核参数,外加一个排查慢查询用的扩展。
-- =============================================================================

-- pg_stat_statements:官方镜像自带的 contrib 扩展,排查慢查询用,不装也不影响 KubeDoor 运行
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- -----------------------------------------------------------------------------
-- 内核参数。ALTER SYSTEM 写进 postgresql.auto.conf,initdb 结束后容器重启 PG 生效。
--
-- 下面这组值按「4 核 8G 独占宿主机」估算,KubeDoor 的写入量主要来自 k8s_events。
-- 换机器请按比例调整,改完执行:
--   docker compose exec postgres psql -U kubedoor -d kubedoor \
--     -c "ALTER SYSTEM SET shared_buffers = '4GB';"
--   docker compose restart postgres
-- -----------------------------------------------------------------------------

-- 内存:shared_buffers 约为物理内存的 25%,effective_cache_size 约 50~75%
ALTER SYSTEM SET shared_buffers            = '2GB';
ALTER SYSTEM SET effective_cache_size      = '5GB';
ALTER SYSTEM SET maintenance_work_mem      = '512MB';
ALTER SYSTEM SET work_mem                  = '32MB';

-- 连接数:master 连接池最大 20,alarm/istio 另有少量连接,留足余量
ALTER SYSTEM SET max_connections           = '200';

-- 写入:KubeDoor 是写多读少(事件+指标批量 COPY),放宽 WAL 减少 checkpoint 抖动
ALTER SYSTEM SET wal_buffers               = '16MB';
ALTER SYSTEM SET min_wal_size              = '1GB';
ALTER SYSTEM SET max_wal_size              = '4GB';
ALTER SYSTEM SET checkpoint_completion_target = '0.9';

-- SSD 假设:随机读代价接近顺序读,并发 IO 拉高
ALTER SYSTEM SET random_page_cost          = '1.1';
ALTER SYSTEM SET effective_io_concurrency  = '200';

-- 统计与慢查询日志(超过 1s 的语句记日志,便于定位问题)
ALTER SYSTEM SET shared_preload_libraries  = 'pg_stat_statements';
ALTER SYSTEM SET log_min_duration_statement = '1000';
ALTER SYSTEM SET log_timezone              = 'Asia/Shanghai';
ALTER SYSTEM SET timezone                  = 'Asia/Shanghai';
