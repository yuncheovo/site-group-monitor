-- ============================================================================
-- site_group_monitor SQLite schema（唯一权威 DDL）
--   7 张表：sites / check_results / daily_snapshots / proxies /
--           change_events / audit_logs / runs
-- 约定：
--   * 时间列一律存 UTC ISO8601 字符串 'YYYY-MM-DDTHH:MM:SSZ'
--   * stat_date 为 YYYY-MM-DD（按 app.display_timezone 计算）
--   * 所有建表/建索引语句幂等（IF NOT EXISTS），可重复执行
-- 注意：PRAGMA foreign_keys 由 storage/database.py 在每个连接上开启，
--      这里保留为注释说明，避免在有活动事务时 PRAGMA 不生效。
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- 1) 站点主表 ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sites (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  domain          TEXT    NOT NULL UNIQUE,          -- 归一化域名 e.g. example.com
  root_url        TEXT    NOT NULL DEFAULT '',      -- 人工覆盖的首页 URL，空则自动 https://domain/
  name            TEXT    NOT NULL DEFAULT '',      -- 备注名
  group_name      TEXT    NOT NULL DEFAULT 'default',
  tags            TEXT    NOT NULL DEFAULT '[]',    -- JSON array
  enabled         INTEGER NOT NULL DEFAULT 1,
  baseline_done   INTEGER NOT NULL DEFAULT 0,       -- 是否已建立基线
  notes           TEXT    NOT NULL DEFAULT '',
  created_at      TEXT    NOT NULL,
  updated_at      TEXT    NOT NULL,
  last_checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sites_domain  ON sites(domain);
CREATE INDEX IF NOT EXISTS idx_sites_enabled ON sites(enabled);
CREATE INDEX IF NOT EXISTS idx_sites_group   ON sites(group_name);

-- 2) 每次检测明细（一条 = 一站一次全维度检测）--------------------------------
CREATE TABLE IF NOT EXISTS check_results (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id             INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  task_id             TEXT    NOT NULL,             -- 一轮任务 UUID
  run_mode            TEXT    NOT NULL DEFAULT 'full',
  checked_at          TEXT    NOT NULL,             -- UTC ISO
  index_status        TEXT    NOT NULL,             -- indexed/not_indexed/unknown_need_manual_check
  index_reason        TEXT    NOT NULL DEFAULT '',
  index_confidence    REAL    NOT NULL DEFAULT 0,
  index_result_count  INTEGER NOT NULL DEFAULT 0,
  index_top_url       TEXT    NOT NULL DEFAULT '',
  evidence_path       TEXT    NOT NULL DEFAULT '',
  http_status         INTEGER,                    -- 站点自身 HTTP 状态码（健康字段）
  index_http_status   INTEGER,                    -- 百度检索页自身状态码（旁路记录，不污染健康字段）
  response_ms         INTEGER,
  final_url           TEXT    NOT NULL DEFAULT '',
  redirect_chain      TEXT    NOT NULL DEFAULT '[]',-- JSON: [{url,status,location}]
  title               TEXT    NOT NULL DEFAULT '',
  description         TEXT    NOT NULL DEFAULT '',
  keywords            TEXT    NOT NULL DEFAULT '',
  tdk_source_url      TEXT    NOT NULL DEFAULT '',
  tdk_encoding        TEXT    NOT NULL DEFAULT '',
  tdk_title_hash      TEXT    NOT NULL DEFAULT '',
  ssl_ok              INTEGER,
  ssl_issuer          TEXT    NOT NULL DEFAULT '',
  ssl_not_after       TEXT    NOT NULL DEFAULT '',
  ssl_days_left       INTEGER,
  anomalies           TEXT    NOT NULL DEFAULT '{}',-- JSON: {mixed_content, redirect_anomaly, ...}
  error_type          TEXT    NOT NULL DEFAULT 'none',
  error_msg           TEXT    NOT NULL DEFAULT '',
  proxy_id            INTEGER,
  duration_ms         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cr_checked      ON check_results(checked_at);
CREATE INDEX IF NOT EXISTS idx_cr_site_time    ON check_results(site_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_cr_index_status ON check_results(index_status);
CREATE INDEX IF NOT EXISTS idx_cr_task         ON check_results(task_id);

-- 3) 每日快照（趋势 / diff 的唯一数据源）-------------------------------------
CREATE TABLE IF NOT EXISTS daily_snapshots (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id        INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  stat_date      TEXT    NOT NULL,              -- YYYY-MM-DD（本地时区）
  index_status   TEXT    NOT NULL,
  http_status    INTEGER,
  response_ms    INTEGER,
  ssl_days_left  INTEGER,
  title          TEXT    NOT NULL DEFAULT '',
  tdk_title_hash TEXT    NOT NULL DEFAULT '',
  is_baseline    INTEGER NOT NULL DEFAULT 0,    -- 1 = 首次基线，不参与 diff
  created_at     TEXT    NOT NULL,
  UNIQUE(site_id, stat_date)
);
CREATE INDEX IF NOT EXISTS idx_ds_date      ON daily_snapshots(stat_date);
CREATE INDEX IF NOT EXISTS idx_ds_site_date ON daily_snapshots(site_id, stat_date DESC);
CREATE INDEX IF NOT EXISTS idx_ds_status    ON daily_snapshots(stat_date, index_status);

-- 4) 代理池 ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proxies (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT    NOT NULL DEFAULT '',
  protocol          TEXT    NOT NULL DEFAULT 'http',   -- http/https/socks5
  host              TEXT    NOT NULL,
  port              INTEGER NOT NULL,
  username          TEXT    NOT NULL DEFAULT '',
  password_enc      TEXT    NOT NULL DEFAULT '',       -- 明文存放（本地工具），展示/日志一律脱敏
  weight            REAL    NOT NULL DEFAULT 1.0,
  success_count     INTEGER NOT NULL DEFAULT 0,
  fail_count        INTEGER NOT NULL DEFAULT 0,
  consecutive_fail  INTEGER NOT NULL DEFAULT 0,
  health_score      REAL    NOT NULL DEFAULT 1.0,
  disabled          INTEGER NOT NULL DEFAULT 0,
  source            TEXT    NOT NULL DEFAULT 'manual',
  source_ref        TEXT    NOT NULL DEFAULT '',
  last_ok_at        TEXT,
  last_fail_at      TEXT,
  created_at        TEXT    NOT NULL,
  last_egress_ip    TEXT    NOT NULL DEFAULT '',
  last_location     TEXT    NOT NULL DEFAULT '',
  last_latency_ms   INTEGER,
  last_probe_at     TEXT,
  last_probe_error  TEXT    NOT NULL DEFAULT '',
  UNIQUE(protocol, host, port)
);
CREATE INDEX IF NOT EXISTS idx_px_enabled ON proxies(disabled, health_score DESC);

