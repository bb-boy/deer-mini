"""
使用python自带的sqlite3模块来操作sqlite数据库
实现connect()：打开 backend/data/deer_mini.db，并开启 SQLite 外键约束；
initialize_database()：创建 threads、runs、checkpoints、run_events 四张表。
"""

import sqlite3
import os
from pathlib import Path

from app.storage.errors import classify_os_error, classify_sqlite_error
from app.storage.sqlite import StorageConnection
from app.memory.task_schema import SCHEMA as MEMORY_TASK_SCHEMA

DATABASE_PATH = Path(__file__).resolve().parents[2] / "data" / "deer_mini.db" #resolve()返回绝对路径，parents[2]返回到第三个父目录。__file__是当前文件的路径，PATH把她变为一个PATH对象方便操作，


def resolve_database_path() -> Path:
    """返回本进程使用的 SQLite 文件；环境变量只接受绝对路径。"""
    configured = os.getenv("DEER_MINI_DATABASE_PATH")
    if not configured:
        return DATABASE_PATH
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError("DEER_MINI_DATABASE_PATH 必须是绝对路径")
    return path.resolve()


def connect() ->sqlite3.Connection:
    """
    打开 backend/data/deer_mini.db，并开启 SQLite 外键约束
    :return: sqlite3.Connection
    """

    database_path = resolve_database_path()
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise classify_os_error(error, operation="open", stage="open") from error
    try:
        conn = sqlite3.connect(database_path, factory=StorageConnection)
    except sqlite3.Error as error:
        raise classify_sqlite_error(error, operation="open", stage="open") from error
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
    except BaseException:
        conn.close()
        raise
    return conn





def initialize_database() -> None:
    """
    创建 threads、runs、checkpoints、run_events 四张表
    :return: None
    """
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT,
                workspace_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('idle', 'running')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'success', 'error', 'interrupted', 'timeout')),
                model_name TEXT,
                thinking_enabled INTEGER NOT NULL,
                reasoning_effort TEXT,
                error TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                thread_id TEXT NOT NULL,
                run_id TEXT NOT NULL, 
                step INTEGER NOT NULL, 
                state_json TEXT NOT NULL, 
                created_at TEXT NOT NULL, 
                UNIQUE(thread_id, run_id, step),
                FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            );



           
            CREATE TABLE IF NOT EXISTS run_events (
                id TEXT PRIMARY KEY, 
                run_id TEXT NOT NULL, 
                thread_id TEXT NOT NULL, 
                sequence INTEGER NOT NULL, 
                event_type TEXT NOT NULL,  
                payload_json TEXT NOT NULL, 
                created_at TEXT NOT NULL,
                UNIQUE(run_id, sequence),
                FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            );


            CREATE INDEX IF NOT EXISTS idx_runs_thread_id ON runs(thread_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_thread_active
            ON runs(thread_id)
            WHERE status IN ('pending', 'running');
            CREATE INDEX IF NOT EXISTS idx_checkpoints_thread_id ON checkpoints(thread_id);
            CREATE INDEX IF NOT EXISTS idx_checkpoints_run_id ON checkpoints(run_id);
            CREATE INDEX IF NOT EXISTS idx_run_events_thread_id ON run_events(thread_id);
            """
        )

        # 旧数据库中的状态默认为 execution；迁移只补缺失指针，不覆盖已有恢复。
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(checkpoints)")}
        if "kind" not in columns:
            conn.execute("ALTER TABLE checkpoints ADD COLUMN kind TEXT NOT NULL DEFAULT 'execution'")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runtime_flags (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thread_heads (
                thread_id TEXT PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE,
                checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id),
                revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_snapshots (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                manifest_json TEXT NOT NULL,
                total_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_restore_points (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
                state_checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id),
                file_snapshot_id TEXT NOT NULL REFERENCES file_snapshots(id),
                kind TEXT NOT NULL CHECK(kind IN ('turn_start','recovery')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_file_points_thread ON file_restore_points(thread_id);
            CREATE TABLE IF NOT EXISTS file_restore_operations (
                operation_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                restore_point_id TEXT NOT NULL,
                recovery_point_id TEXT NOT NULL,
                expected_revision INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('prepared','applying','committed','rolled_back','needs_recovery')),
                error TEXT,
                cleaned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_file_operations_thread ON file_restore_operations(thread_id);
        """)
        conn.execute("""
            INSERT OR IGNORE INTO thread_heads(thread_id, checkpoint_id, revision)
            SELECT c.thread_id,c.id,1 FROM checkpoints c
            WHERE c.id=(SELECT c2.id FROM checkpoints c2 WHERE c2.thread_id=c.thread_id
                        ORDER BY c2.created_at DESC,c2.id DESC LIMIT 1)
        """)
        conn.executescript(MEMORY_TASK_SCHEMA)
