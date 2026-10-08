"""Native file tools: behavior, isolation, bounded work and durable writes."""
import asyncio
import importlib
import json
import os
import threading

import pytest

from app.domain.messages import ToolCall
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.storage.errors import StorageError


@pytest.fixture
def thread(tmp_path):
    for area in ("workspace", "uploads", "outputs"):
        (tmp_path / area).mkdir()
    async def noop(*args):
        return None
    return tmp_path, RuntimeContext("u", "t", "r", str(tmp_path / "workspace"), noop, noop)


def tool(name):
    module = importlib.import_module("app.tools." + name)
    return getattr(module, {"glob": "GlobTool", "grep": "GrepTool", "write_file": "WriteFileTool",
                           "edit_file": "EditFileTool", "read_file_rewrite": "ReadFileTool"}[name])()


async def execute(name, context, **args):
    instance = tool(name)
    return await instance.execute(ToolCall("call", instance.definition.name, args), context)


def run(name, thread, **args):
    return asyncio.run(execute(name, thread[1], **args))


def data(result):
    assert not result.is_error, result.content
    return json.loads(result.content)


@pytest.mark.parametrize("area", ["workspace", "uploads", "outputs"])
def test_write_create_overwrite_and_virtual_paths(thread, area):
    root, _ = thread
    path = f"/mnt/user-data/{area}/nested/hello.txt"
    result = data(run("write_file", thread, path=path, content="你好\r\n"))
    assert result["path"] == f"{area}/nested/hello.txt"
    assert result["bytes_written"] == len("你好\r\n".encode())
    assert (root / area / "nested/hello.txt").read_bytes() == "你好\r\n".encode()
    assert run("write_file", thread, path=path, content="bad").is_error
    assert data(run("write_file", thread, path=path, content="new", overwrite=True))
    assert (root / area / "nested/hello.txt").read_text() == "new"


@pytest.mark.parametrize("encoding", ["utf-8", "gb18030", "utf-16"])
def test_edit_unique_all_and_encoding_newlines(thread, encoding):
    root, _ = thread
    target = root / "workspace/test.txt"
    original = "甲\r\n乙 甲\r\n"
    target.write_bytes(original.encode(encoding))
    assert run("edit_file", thread, path="test.txt", old_string="甲", new_string="丙", encoding=encoding).is_error
    assert target.read_bytes() == original.encode(encoding)
    assert run("edit_file", thread, path="test.txt", old_string="missing", new_string="丙", encoding=encoding).is_error
    assert target.read_bytes() == original.encode(encoding)
    result = data(run("edit_file", thread, path="test.txt", old_string="甲", new_string="丙",
                      replace_all=True, encoding=encoding))
    assert result["replacements"] == 2
    assert "--- workspace/test.txt" in result["diff"]
    assert target.read_bytes() == original.replace("甲", "丙").encode(encoding)


def test_edit_preserves_utf16_big_endian(thread):
    target = thread[0] / "workspace/be.txt"
    target.write_bytes(b"\xfe\xff" + "甲\r\n乙".encode("utf-16-be"))
    data(run("edit_file", thread, path="be.txt", old_string="甲", new_string="丙", encoding="utf-16"))
    assert target.read_bytes() == b"\xfe\xff" + "丙\r\n乙".encode("utf-16-be")


@pytest.mark.parametrize("name,args", [
    ("glob", {"pattern": ""}), ("glob", {"pattern": "*", "limit": True}),
    ("glob", {"pattern": "../*"}), ("grep", {"pattern": "x", "context_lines": 6}),
    ("grep", {"pattern": "x" * 4097}), ("grep", {"pattern": "x", "literal": 1}),
    ("write_file", {"path": " ", "content": ""}),
    ("write_file", {"path": "a", "content": "", "overwrite": 1}),
    ("edit_file", {"path": "a", "old_string": "", "new_string": ""}),
    ("edit_file", {"path": "a", "old_string": "a", "new_string": "", "extra": 1}),
])
def test_strict_arguments(thread, name, args):
    assert run(name, thread, **args).is_error


@pytest.mark.parametrize("path", ["/etc/passwd", "../other/file", "workspace/../outputs/x",
                                   "workspace/.tool-results/x", "workspace/.storage-x.tmp"])
