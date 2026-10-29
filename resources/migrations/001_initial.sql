-- 群典 2.0 初始结构。迁移脚本发布后不得修改，结构变更新增 002_*.sql。

CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 一条群典一行；payload 保存完整消息树，其余列服务于查询与统计。
CREATE TABLE records (
  id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('message', 'forward')),
  content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
  author_id TEXT,
  recorded_at TEXT NOT NULL,
  search_text TEXT NOT NULL,
  has_media INTEGER NOT NULL CHECK (has_media IN (0, 1)),
  payload TEXT NOT NULL CHECK (json_valid(payload)),
  UNIQUE (group_id, content_hash)
);
CREATE INDEX ix_records_group_time ON records (group_id, recorded_at);

-- 记录内出现的 QQ：involved 表示普通作者或转发节点作者（个人随机池与爆典），
-- authored_text 为该用户本人正文（规范化后），用于“@某人 + 关键词”。
CREATE TABLE record_participants (
  record_id TEXT NOT NULL REFERENCES records (id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  involved INTEGER NOT NULL CHECK (involved IN (0, 1)),
  authored_text TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (record_id, user_id)
);
CREATE INDEX ix_participants_user ON record_participants (user_id);

-- 转发节点与回复快照作者的规范化昵称：读取时的别名与唯一昵称纠正据此扩展候选。
CREATE TABLE record_names (
  record_id TEXT NOT NULL REFERENCES records (id) ON DELETE CASCADE,
  name_norm TEXT NOT NULL,
  PRIMARY KEY (record_id, name_norm)
);
CREATE INDEX ix_record_names_name ON record_names (name_norm);

CREATE TABLE media (
  sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
  mime TEXT NOT NULL,
  size INTEGER NOT NULL CHECK (size > 0),
  created_at TEXT NOT NULL,
  verified_at TEXT,
  missing INTEGER NOT NULL DEFAULT 0 CHECK (missing IN (0, 1))
);

CREATE TABLE record_media (
  record_id TEXT NOT NULL REFERENCES records (id) ON DELETE CASCADE,
  sha256 TEXT NOT NULL REFERENCES media (sha256),
  PRIMARY KEY (record_id, sha256)
);
CREATE INDEX ix_record_media_sha ON record_media (sha256);

CREATE TABLE avatars (
  user_id TEXT PRIMARY KEY,
  fetched_at REAL NOT NULL,
  last_used REAL NOT NULL,
  size INTEGER NOT NULL CHECK (size >= 0)
);

-- 删除审计不保存被删正文。
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  deleted_by TEXT NOT NULL,
  deleted_at TEXT NOT NULL,
  source TEXT NOT NULL,
  node_path TEXT,
  removed_nodes INTEGER,
  record_deleted INTEGER
);

CREATE TABLE group_overrides (
  group_id TEXT PRIMARY KEY,
  value TEXT NOT NULL CHECK (json_valid(value)),
  updated_at TEXT NOT NULL
);

CREATE TABLE author_aliases (
  group_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  alias_norm TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY (group_id, alias_norm)
);
