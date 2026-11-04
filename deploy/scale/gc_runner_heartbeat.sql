-- Heartbeat row per gc_runner shard (server/hub/tools/gc_runner.py, 2026-11-04).
-- The runner itself runs the CREATE TABLE IF NOT EXISTS on start (the paprika
-- user owns the schema); this file is the reference DDL plus the one-off GRANT
-- the pipeline control plane (delian user, pipeline/api/flow.py
-- _gc_runner_alerts) needs to read it. All times are UTC: MariaDB NOW() on
-- 10.10.50.20 is JST, so never compare these columns against NOW().
--
--   mysql -h 10.10.50.20 -u root -p paprika < deploy/scale/gc_runner_heartbeat.sql
CREATE TABLE IF NOT EXISTS paprika.gc_runner_heartbeat (
  shard        TINYINT NOT NULL,
  shards_total TINYINT NOT NULL,
  hub_id       VARCHAR(32) NOT NULL,
  state        ENUM('starting','pass','idle','fail_closed') NOT NULL,
  state_since  DATETIME NOT NULL,      -- UTC, when `state` last changed
  last_pass_at DATETIME NULL,          -- UTC, last pass that deleted >= 1 row
  beat_at      DATETIME NOT NULL,      -- UTC, last write of any kind
  rows_total   BIGINT NOT NULL DEFAULT 0,
  rate_per_min INT NOT NULL DEFAULT 0,
  cutoff_at    DATETIME NULL,          -- UTC, cutoff the last pass actually used
  PRIMARY KEY (shard)
) ENGINE=InnoDB;

GRANT SELECT ON paprika.gc_runner_heartbeat TO 'delian'@'%';