@pytest.mark.parametrize("name,args", [
    ("glob", {"pattern": "*"}), ("grep", {"pattern": "x"}),
    ("write_file", {"content": "x"}), ("edit_file", {"old_string": "x", "new_string": "y"}),
    ("read_file_rewrite", {}),
])
def test_paths_cannot_escape_or_expose_internal_files(thread, name, args, path):
    result = run(name, thread, path=path, **args)
    assert result.is_error
    assert str(thread[0]) not in result.content


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory_symlink"])
def test_unsafe_files_cannot_be_read_or_mutated(thread, kind):
    root, _ = thread
    outside = root / "outside.txt"
    outside.write_text("secret")
    target = root / "workspace/unsafe"
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.symlink_to(root, target_is_directory=True)
    for name, args in [("read_file_rewrite", {}), ("grep", {"pattern": "secret"}),
                       ("write_file", {"content": "bad", "overwrite": True}),
                       ("edit_file", {"old_string": "secret", "new_string": "bad"})]:
        result = run(name, thread, path="workspace/unsafe", **args)
        assert result.is_error, (kind, name, result.content)
        assert str(root) not in result.content
    assert outside.read_text() == "secret"


def test_glob_recursion_hidden_and_skipped(thread):
    root, _ = thread
    workspace = root / "workspace"
    (workspace / "top.py").write_text("x")
    (workspace / ".hidden.py").write_text("x")
    (workspace / "sub").mkdir()
    (workspace / "sub/child.py").write_text("x")
    for folder in (".git", ".venv", "node_modules", "__pycache__", ".tool-results"):
        (workspace / folder).mkdir()
        (workspace / folder / "skip.py").write_text("secret")
    (workspace / ".storage-a.tmp").write_text("secret")
    (workspace / "link.py").symlink_to(workspace / "top.py")
    result = data(run("glob", thread, pattern="**/*.py"))
    assert result["files"] == ["workspace/.hidden.py", "workspace/sub/child.py", "workspace/top.py"]
    assert result["skipped"]
    assert result["truncated"] is False
    assert data(run("glob", thread, pattern="**/*.py", limit=1))["truncated"]


def test_grep_literal_context_case_and_no_match(thread):
    target = thread[0] / "workspace/test.txt"
    target.write_text("first\nA+B\nthird\nlast\n", encoding="utf-8")
    result = data(run("grep", thread, pattern="a+b", literal=True, case_sensitive=False, context_lines=1))
    assert result["matches"] == [{
        "path": "workspace/test.txt", "line": 2, "text": "A+B",
        "context": [{"line": 1, "text": "first"}, {"line": 3, "text": "third"}],
    }]
    assert not result["partial"]
    assert data(run("grep", thread, pattern="missing"))["matches"] == []
    assert run("grep", thread, pattern="[").is_error


def test_grep_reports_binary_encoding_limits_and_regex_timeout(thread):
    workspace = thread[0] / "workspace"
    (workspace / "binary").write_bytes(b"hello\x00secret")
    (workspace / "encoding").write_bytes(b"\xffsecret")
    (workspace / "large").write_bytes(b"x" * (1024 * 1024 + 1))
    (workspace / "regex").write_text("a" * 100000 + "!")
    result = data(run("grep", thread, pattern="(a+)+$"))
    assert result["partial"]
    reasons = {item["reason"] for item in result["skipped"]}
    assert {"binary", "encoding", "file_size_limit", "regex_timeout"} <= reasons


def test_io_limits_and_read_numbered_lines(thread):
    target = thread[0] / "workspace/test.txt"
    target.write_bytes(b"a\r\nb\r\nc")
    result = run("read_file_rewrite", thread, path="test.txt", base=2, offset=1, line_numbers=True)
    assert not result.is_error
    assert result.content == "2: b\n"
    assert run("read_file_rewrite", thread, path="test.txt", line_numbers=1).is_error
    target.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    assert run("read_file_rewrite", thread, path="test.txt").is_error
    assert run("edit_file", thread, path="test.txt", old_string="x", new_string="y").is_error
    assert run("write_file", thread, path="too-big", content="x" * (8 * 1024 * 1024 + 1)).is_error
    assert not (thread[0] / "workspace/too-big").exists()


def test_two_create_writes_cannot_both_succeed(thread):
    async def scenario():
        return await asyncio.gather(*[
            execute("write_file", thread[1], path="race.txt", content=str(index))
            for index in range(8)
        ])
    results = asyncio.run(scenario())
    assert sum(not item.is_error for item in results) == 1


def test_concurrent_edits_preserve_all_updates(thread):
    target = thread[0] / "workspace/race.txt"
    target.write_text("".join(f"[{index}]" for index in range(20)))
    async def scenario():
        return await asyncio.gather(*[
            execute("edit_file", thread[1], path="race.txt", old_string=f"[{index}]", new_string=f"<{index}>")
            for index in range(20)
        ])
    assert all(not item.is_error for item in asyncio.run(scenario()))
    assert target.read_text() == "".join(f"<{index}>" for index in range(20))


