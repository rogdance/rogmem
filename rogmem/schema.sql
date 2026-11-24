PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  device_name TEXT NOT NULL,
  program TEXT NOT NULL,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL,
  pubkey TEXT NOT NULL,
  key_hash TEXT,
  created_at TEXT NOT NULL,
  connected_at TEXT,
  call_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  session_id TEXT,
  turn_id TEXT,
  msg_seq INTEGER,
  device_id TEXT,
  device_name TEXT,
  agent_id TEXT,
  agent_name TEXT,
  project_ref TEXT,
  event_at TEXT,
  event_basis TEXT,
  ingested_at TEXT NOT NULL,
  idempotency_key TEXT,
  content_hash TEXT NOT NULL,
  body TEXT NOT NULL,
  body_chars INTEGER NOT NULL DEFAULT 0,
  body_preview TEXT NOT NULL DEFAULT '',
  excerpt TEXT NOT NULL DEFAULT '',
  redacted INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  lane TEXT NOT NULL,
  pair_ready INTEGER NOT NULL DEFAULT 0,
  wait_until TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  filename TEXT,
  coverage TEXT,
  residual TEXT,
  pipeline TEXT,
  stage_json TEXT
);

CREATE TABLE IF NOT EXISTS source_cards (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT '',
  filename TEXT NOT NULL DEFAULT '',
  ingested_at TEXT NOT NULL DEFAULT '',
  pipeline TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  body_chars INTEGER NOT NULL DEFAULT 0,
  body_preview TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS source_openings (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT '',
  event_at TEXT NOT NULL DEFAULT '',
  project_ref TEXT NOT NULL DEFAULT '',
  excerpt TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS source_openings_recent
ON source_openings(event_at, id);

CREATE TABLE IF NOT EXISTS crystal_openings (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  sketch TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  repo TEXT NOT NULL DEFAULT ''
);

-- Cross-page standing sketches (question → answer). core reads these directly.
CREATE TABLE IF NOT EXISTS standing_sketches (
  id TEXT PRIMARY KEY,
  question TEXT NOT NULL,
  answer_md TEXT NOT NULL DEFAULT '',
  source_crystal_ids TEXT NOT NULL DEFAULT '[]',
  source_fingerprint TEXT NOT NULL DEFAULT '',
  cite_json TEXT NOT NULL DEFAULT '[]',
  refreshed_at TEXT NOT NULL DEFAULT '',
  stale INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS standing_sketches_stale ON standing_sketches(stale);

CREATE UNIQUE INDEX IF NOT EXISTS sources_idem ON sources(idempotency_key);
CREATE INDEX IF NOT EXISTS sources_recent_turn
ON sources(IFNULL(event_at, ''), id, kind, IFNULL(project_ref, ''))
WHERE redacted = 0 AND kind IN ('user', 'agent');
CREATE INDEX IF NOT EXISTS sources_ingested ON sources(ingested_at, id);
-- 文件导入按内容摘要查重、读取线程按状态取队头：两处都在写锁里，不能扫全表。
CREATE INDEX IF NOT EXISTS sources_content_hash ON sources(content_hash);
CREATE INDEX IF NOT EXISTS sources_status_ingested ON sources(status, ingested_at, id);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  loc TEXT,
  text TEXT NOT NULL,
  vec_json TEXT,
  cited INTEGER NOT NULL DEFAULT 0,
  role TEXT NOT NULL DEFAULT 'child',
  parent_id INTEGER,
  embed_text TEXT
);

CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source_id);
-- 还差向量的切片。热轮询每轮都要问一次「谁还缺向量」，没有它就得全表扫。
CREATE INDEX IF NOT EXISTS chunks_missing_vec ON chunks(source_id)
WHERE role != 'parent' AND vec_json IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunk USING fts5(chunk_id UNINDEXED, text, tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_stmt USING fts5(stmt_id UNINDEXED, text, tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_crystal USING fts5(crystal_id UNINDEXED, text, tokenize='unicode61');

CREATE TABLE IF NOT EXISTS crystallizations (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  chapters_json TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  envelope_json TEXT,
  body_md TEXT,
  facts_json TEXT,
  inferences_json TEXT,
  merged_into TEXT,
  maybe_with TEXT,
  possible_duplicate_of TEXT,
  unresolved_conflicts TEXT,
  deferred_retries INTEGER NOT NULL DEFAULT 0,
  lease_holder TEXT,
  lease_until TEXT
);

CREATE TABLE IF NOT EXISTS crystal_events (
  id INTEGER PRIMARY KEY,
  crystal_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS crystal_events_crystal_at ON crystal_events(crystal_id, at);
CREATE INDEX IF NOT EXISTS crystallizations_name ON crystallizations(name, status);

CREATE TABLE IF NOT EXISTS crystal_leases (
  crystal_id TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  until TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT 'integrate'
);

CREATE TABLE IF NOT EXISTS statements (
  id TEXT PRIMARY KEY,
  crystal_id TEXT NOT NULL,
  field TEXT,
  chapter TEXT,
  text TEXT NOT NULL,
  quote TEXT NOT NULL,
  source_id TEXT NOT NULL,
  chunk_seq INTEGER,
  trust TEXT NOT NULL,
  rank TEXT NOT NULL,
  event_at TEXT,
  created_at TEXT NOT NULL,
  invalid_at TEXT,
  home_crystal TEXT,
  valid_until TEXT
);

CREATE TABLE IF NOT EXISTS statement_sources (
  statement_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  quote TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (statement_id, source_id)
);

-- Belief units (Hindsight observation spirit): evidence set above statements.
-- vec_json is near-dup probe only; never enters names_correspond / uniqueness.
CREATE TABLE IF NOT EXISTS beliefs (
  id TEXT PRIMARY KEY,
  crystal_id TEXT NOT NULL,
  place TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  quote TEXT NOT NULL,
  source_id TEXT NOT NULL,
  event_at TEXT,
  created_at TEXT NOT NULL,
  invalid_at TEXT,
  merged_into TEXT,
  vec_json TEXT
);

CREATE TABLE IF NOT EXISTS belief_sources (
  belief_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  statement_id TEXT,
  quote TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (belief_id, source_id)
);

CREATE TABLE IF NOT EXISTS belief_statements (
  belief_id TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  PRIMARY KEY (belief_id, statement_id)
);

CREATE INDEX IF NOT EXISTS beliefs_crystal_place
  ON beliefs(crystal_id, place)
  WHERE invalid_at IS NULL AND IFNULL(merged_into, '') = '';

CREATE TABLE IF NOT EXISTS edges (
  id TEXT PRIMARY KEY,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  kind TEXT NOT NULL,
  statement_id TEXT NOT NULL,
  ended INTEGER NOT NULL DEFAULT 0,
  home_src TEXT,
  home_dst TEXT
);

CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  lane TEXT NOT NULL,
  state TEXT NOT NULL,
  next_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  cursor INTEGER NOT NULL DEFAULT 0,
  leased_until TEXT,
  superseded INTEGER NOT NULL DEFAULT 0,
  enqueued_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  finished_jd REAL
);

CREATE TABLE IF NOT EXISTS revisions (
  id INTEGER PRIMARY KEY,
  crystal_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL,
  source_id TEXT,
  quote TEXT,
  event_at TEXT,
  statement_id TEXT,
  actor TEXT,
  change TEXT,
  basis TEXT,
  snapshot TEXT,
  page_json TEXT,
  scope TEXT,
  gist TEXT
);

CREATE INDEX IF NOT EXISTS revisions_source ON revisions(source_id);
CREATE INDEX IF NOT EXISTS revisions_crystal ON revisions(crystal_id, id);
CREATE INDEX IF NOT EXISTS statements_source ON statements(source_id);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  message TEXT NOT NULL,
  open INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS hosts (
  program TEXT PRIMARY KEY,
  found INTEGER NOT NULL,
  access TEXT NOT NULL,
  health TEXT NOT NULL,
  detail TEXT,
  reported_at TEXT NOT NULL,
  importable INTEGER
);

CREATE TABLE IF NOT EXISTS host_confirms (
  program TEXT PRIMARY KEY,
  confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS host_imports (
  program TEXT PRIMARY KEY,
  confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS host_jobs (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  program TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'history_import',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  claimed_at TEXT,
  lease_until TEXT,
  lease_token TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  posted INTEGER NOT NULL DEFAULT 0,
  duplicates INTEGER NOT NULL DEFAULT 0,
  refused INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  last_error_class TEXT,
  completed_at TEXT,
  cancelled_at TEXT,
  requested_by TEXT NOT NULL DEFAULT 'admin',
  UNIQUE(agent_id, request_id)
);
CREATE INDEX IF NOT EXISTS host_jobs_by_agent_status ON host_jobs(agent_id, status);
CREATE INDEX IF NOT EXISTS host_jobs_by_device ON host_jobs(device_id, created_at DESC);
-- 维护时钟每一圈都要「到期的 pending」「过期的租约」「到次数的 pending」各扫一遍。
-- 没有这两条，每一圈都是整表扫，而这一圈每 0.2 秒就转一次。
CREATE INDEX IF NOT EXISTS host_jobs_due ON host_jobs(status, expires_at);
CREATE INDEX IF NOT EXISTS host_jobs_lease ON host_jobs(status, lease_until);
CREATE INDEX IF NOT EXISTS host_jobs_attempts ON host_jobs(status, max_attempts);
-- R4-4 / R5 A2：跨进程保险。同 (agent_id, program) 只许一条 pending/claimed/running/partial。
-- 此索引的创建放在 engine.connect() 的迁移路径中执行（先规范化老库旧 active 重复，
-- 再 CREATE UNIQUE INDEX IF NOT EXISTS），避免 schema.sql executescript 阶段
-- 老库启动即 IntegrityError。

-- 导入对账账本：以幂等键（session:seq:content_hash）为单位记一台电脑一个宿主的终态。
-- 「待导入会话数」不再用「本机扫描总数 − 中枢累计录入数」这种跨口径的减法，
-- 而是用「本机扫描总数 − 已确认在库 − 已确认无法导入」。
-- stored / duplicate 都算「已确认在库」，refused 算「已确认无法导入」；三者都是终态，
-- 重复点击导入不会把它们又算回待导入。同一条键只记一次，跨设备互不串账。
CREATE TABLE IF NOT EXISTS host_import_receipts (
  device_id TEXT NOT NULL,
  program TEXT NOT NULL,
  idem TEXT NOT NULL,
  outcome TEXT NOT NULL,          -- stored | duplicate | refused
  source_id TEXT,
  job_id TEXT NOT NULL DEFAULT '',
  seen_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (device_id, program, idem)
);
CREATE INDEX IF NOT EXISTS host_import_receipts_by_host
  ON host_import_receipts(device_id, program, outcome);

CREATE TABLE IF NOT EXISTS enroll_grants (
  id TEXT PRIMARY KEY,
  secret_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  device_id TEXT NOT NULL DEFAULT '',
  device_name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS enroll_grant_redemptions (
  grant_id TEXT NOT NULL,
  program TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  redeemed_at TEXT NOT NULL,
  wrapped TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (grant_id, program)
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  step TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL,
  subject TEXT NOT NULL,
  summary TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  ref_kind TEXT NOT NULL DEFAULT '',
  ref_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS run_log_time ON run_log(created_at, id);

CREATE TABLE IF NOT EXISTS provider_health (
  id INTEGER PRIMARY KEY,
  provider_id TEXT NOT NULL,
  at TEXT NOT NULL,
  health TEXT NOT NULL,
  probed_jd REAL
);

CREATE INDEX IF NOT EXISTS provider_health_provider_at ON provider_health(provider_id, at);

-- 萃取前分诊：每个送去萃取或被跳过的单元留一行指纹。decision 是 pending / distilled / skipped，
-- pending 只在这一次萃取真正收尾后才改成 distilled，失败的尝试不会冒充「已萃取过」。
CREATE TABLE IF NOT EXISTS distill_fingerprints (
  source_id TEXT PRIMARY KEY,
  unit_hash TEXT NOT NULL,
  template_key TEXT NOT NULL DEFAULT '',
  agent_sample TEXT NOT NULL DEFAULT '',
  decision TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  unit_at TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL
);
-- 重页（记忆主人，以及很长的页）不在每次交流时并页：草稿先停在这里，攒批后一次并完。
-- 分节并页仍写不下的草稿也停在这里，页不动、不拼接。consumed_at 为空就是还没并进去。
CREATE TABLE IF NOT EXISTS page_backlog (
  id INTEGER PRIMARY KEY,
  crystal_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  draft_md TEXT NOT NULL,
  material_md TEXT NOT NULL DEFAULT '',
  event_at TEXT NOT NULL DEFAULT '',
  event_basis TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL,
  consumed_at TEXT,
  tries INTEGER NOT NULL DEFAULT 0,
  tried_at TEXT,
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS page_backlog_open ON page_backlog(crystal_id, consumed_at, id);
CREATE INDEX IF NOT EXISTS page_backlog_source ON page_backlog(source_id);
CREATE INDEX IF NOT EXISTS distill_fingerprints_hash ON distill_fingerprints(unit_hash, decision);
CREATE INDEX IF NOT EXISTS distill_fingerprints_template ON distill_fingerprints(template_key, decision);
CREATE INDEX IF NOT EXISTS statements_crystal ON statements(crystal_id, invalid_at);
CREATE INDEX IF NOT EXISTS edges_mention ON edges(src, dst) WHERE ended=0 AND kind='提及';
CREATE INDEX IF NOT EXISTS edges_mention_dst ON edges(dst, src) WHERE ended=0 AND kind='提及';
CREATE INDEX IF NOT EXISTS edges_live_other ON edges(statement_id, src, dst, kind) WHERE ended=0 AND kind!='提及';
CREATE INDEX IF NOT EXISTS queue_other_state ON queue(id) WHERE state NOT IN ('queued', 'leased', 'failed', 'done');
-- 调度每一轮只看队列的两头：等最久的一段和新到的一段。少了它，等待中的整条堆积
-- （生产上三万六千行）每一轮都要连正文一起装进内存，页缓存一冷就是几分钟。
CREATE INDEX IF NOT EXISTS queue_waiting ON queue(state, id);
-- 租约回收只碰少数几行。走索引之后它不再每一轮全表扫 queue。
CREATE INDEX IF NOT EXISTS queue_leased ON queue(state, superseded, leased_until);
-- 到期的 failed 行按 next_at 复位，每轮只取一段，不整批改写。
CREATE INDEX IF NOT EXISTS queue_due ON queue(state, next_at, id);
