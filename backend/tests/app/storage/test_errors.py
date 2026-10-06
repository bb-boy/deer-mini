import asyncio
import errno
import sqlite3

import pytest


@pytest.mark.parametrize("code,category,recovery", [
    (errno.ENOENT, "not_found", "manual"),
    (errno.ENOSPC, "no_space", "wait"),
    (errno.EACCES, "permission", "wait"),
    (errno.EBUSY, "busy", "retry"),
    (errno.EIO, "io", "wait"),
])
def test_os_classification_never_exposes_exception_text(code, category, recovery):
    from app.storage.errors import classify_os_error
    source = OSError(code, "private content", "/private/user/path")
    error = classify_os_error(source, operation="write", stage="open")
    assert error.safe_fields() == dict(backend="file", operation="write", category=category,
                                      stage="open", commit_state="not_committed", recovery=recovery)
    assert "private" not in str(error)
    assert error.__cause__ is source


def test_uncertain_commit_requires_verification_even_when_environment_recovers():
    from app.storage.errors import classify_os_error
    error = classify_os_error(OSError(errno.ENOSPC, "full"), operation="write",
                              stage="directory_sync", commit_state="uncertain")
    assert error.recovery == "verify"


@pytest.mark.parametrize("code,category", [
    (sqlite3.SQLITE_BUSY, "busy"), (sqlite3.SQLITE_FULL, "no_space"),
    (sqlite3.SQLITE_CORRUPT, "corrupt"), (sqlite3.SQLITE_IOERR, "io"),
])
def test_sqlite_classification_uses_numeric_code(code, category):
    from app.storage.errors import classify_sqlite_error
    source = sqlite3.OperationalError("private sql values")
    source.sqlite_errorcode = code
    error = classify_sqlite_error(source, operation="write", stage="execute")
    assert error.backend == "sqlite" and error.category == category
    assert error.__cause__ is source and "private" not in str(error)
