"""记忆选择与抽取的模型契约，所有客户端为可控假模型。"""

import asyncio
import json
from copy import deepcopy

import pytest

from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext


def state(text="解释方案时先给结论"):
    return ThreadState(user_id="alice", thread_id="thread", workspace_path="/tmp/workspace",
                       messages=[Message(role="user", content=text)])


def context():
    async def no_op(*args, **kwargs):
        pass
    return RuntimeContext(user_id="alice", thread_id="thread", run_id="run",
                          workspace_path="/tmp/workspace", record_event=no_op, save_checkpoint=no_op)


def seed(store, name="解释风格", content="规则正文 PRIVATE_BODY\n**Why:** 用户要求\n**How to apply:** 解释时"):
    return store.upsert("alice", kind="feedback", name=name, description="先给结论",
                        content=content, source_thread_id="source", source_run_id="source-run")


class FakeModel:
    def __init__(self, content='{"memory_indices":[1]}', action=None):
        self.content, self.action, self.requests, self.closed = content, action, [], 0

    async def chat(self, messages, tools, **kwargs):
        self.requests.append((deepcopy(messages), tools))
        if self.action:
            await self.action()
        return Message(role="assistant", content=self.content)

    async def close(self):
        self.closed += 1


def test_selection_only_exposes_names_descriptions_then_loads_body(tmp_path):
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    record = seed(store)
    model = FakeModel()
    result = asyncio.run(MemorySelector(store, lambda: model).select(state(), context()))
    request, tools = model.requests[0]
    payload = json.loads(request[-1].content)
    assert tools == []
    assert payload["candidates"] == [{"index": 1, "name": "解释风格", "description": "先给结论"}]
    assert record.id not in request[-1].content and "PRIVATE_BODY" not in request[-1].content
    assert "PRIVATE_BODY" in result and "**How to apply:**" in result
    assert model.closed == 1


def test_empty_store_never_constructs_selector_model(tmp_path):
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore
    def forbidden():
        raise AssertionError("empty store created a model")
    assert asyncio.run(MemorySelector(MemoryStore(data_root=tmp_path), forbidden).select(state(), context())) == ""


@pytest.mark.parametrize("indices", [[True], [0], [-1], [2], ["1"], "1", [1, 1, 1, 1, 1, 1]])
def test_selection_rejects_invalid_indices(tmp_path, indices):
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    seed(store)
    model = FakeModel(json.dumps({"memory_indices": indices}))
    assert asyncio.run(MemorySelector(store, lambda: model).select(state(), context())) == ""
    assert model.closed == 1


def test_selection_snapshot_survives_index_reordering(tmp_path):
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    seed(store)
    async def reorder():
        seed(store, name="新记忆", content="NEW_CONTENT")
    model = FakeModel(action=reorder)
    result = asyncio.run(MemorySelector(store, lambda: model).select(state(), context()))
    assert "PRIVATE_BODY" in result and "NEW_CONTENT" not in result


def test_selector_timeout_falls_back_but_external_cancel_propagates(tmp_path):
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    seed(store)
    async def wait():
        await asyncio.Event().wait()
    model = FakeModel(action=wait)
    assert asyncio.run(MemorySelector(store, lambda: model, timeout=0.01).select(state(), context())) == ""
    assert model.closed == 1
    async def scenario():
        another = FakeModel(action=wait)
        task = asyncio.create_task(MemorySelector(store, lambda: another).select(state(), context()))
        while not another.requests:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert another.closed == 1
    asyncio.run(scenario())


def test_context_injection_is_request_only_and_reused(tmp_path):
    from app.agents.memory_context_middleware import MemoryContextMiddleware
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    seed(store)
    model = FakeModel()
    original = state()
    async def scenario():
        middleware = MemoryContextMiddleware(MemorySelector(store, lambda: model))
        await middleware.before_agent(original, context())
        for _ in range(2):
            messages = [Message(role="system", content="rules"), *original.messages]
            prepared = await middleware.prepare_model_messages(original, context(), messages)
            assert sum("<selected_memories>" in item.content for item in prepared) == 1
            assert prepared[-1].content == original.messages[-1].content
        assert len(model.requests) == 1
        assert not any("<selected_memories>" in item.content for item in original.messages)
    asyncio.run(scenario())


