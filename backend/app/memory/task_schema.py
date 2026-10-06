"""记忆保存任务表：与文件分别提交，来源标识在 Thread 删除后仍保留。"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_save_tasks (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    source_thread_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    window INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS memory_save_items (
    operation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES memory_save_tasks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    memory_id TEXT NOT NULL,
    expected_version TEXT,
    result_digest TEXT NOT NULL,
    payload_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    phase TEXT NOT NULL DEFAULT 'registered',
    next_attempt_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_json TEXT
);
CREATE TABLE IF NOT EXISTS memory_save_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES memory_save_tasks(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    window INTEGER NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    error_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_tasks_user ON memory_save_tasks(user_id, registered_at);
CREATE INDEX IF NOT EXISTS idx_memory_tasks_source ON memory_save_tasks(source_thread_id, user_id);
CREATE INDEX IF NOT EXISTS idx_memory_items_due ON memory_save_items(status, next_attempt_at);
"""