def test_cancellation_waits_for_inflight_write(thread, monkeypatch):
    from app.storage.file_io import AtomicWriter
    started, release = threading.Event(), threading.Event()
    commit = AtomicWriter.commit
    def slow_commit(self):
        started.set()
        assert release.wait(3)
        commit(self)
    monkeypatch.setattr(AtomicWriter, "commit", slow_commit)
    async def scenario():
        task = asyncio.create_task(execute("write_file", thread[1], path="cancel.txt", content="done"))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert (thread[0] / "workspace/cancel.txt").read_text() == "done"


@pytest.mark.parametrize("state", ["committed", "uncertain"])
def test_post_commit_failures_propagate(thread, monkeypatch, state):
    from app.storage.file_io import AtomicWriter
    commit = AtomicWriter.commit
    def failed(self):
        commit(self)
        raise StorageError(backend="file", operation="write", category="io",
                           stage="directory_sync", commit_state=state)
    monkeypatch.setattr(AtomicWriter, "commit", failed)
    with pytest.raises(StatePersistenceError):
        run("write_file", thread, path="failed.txt", content="done")
    assert (thread[0] / "workspace/failed.txt").read_text() == "done"


def test_precommit_failures_are_tool_errors_and_preserve_original(thread, monkeypatch):
    from app.storage.file_io import AtomicWriter
    target = thread[0] / "workspace/a"
    target.write_text("old")
    def failed(self, content):
        raise StorageError(backend="file", operation="write", category="no_space", stage="write")
    monkeypatch.setattr(AtomicWriter, "write", failed)
    assert run("write_file", thread, path="a", content="new", overwrite=True).is_error
    assert target.read_text() == "old"
    assert sorted(path.name for path in target.parent.iterdir()) == ["a"]



def test_read_preserves_textio_line_selection(thread):
    target = thread[0] / "workspace/lines"
    target.write_bytes("first\r\nsecond\rthird\u2028still-third\vstill-third\nlast".encode())
    result = run("read_file_rewrite", thread, path="lines", base=2, offset=2)
    assert result.content == "second\nthird\u2028still-third\vstill-third\n"
    numbered = run("read_file_rewrite", thread, path="lines", base=3, offset=1, line_numbers=True)
    assert numbered.content == "3: third\u2028still-third\vstill-third\n"


def test_edit_diff_marks_missing_final_newline(thread):
    (thread[0] / "workspace/no-newline").write_text("old")
    result = data(run("edit_file", thread, path="no-newline", old_string="old", new_string="new"))
    assert "-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n" in result["diff"]


def test_large_edit_uses_bounded_linear_diff(thread, monkeypatch):
    import app.tools.edit_file as module
    original = "a\nb\n" * 30000
    (thread[0] / "workspace/large-edit").write_text(original)
    def unbounded(*args, **kwargs):
        pytest.fail("large files must not use quadratic diff matching")
    monkeypatch.setattr(module, "unified_diff", unbounded)
    result = data(run("edit_file", thread, path="large-edit", old_string="a", new_string="c", replace_all=True))
    assert "@@ -1,60000 +1,60000 @@" in result["diff"]
    assert result["replacements"] == 30000


def test_scan_entry_depth_and_time_budgets(thread, monkeypatch):
    import app.tools.file_access as access
    workspace = thread[0] / "workspace"
    for index in range(4):
        (workspace / f"{index}.txt").write_text("x")
    monkeypatch.setattr(access, "MAX_SCAN_ENTRIES", 2)
    result = data(run("glob", thread, pattern="**/*"))
    assert result["truncated"]
    assert "entry_limit" in {item["reason"] for item in result["skipped"]}
    monkeypatch.setattr(access, "MAX_SCAN_ENTRIES", 10000)
    (workspace / "sub").mkdir()
    (workspace / "sub/deeper").mkdir()
    (workspace / "sub/deeper/x").write_text("x")
    monkeypatch.setattr(access, "MAX_SCAN_DEPTH", 1)
    result = data(run("grep", thread, pattern="x"))
    assert result["partial"] and result["truncated"]
    assert "depth_limit" in {item["reason"] for item in result["skipped"]}
    monkeypatch.setattr(access, "SCAN_SECONDS", -1.0)
    result = data(run("grep", thread, pattern="x"))
    assert result["partial"] and result["truncated"]
    assert "time_limit" in {item["reason"] for item in result["skipped"]}


