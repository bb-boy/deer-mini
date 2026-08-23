"""
使用python自带的sqlite3模块来操作sqlite数据库
实现connect()：打开 backend/data/deer_mini.db，并开启 SQLite 外键约束；
initialize_database()：创建 threads、runs、checkpoints、run_events 四张表。
"""

import sqlite3
from pathlib import Path

DATABASE_PATH = Path(__file__).resolve().parents[2] / "data" / "deer_mini.db" #resolve()返回绝对路径，parents[2]返回到第三个父目录。__file__是当前文件的路径，PATH把她变为一个PATH对象方便操作，


def connect() ->sqlite3.Connection:
    """
    打开 backend/data/deer_mini.db，并开启 SQLite 外键约束
    :return: sqlite3.Connection
    """

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)  # 确保 data 文件夹存在
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row  # 设置行工厂，默认是元组返回，设置行工厂就会可以通过row["name"]来访问
    conn.execute("PRAGMA foreign_keys = ON;") #打开外键约束检查
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