def extraction_change(**overrides):
    result = {"action": "add", "type": "feedback", "name": "解释风格",
              "description": "先给结论", "content":
              "解释时先给结论。\n**Why:** 用户要求\n**How to apply:** 解释时",
              "evidence": "解释方案时先给结论"}
    result.update(overrides)
    return result


def test_extracts_user_evidence_and_deduplicates(tmp_path):
    from app.memory.extractor import MemoryExtractor
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    models = []
    def factory():
        model = FakeModel(json.dumps({"changes": [extraction_change()]}, ensure_ascii=False))
        models.append(model)
        return model
    for _ in range(2):
        asyncio.run(MemoryExtractor(store, factory).extract(state(), "run"))
    assert len(store.list("alice")) == 1
    assert all(model.closed == 1 and model.requests[0][1] == [] for model in models)


@pytest.mark.parametrize("override", [
    {"type": "project"}, {"evidence": "助手编造的事实"}, {"evidence": ""},
    {"action": "update", "memory_id": "feedback_unknown"},
    {"content": "API_KEY=sk-test-secret-value"}, {"name": ""},
    {"action": "delete"}, {"path": "../../outside.md"},
])
def test_invalid_extraction_cannot_write(tmp_path, override):
    from app.memory.extractor import MemoryExtractor
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    model = FakeModel(json.dumps({"changes": [extraction_change(**override)]}, ensure_ascii=False))
    with pytest.raises(ValueError):
        asyncio.run(MemoryExtractor(store, lambda: model).extract(state(), "run"))
    assert store.list("alice") == []
    assert model.closed == 1


def test_prior_user_or_assistant_text_is_not_current_evidence(tmp_path):
    from app.memory.extractor import MemoryExtractor
    from app.memory.store import MemoryStore
    original = state()
    original.messages.extend([Message(role="assistant", content="解释方案时先给结论"),
                              Message(role="user", content="今天下雨吗")])
    store = MemoryStore(data_root=tmp_path)
    model = FakeModel(json.dumps({"changes": [extraction_change()]}, ensure_ascii=False))
    with pytest.raises(ValueError):
        asyncio.run(MemoryExtractor(store, lambda: model).extract(original, "run"))
    assert store.list("alice") == []


def test_all_change_text_is_validated_before_first_write(tmp_path):
    from app.memory.extractor import MemoryExtractor
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    changes = [extraction_change(), extraction_change(name="broken\ud800")]
    model = FakeModel(json.dumps({"changes": changes}))
    with pytest.raises(ValueError):
        asyncio.run(MemoryExtractor(store, lambda: model).extract(state(), "run"))
    assert store.list("alice") == []


def test_cancel_during_failed_selection_cleanup_propagates(tmp_path):
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore

    async def scenario():
        store = MemoryStore(data_root=tmp_path)
        seed(store)
        closing = asyncio.Event()

        class Model:
            async def chat(self, **kwargs):
                raise ValueError("selection failure")

            async def close(self):
                closing.set()
                await asyncio.sleep(0.01)

        task = asyncio.create_task(MemorySelector(store, Model).select(state(), context()))
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel("user_cancelled_during_cleanup")
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_current_request_tail_is_visible_after_long_paste(tmp_path):
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    seed(store)
    model = FakeModel()
    original = state("背景资料" * 6000 + "请按照先给结论的习惯解释方案")
    asyncio.run(MemorySelector(store, lambda: model).select(original, context()))
    payload = json.loads(model.requests[0][0][-1].content)
    assert payload["current_user"].endswith("请按照先给结论的习惯解释方案")
    assert len(payload["current_user"]) <= 16_000