def test_grep_total_bytes_budget_and_exact_match_limit(thread, monkeypatch):
    import app.tools.grep as module
    workspace = thread[0] / "workspace"
    (workspace / "one").write_text("x\nx\n")
    result = data(run("grep", thread, pattern="x", limit=2))
    assert len(result["matches"]) == 2 and not result["truncated"]
    assert data(run("grep", thread, pattern="x", limit=1))["truncated"]
    (workspace / "two").write_text("x\nx\n")
    monkeypatch.setattr(module, "MAX_GREP_TOTAL_BYTES", 5)
    result = data(run("grep", thread, pattern="x"))
    assert result["partial"] and result["truncated"]
    assert "total_bytes_limit" in {item["reason"] for item in result["skipped"]}


def test_scan_permission_failure_is_explicit(thread, monkeypatch):
    import app.tools.grep as module
    (thread[0] / "workspace/file").write_text("secret")
    def denied(*args):
        raise StorageError(backend="file", operation="read", category="permission", stage="open")
    monkeypatch.setattr(module, "bounded_read", denied)
    result = data(run("grep", thread, pattern="secret"))
    assert result["partial"] and not result["matches"]
    assert result["skipped"] == [{"path": "workspace/file", "reason": "permission"}]


def test_read_symlink_swap_between_validation_and_open_is_rejected(thread, monkeypatch):
    import app.tools.file_access as access
    target = thread[0] / "workspace/inside"
    target.write_text("inside")
    outside = thread[0] / "outside"
    outside.write_text("secret")
    read = access.bounded_read
    def swapped(fd, name, limit):
        target.unlink()
        target.symlink_to(outside)
        return read(fd, name, limit)
    monkeypatch.setattr(access, "bounded_read", swapped)
    result = run("read_file_rewrite", thread, path="inside")
    assert result.is_error and "secret" not in result.content


def test_parent_symlink_swap_between_resolution_and_open_is_rejected(thread, monkeypatch):
    import app.tools.file_access as access
    parent = thread[0] / "workspace/sub"
    parent.mkdir()
    outside = thread[0] / "outside-dir"
    outside.mkdir()
    resolve = access.resolve_target
    def swapped(context, raw):
        result = resolve(context, raw)
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
        return result
    monkeypatch.setattr(access, "resolve_target", swapped)
    result = run("write_file", thread, path="workspace/sub/file", content="secret")
    assert result.is_error and not (outside / "file").exists()


