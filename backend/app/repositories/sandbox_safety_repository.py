"""持久化容器停止证明；缺少 Docker CLI 时不能把崩溃后的活跃容器当作不存在。"""
from app.infrastructure.database import connect


class SandboxSafetyRepository:
    def _set(self, value: str) -> None:
        with connect() as conn:
            conn.execute("INSERT INTO runtime_flags(name,value) VALUES('sandbox_state',?) "
                         'ON CONFLICT(name) DO UPDATE SET value=excluded.value',(value,))

    def mark_active(self) -> None:
        """在启用容器会话之前持久化；进程异常退出时保留 active。"""
        self._set('active')

    def mark_clear(self) -> None:
        """仅在确认本项目所有容器已经停止后记录。"""
        self._set('clear')

    def require_clear_without_docker(self) -> None:
        with connect() as conn:
            row=conn.execute("SELECT value FROM runtime_flags WHERE name='sandbox_state'").fetchone()
            if row is not None:
                unsafe=row['value']!='clear'
            else:
                # 升级旧库时，模型请求 Bash 已先写入 execution checkpoint；也检查历史事件。
                unsafe=bool(conn.execute("""
                    SELECT EXISTS(SELECT 1 FROM checkpoints c,json_tree(c.state_json) j
                                  WHERE j.key='name' AND j.value='bash')
                        OR EXISTS(SELECT 1 FROM run_events e,json_tree(e.payload_json) j
                                  WHERE j.key='tool_name' AND j.value='bash')
                """).fetchone()[0])
        if unsafe:
            raise RuntimeError('无法确认遗留 Bash 容器已停止，请恢复 Docker 访问后重新启动')