-- 5) 变化事件（diff 产物，驱动报告与告警）------------------------------------
CREATE TABLE IF NOT EXISTS change_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  event_type  TEXT    NOT NULL,              -- new_indexed/lost_indexed/...
  old_value   TEXT    NOT NULL DEFAULT '',
  new_value   TEXT    NOT NULL DEFAULT '',
  stat_date   TEXT    NOT NULL,
  detail      TEXT    NOT NULL DEFAULT '{}', -- JSON
  notified    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ce_date ON change_events(stat_date, event_type);
CREATE INDEX IF NOT EXISTS idx_ce_site ON change_events(site_id, created_at DESC);
-- 幂等唯一键：同一站点、同一类型、同一天只允许一条事件。
-- 没有它时，同一天重复执行 `sgm diff` / 定时任务重试会把同一条告警
-- 反复插入，导致邮件与报告重复推送（已由 QA 探针 E1/E3 实证）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_ce_site_type_date
  ON change_events(site_id, event_type, stat_date);

-- 6) 审计日志 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,                  -- UTC ISO
  actor      TEXT NOT NULL,                  -- cli / web / scheduler / system
  action     TEXT NOT NULL,                  -- site.add / check.run / proxy.disable ...
  target     TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '{}',
  level      TEXT NOT NULL DEFAULT 'info',
  request_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_al_ts     ON audit_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_al_action ON audit_logs(action, ts DESC);

-- 7) 运行批次统计（可观测性 R-020）-------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      TEXT NOT NULL UNIQUE,
  mode         TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  status       TEXT NOT NULL DEFAULT 'running',  -- running/success/partial/failed/cancelled
  total        INTEGER NOT NULL DEFAULT 0,
  indexed      INTEGER NOT NULL DEFAULT 0,
  not_indexed  INTEGER NOT NULL DEFAULT 0,
  unknown      INTEGER NOT NULL DEFAULT 0,
  failed       INTEGER NOT NULL DEFAULT 0,
  concurrency  INTEGER NOT NULL DEFAULT 0,
  proxies_used INTEGER NOT NULL DEFAULT 0,
  duration_ms  INTEGER NOT NULL DEFAULT 0,
  config_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

-- 8) 代理 API 源（API 动态获取，定时拉取 IP 列表填充代理池）------------------
CREATE TABLE IF NOT EXISTS proxy_api_sources (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  name                TEXT    NOT NULL DEFAULT '',
  url                 TEXT    NOT NULL UNIQUE,        -- 返回 IP 列表的接口地址
  protocol            TEXT    NOT NULL DEFAULT 'http', -- 拉取到的代理默认协议
  username            TEXT    NOT NULL DEFAULT '',
  password_enc        TEXT    NOT NULL DEFAULT '',     -- 本地明文（脱敏展示）
  refresh_interval_min INTEGER NOT NULL DEFAULT 30,   -- 自动刷新间隔（分钟）
  rotate_per_check    INTEGER NOT NULL DEFAULT 0,     -- 0=百度风控时换 IP；1=每站换 IP
  enabled             INTEGER NOT NULL DEFAULT 1,
  last_fetch_at       TEXT,
  last_fetch_count    INTEGER NOT NULL DEFAULT 0,
  last_error          TEXT    NOT NULL DEFAULT '',
  created_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pas_enabled ON proxy_api_sources(enabled);

-- schema 版本：使用 user_version PRAGMA（避免额外引入第 9 张表）
PRAGMA user_version = 6;