def test_selected_bodies_skip_whole_oversized_record_and_preserve_order(tmp_path):
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    for index in range(5):
        seed(store, name=f"规则{index}", content="x" * 25_000 if index == 3 else f"BODY_{index}")
    model = FakeModel('{"memory_indices":[2,5,1,5,3]}')
    result = json.loads(asyncio.run(MemorySelector(store, lambda: model).select(state(), context())))
    assert [item["name"] for item in result] == ["规则0", "规则4", "规则2"]


def test_selection_candidate_budget_is_whole_sequential_items(tmp_path, monkeypatch):
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    for index in range(4):
        seed(store, name=f"规则{index}")
    monkeypatch.setattr("app.memory.selector.INDEX_CHAR_BUDGET", 160)
    model = FakeModel('{"memory_indices":[]}')
    asyncio.run(MemorySelector(store, lambda: model).select(state(), context()))
    candidates = json.loads(model.requests[0][0][-1].content)["candidates"]
    assert 0 < len(candidates) < 4
    assert [item["index"] for item in candidates] == list(range(1, len(candidates) + 1))
    assert len(json.dumps(candidates, ensure_ascii=False)) <= 160


def test_selector_never_swallows_state_persistence_error(tmp_path, monkeypatch):
    from app.memory.selector import MemorySelector
    from app.memory.store import MemoryStore
    from app.runtime.errors import StatePersistenceError
    store = MemoryStore(data_root=tmp_path)

    def fail(_user):
        raise StatePersistenceError("read failed")

    monkeypatch.setattr(store, "catalog", fail)
    with pytest.raises(StatePersistenceError):
        asyncio.run(MemorySelector(store, FakeModel).select(state(), context()))


@pytest.mark.parametrize("secret", [
    "My password is hunter2", "我的密码是测试口令", "服务密钥：测试凭据",
    "ghp_" + "A" * 36, "github_pat_" + "A" * 30,
    "xoxb-1234567890-1234567890-abcdefghij", "AKIA" + "A" * 16,
])
def test_credentials_in_natural_language_or_known_token_format_cannot_write(tmp_path, secret):
    from app.memory.extractor import MemoryExtractor
    from app.memory.store import MemoryStore
    store = MemoryStore(data_root=tmp_path)
    model = FakeModel(json.dumps({"changes": [extraction_change(
        type="user", content=secret, evidence=secret)]}, ensure_ascii=False))
    with pytest.raises(ValueError):
        asyncio.run(MemoryExtractor(store, lambda: model).extract(state(secret), "run"))
    assert store.list("alice") == []


@pytest.mark.parametrize("duplicate_target", [False, True])
def test_current_correction_updates_same_id_or_rejects_duplicate_targets(tmp_path, duplicate_target):
    from datetime import datetime, timedelta, timezone
    from app.memory.extractor import MemoryExtractor
    from app.memory.store import MemoryStore
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    store = MemoryStore(data_root=tmp_path, clock=lambda: now)
    previous = seed(store)
    now += timedelta(hours=49)
    assert previous.is_stable(now)
    change = extraction_change(action="update", memory_id=previous.id,
        content="结论优先，然后用一个简短例子。\n**Why:** 用户明确纠正。\n**How to apply:** 解释时。")
    model = FakeModel(json.dumps({"changes": [change, change] if duplicate_target else [change]},
                                ensure_ascii=False))
    if duplicate_target:
        with pytest.raises(ValueError):
            asyncio.run(MemoryExtractor(store, lambda: model).extract(state(), "new-run"))
        assert store.read("alice", previous.id) == previous
    else:
        asyncio.run(MemoryExtractor(store, lambda: model).extract(state(), "new-run"))
        updated = store.read("alice", previous.id)
        assert len(store.list("alice")) == 1
        assert updated.id == previous.id and updated.content == change["content"]
        assert updated.created_at == previous.created_at and updated.updated_at == now
        assert updated.source_run_id == "new-run" and updated.source_thread_id == "thread"
        assert not updated.is_stable(now)