def test_directory_sync_failure_propagates_without_claiming_success(thread, monkeypatch):
    from app.storage import file_io
    fsync = file_io.os.fsync
    calls = 0
    def failed(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(5, "private physical path")
        return fsync(fd)
    monkeypatch.setattr(file_io.os, "fsync", failed)
    with pytest.raises(StatePersistenceError):
        run("write_file", thread, path="nested/file", content="done")


def test_write_and_edit_share_same_thread_lock(thread, monkeypatch):
    from app.storage.file_io import AtomicWriter
    target = thread[0] / "workspace/shared"
    target.write_text("before")
    started, release = threading.Event(), threading.Event()
    commit = AtomicWriter.commit
    calls = 0
    def delayed(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(3)
        return commit(self)
    monkeypatch.setattr(AtomicWriter, "commit", delayed)
    async def scenario():
        writing = asyncio.create_task(execute("write_file", thread[1], path="shared", content="after", overwrite=True))
        assert await asyncio.to_thread(started.wait, 3)
        editing = asyncio.create_task(execute("edit_file", thread[1], path="shared", old_string="after", new_string="edited"))
        await asyncio.sleep(0.02)
        assert not editing.done()
        release.set()
        return await asyncio.gather(writing, editing)
    assert all(not result.is_error for result in asyncio.run(scenario()))
    assert target.read_text() == "edited"



def test_grep_line_numbers_match_read_file(thread):
    (thread[0] / "workspace/lines").write_text("first\r\nsecond\u2028find-me\vstill-second\nlast")
    result = data(run("grep", thread, pattern="find-me"))
    assert result["matches"][0]["line"] == 2
    assert result["matches"][0]["text"] == "second\u2028find-me\vstill-second"


def test_grep_output_budget_is_explicit(thread, monkeypatch):
    import app.tools.grep as module
    (thread[0] / "workspace/output").write_text(("found " + "x" * 100 + "\n") * 20)
    monkeypatch.setattr(module, "MAX_GREP_OUTPUT_BYTES", 500, raising=False)
    result = data(run("grep", thread, pattern="found", context_lines=5))
    assert result["partial"] and result["truncated"]
    assert "output_limit" in {item["reason"] for item in result["skipped"]}
    assert len(json.dumps(result["matches"]).encode()) <= 500


@pytest.mark.parametrize("name,args", [
    ("edit_file", {"old_string": "old", "new_string": "new"}),
    ("write_file", {"content": "new", "overwrite": True}),
])
def test_mutation_preserves_executable_permissions_without_special_bits(thread, name, args):
    target = thread[0] / "workspace/script"
    target.write_text("old")
    target.chmod(0o6751)
    data(run(name, thread, path="script", **args))
    assert target.stat().st_mode & 0o7777 == 0o751


def test_upload_staging_is_not_exposed(thread):
    (thread[0] / "workspace/.upload-in-progress").write_text("secret")
    assert run("read_file_rewrite", thread, path=".upload-in-progress").is_error
    result = data(run("glob", thread, pattern="**/*"))
    assert result["files"] == []
    assert result["skipped"] == [{"path": "workspace/.upload-in-progress", "reason": "internal"}]



def test_skipped_details_are_bounded_without_hiding_incomplete_search(thread, monkeypatch):
    import app.tools.file_access as access
    workspace = thread[0] / "workspace"
    for index in range(10):
        (workspace / f".storage-{index}.tmp").write_text("secret")
    monkeypatch.setattr(access, "MAX_SKIPPED_DETAILS", 2, raising=False)
    for name, args in [("grep", {"pattern": "secret"}), ("glob", {"pattern": "**/*"})]:
        result = data(run(name, thread, **args))
        assert len(result["skipped"]) == 2
        assert result["skipped_count"] == 10
        assert result["skipped_truncated"]
        if name == "grep":
            assert result["partial"]


def test_glob_output_budget_reports_truncation(thread, monkeypatch):
    import app.tools.glob as module
    for index in range(10):
        (thread[0] / "workspace" / (str(index) + "x" * 80)).write_text("x")
    monkeypatch.setattr(module, "MAX_GLOB_OUTPUT_BYTES", 200, raising=False)
    result = data(run("glob", thread, pattern="**/*"))
    assert result["truncated"]
    assert "output_limit" in {item["reason"] for item in result["skipped"]}
    assert len(json.dumps(result["files"]).encode()) <= 200


def test_path_resolution_does_not_swallow_state_persistence_error(thread, monkeypatch):
    import app.tools.file_access as access
    def failed(*args):
        raise StatePersistenceError("critical")
    monkeypatch.setattr(access, "ThreadPaths", failed)
    for name, args in [("glob", {"pattern": "*"}), ("grep", {"pattern": "x"}),
                       ("write_file", {"content": "x"}),
                       ("edit_file", {"old_string": "x", "new_string": "y"}),
                       ("read_file_rewrite", {})]:
        with pytest.raises(StatePersistenceError):
            run(name, thread, path="file", **args)


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_preserved_read_can_feed_exact_multiline_edit(thread, newline):
    target = thread[0] / "workspace/newlines.txt"
    selected = f"alpha{newline}beta{newline}"
    original = f"prefix{newline}{selected}suffix"
    target.write_bytes(original.encode())

    default = run("read_file_rewrite", thread, path="newlines.txt", base=2, offset=2)
    assert not default.is_error
    assert default.content == "alpha\nbeta\n"
    rejected = run("edit_file", thread, path="newlines.txt",
                   old_string=default.content, new_string="changed")
    assert rejected.is_error
    assert target.read_bytes() == original.encode()

    preserved = run("read_file_rewrite", thread, path="newlines.txt",
                    base=2, offset=2, preserve_newlines=True)
    assert not preserved.is_error
    assert preserved.content == selected
    replacement = f"ALPHA{newline}BETA{newline}"
    data(run("edit_file", thread, path="newlines.txt",
             old_string=preserved.content, new_string=replacement))
    assert target.read_bytes() == original.replace(selected, replacement).encode()


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
@pytest.mark.parametrize("preserve_newlines", [False, True])
def test_preserve_newlines_keeps_textio_numbering_and_range(thread, newline, preserve_newlines):
    target = thread[0] / "workspace/numbered.txt"
    middle = "second\u2028same-line\vstill-same-line"
    target.write_bytes(f"first{newline}{middle}{newline}third{newline}last".encode())
    result = run("read_file_rewrite", thread, path="numbered.txt", base=2, offset=2,
                 line_numbers=True, preserve_newlines=preserve_newlines)
    expected_newline = newline if preserve_newlines else "\n"
    assert not result.is_error
    assert result.content == f"2: {middle}{expected_newline}3: third{expected_newline}"


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_preserve_newlines_requires_strict_boolean(thread, value):
    result = run("read_file_rewrite", thread, path="file", preserve_newlines=value)
    assert result.is_error
    assert result.content == "read_file 参数格式错误"
