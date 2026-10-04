# Structured Memory Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Preserve the captured working-tree baseline and do not deploy services.

**Goal:** Implement user-private Markdown memories with numeric metadata-only selection and successful-Run asynchronous extraction.

**Architecture:** A filesystem store owns validation, atomic Markdown/index persistence and the 48-hour derived stable flag. A lead-agent middleware selects once and injects request-only context. A Coordinator-owned worker serializes extraction per user after confirmed success.

**Tech Stack:** Python 3.12, existing ChatModel protocol/error middleware, asyncio, SQLite-backed Runtime, pytest with fake models and temporary real files/databases.

**Workspace:** `/Users/jk/Documents/ChatGPT/de/work/structured-memory-20261004`; remote Git worktree `/home/pl/.config/superpowers/worktrees/deer_mini/structured-memory`; early isolated test copy `/home/pl/deer_mini_memory_20261004`. The baseline captures remote tracked and untracked backend code without credentials or runtime data.

## Task 1 — Filesystem store

- [x] Add `backend/tests/app/memory/test_store.py` before production code. Test three types, private user roots, safe paths/symlinks, missing/corrupt index rebuild, duplicate/update stability, exact 48-hour boundary and atomic replacement failure.
- [x] Verify missing `app.memory.store` fails the new tests.
- [x] Implement `backend/app/memory/store.py` and package initializer. Interface:

```python
store = MemoryStore(data_root=temporary_root, clock=utc_clock)
record = store.upsert("alice", kind="feedback", name="解释风格",
    description="先给结论", content="先给结论\n\n**Why:** 用户要求\n**How to apply:** 解释方案",
    source_thread_id="thread", source_run_id="run")
records = store.list("alice")
selected = store.read("alice", record.id)
assert selected.is_stable(now) is False
```

- [x] Run store tests with remote Python 3.12 in the isolated copy, inspect specification compliance then code quality.

## Task 2 — Selection, context and extraction

- [x] Add `backend/tests/app/memory/test_model_flow.py` before implementation. Fake model records messages and returns `{"memory_indices":[1]}`. Verify only name/description/number exposed, selected complete feedback body injected, empty store skips call, invalid indices/cancellation/timeouts, snapshot mapping, no duplicate injection and evidence-backed extraction.
- [x] Implement `backend/app/memory/prompts.py`, `model_calls.py`, `selector.py`, `extractor.py` and `backend/app/agents/memory_context_middleware.py`.
- [x] Selector interface `await selector.select(state, context) -> str`; extractor interface `await extractor.extract(state, run_id) -> None`. Inject independent `Callable[[], ChatModel]` and shared store. Internal model calls use `tools=[]`, isolated error middleware/retry budget and bounded timeout, no UI output; always close clients.
- [x] Selection outputs `memory_indices`, strict positive ints, maximum 5, deduplicated, mapped using immutable per-call candidates. Input history max 8 messages, candidate budget 32,000 characters, selected body budget 24,000.
- [x] Extraction output `changes` contains `action`, `memory_id` for updates, `type`, `name`, `description`, `content`, `evidence`. Match evidence against user message text; reject secret-looking content, unsupported types/fields and invalid targets before any batch write. Persist on background filesystem thread and propagate cancellation/persistence failure to worker boundary.
- [x] Run new tests and review scope/quality before integration.

## Task 3 — Successful Run scheduling

- [x] Add `backend/tests/app/services/test_memory_flow.py`: real temporary SQLite and ThreadService, two independent model clients, successful main state before extraction, no extraction on cancellation/error/persistence failure, background failure leaves successful Run intact, per-user serialization, bounded shutdown.
- [x] Implement `backend/app/memory/service.py` for task tracking, per-user queues/locks and shutdown. Inject into `RunCoordinator` with optional enablement; keep empty-memory selection free of extra calls.
- [x] Modify `backend/app/services/run_coordinator.py`: construct lead memory middleware; successful task callback uses a captured final-state copy and Run identity; add background memory service shutdown. Preserve main Run and event error behavior.
- [x] Modify `backend/app/agents/prompts/builder.py` to include fixed memory-use rules and `backend/.env.example` to document memory enablement without keys.
- [x] Run new lifecycle tests, then existing backend regression.

## Task 4 — Review and handoff

- [x] Review the task diff relative to the snapshot baseline, including no secrets/data and no unrelated frontend changes.
- [x] Write `backend/docs/memory.md`: paths, body/index schemas, numeric snapshot IDs, stable reset semantics, read/write timing, optional settings, restart/next-turn limitations.
- [x] Copy approved design and completed plan into isolated project docs. Preserve task-only changes in focused Git commits; verify captured existing-file baselines before transfer.
- [x] Run `pytest tests/app --ignore=tests/app/model/test_factory.py`, compile new modules, and `git diff --check` in isolated workspace.
- [x] Report verified changes and checks; preserve development copies. Commit focused module increments on `codex/structured-memory` as now authorized; do not push/deploy without current-task authorization.

## Test command

After transferring only changed source/test files to the isolated remote copy:

```sh
cd /home/pl/deer_mini_memory_20261004/backend
env -u DEER_MINI_DATABASE_PATH -u DEER_MINI_DATA_ROOT /home/pl/deer_mini/backend/.venv/bin/python -c 'from app.infrastructure.database import initialize_database; initialize_database(); import pytest; raise SystemExit(pytest.main(["tests/app", "--ignore=tests/app/model/test_factory.py", "-q"]))'
```

The development copy has no live data or `.env`. Existing tests override environment paths or `DATABASE_PATH` themselves; global path overrides must not mask those fixtures.

## Final verification

- Isolated remote Git worktree, Python 3.12: 598 passed, 10 skipped; one existing LangSmith deprecation warning.
- Selection cleanup cancellation and credentials regression cases were reproduced failing before their fixes, then passed in the complete suite.
- Successful same-ID correction, duplicate update rejection, enabled-memory outer Run timeout, per-user serialization and client shutdown are covered.
- Module syntax compilation and task-diff whitespace checks passed. No real model call or live database was used.
- Keep codex/structured-memory for review; do not merge, push or deploy as part of this task.
