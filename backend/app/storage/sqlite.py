"""保留 Repository 的 sqlite3 接口，集中区分语句执行与提交失败，不隐式重试。"""

from collections.abc import Iterable
import sqlite3
from types import TracebackType
from typing import Any

from app.storage.errors import classify_sqlite_error


class StorageCursor(sqlite3.Cursor):
    def execute(self, sql: str, parameters: Any = ()) -> "StorageCursor":
        is_commit = sql.strip().split(maxsplit=1)[0].rstrip(";").upper() in {"COMMIT", "END"} if sql.strip() else False
        try:
            return super().execute(sql, parameters)
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as error:
            raise classify_sqlite_error(error, operation="commit" if is_commit else "execute",
                                        stage="commit" if is_commit else "execute",
                                        commit_state="uncertain" if is_commit else "not_committed") from error

    def executemany(self, sql: str, seq_of_parameters: Iterable[Any]) -> "StorageCursor":
        try:
            return super().executemany(sql, seq_of_parameters)
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as error:
            raise classify_sqlite_error(error, operation="execute", stage="execute") from error

    def executescript(self, sql_script: str) -> "StorageCursor":
        try:
            return super().executescript(sql_script)
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as error:
            # executescript 会先提交已有事务，且脚本可含多个独立提交。
            raise classify_sqlite_error(error, operation="execute", stage="execute_script",
                                        commit_state="uncertain") from error

    def fetchone(self) -> Any:
        try:
            return super().fetchone()
        except sqlite3.Error as error:
            raise classify_sqlite_error(error, operation="read", stage="read") from error

    def fetchmany(self, size: int | None = None) -> list[Any]:
        try:
            return super().fetchmany(self.arraysize if size is None else size)
        except sqlite3.Error as error:
            raise classify_sqlite_error(error, operation="read", stage="read") from error

    def fetchall(self) -> list[Any]:
        try:
            return super().fetchall()
        except sqlite3.Error as error:
            raise classify_sqlite_error(error, operation="read", stage="read") from error

    def __next__(self) -> Any:
        try:
            return super().__next__()
        except sqlite3.Error as error:
            raise classify_sqlite_error(error, operation="read", stage="read") from error


class StorageConnection(sqlite3.Connection):
    def cursor(self, factory: Any = StorageCursor) -> sqlite3.Cursor:
        try:
            return super().cursor(factory)
        except sqlite3.Error as error:
            raise classify_sqlite_error(error, operation="execute", stage="execute") from error

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Iterable[Any]) -> sqlite3.Cursor:
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        return self.cursor().executescript(sql_script)

    def commit(self) -> None:
        try:
            super().commit()
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as error:
            raise classify_sqlite_error(error, operation="commit", stage="commit",
                                        commit_state="uncertain") from error

    def rollback(self) -> None:
        try:
            super().rollback()
        except sqlite3.Error as error:
            raise classify_sqlite_error(error, operation="rollback", stage="rollback",
                                        commit_state="uncertain") from error

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> bool:
        if exc_type is not None:
            try:
                super().rollback()
            except sqlite3.Error:
                # 不能让清理异常覆盖取消、关键持久化失败或原始业务异常。
                pass
            return False
        try:
            self.commit()
        except BaseException:
            try:
                super().rollback()
            except sqlite3.Error:
                pass
            raise
        return False
