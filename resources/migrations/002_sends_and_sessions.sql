-- 防重复抽取与主动推送。

-- 每条记录最近一次被随机发送的时间；记录删除时一并清理，不进入备份。
CREATE TABLE record_sends (
  record_id TEXT PRIMARY KEY REFERENCES records (id) ON DELETE CASCADE,
  group_id TEXT NOT NULL,
  last_sent_at REAL NOT NULL,
  send_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_record_sends_recent ON record_sends (group_id, last_sent_at);

-- 定时推送需要的群会话；没有事件上下文时据此找回平台实例与机器人账号。
CREATE TABLE group_sessions (
  group_id TEXT PRIMARY KEY,
  umo TEXT NOT NULL,
  platform_id TEXT NOT NULL,
  self_id TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
