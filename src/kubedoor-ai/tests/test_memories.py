import asyncio
import copy
import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from kubedoor_ai.domain import AIError, Identity
from kubedoor_ai.memories import MemoryLibrary, redact_memory_text


WRITER = Identity("operator", "rw")
READER = Identity("observer", "read")
PROVIDER = {"base_url": "https://mock.invalid/v1", "api_key": "temporary-provider-secret", "model": "deepseek-flash"}
META = ("id", "title", "version", "created_by", "updated_by", "created_at", "updated_at")


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.sessions = {"own-session": WRITER.username, "reader-session": READER.username, "other-session": "other"}
        self.active = False
        self.messages = []

    async def session(self, sid, identity):
        self.calls.append(("session", sid, identity.username))
        if self.sessions.get(sid) != identity.username:
            raise AIError("会话不存在", 404, "not_found")
        return {"id": sid, "username": identity.username}

    async def one(self, sql, *args):
        self.calls.append(("one", sql, args))
        if "FROM kubedoor_ai_runs" in sql:
            return {"id": "running"} if self.active else None
        if sql.startswith("SELECT count(*)"):
            return {"total": len(self.matching(args[0]))}
        if sql.startswith("INSERT INTO kubedoor_ai_memories"):
            mid, title, content, username = args
            self.rows[mid] = {"id": mid, "title": title, "content": content, "version": 1,
                              "created_by": username, "updated_by": username, "created_at": 1, "updated_at": 1}
            return copy.deepcopy(self.rows[mid])
        mid = args[0]
        row = self.rows.get(mid)
        if sql.startswith("UPDATE kubedoor_ai_memories"):
            if not row or row["version"] != args[4]:
                return None
            row.update(title=args[1], content=args[2], updated_by=args[3], version=row["version"] + 1,
                       updated_at=row["updated_at"] + 1)
            return copy.deepcopy(row)
        if sql.startswith("DELETE FROM kubedoor_ai_memories"):
            if not row or row["version"] != args[1]:
                return None
            self.rows.pop(mid)
            return {"id": mid}
        if sql.startswith("SELECT version"):
            return {"version": row["version"]} if row else None
        return copy.deepcopy(row)

    def matching(self, pattern):
        needle = pattern[1:-1].replace("\\%", "%").replace("\\_", "_").replace("\\\\", "\\").lower()
        return sorted((row for row in self.rows.values() if needle in row["title"].lower()),
                      key=lambda row: (-row["updated_at"], row["id"]))

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if "FROM kubedoor_ai_messages" in sql:
            assert "r.status='completed'" in sql
            assert "JOIN kubedoor_ai_runs" in sql
            rows = [row for row in self.messages if row.get("status", "completed") == "completed"]
            return copy.deepcopy(list(reversed(rows))[:args[1]])
        if "ANY($1::uuid[])" in sql:
            return [copy.deepcopy(row) for mid, row in self.rows.items() if mid in args[0]]
        pattern, size, offset = args
        return [{key: row[key] for key in META} for row in self.matching(pattern)[offset:offset + size]]


class SummaryModel:
    def __init__(self, output=None, error=None, delay=0):
        self.output = output or AIMessage(content=json.dumps({"title": "排障经验", "content": "## 验证\n先查询，再修改。"}, ensure_ascii=False))
        self.error, self.delay, self.calls = error, delay, []

    def bind_tools(self, *_args, **_kwargs):
        raise AssertionError("Summary must never bind cluster tools")

    async def ainvoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.output


@pytest.fixture
def library():
    return MemoryLibrary(MemoryStore())


@pytest.mark.asyncio
async def test_global_memory_listing_is_paginated_and_only_contains_metadata(library):
    await library.create(WRITER, {"title": "DNS经验", "content": "private body"})
    other = await library.create(Identity("another", "rw"), {"title": "资源经验", "content": "different body"})
    result = await library.list({"page": "1", "page_size": "1"})
    assert result["total"] == 2 and result["page"] == 1 and result["page_size"] == 1
    assert len(result["memories"]) == 1
    assert set(result["memories"][0]) == set(META)
    assert "content" not in result["memories"][0]
    assert (await library.get(other["id"]))["created_by"] == "another"
    queries = [call[1] for call in library.store.calls if call[0] in ("one", "fetch")]
    assert not any("username=" in sql or "created_by=" in sql for sql in queries if "SELECT" in sql)
    assert (await library.list({"page": 5, "page_size": 1}))["total"] == 2


@pytest.mark.asyncio
async def test_title_search_is_a_literal_parameter_not_sql_or_wildcards(library):
    await library.create(WRITER, {"title": "CPU_100%", "content": "body"})
    await library.create(WRITER, {"title": "CPUanything", "content": "body"})
    result = await library.list({"q": "_100%"})
    assert result["total"] == 1
    call = next(call for call in reversed(library.store.calls) if call[0] == "fetch")
    assert call[2][0] == "%\\_100\\%%"
    assert "_100%" not in call[1]
    assert (await library.list({"search": "anything"}))["total"] == 1


@pytest.mark.parametrize("query", [{"page": 0}, {"page": True}, {"page": "1.5"}, {"page": 10**30},
                                   {"page_size": 101}, {"page_size": -1}, {"page_size": False},
                                   {"q": []}, {"q": "x" * 201}, ["bad-query"]])
@pytest.mark.asyncio
async def test_invalid_list_input_does_not_query_store(library, query):
    with pytest.raises(AIError):
        await library.list(query)
    assert library.store.calls == []


@pytest.mark.parametrize("method", ["create", "update", "delete"])
@pytest.mark.asyncio
async def test_read_only_users_cannot_modify_shared_memory(library, method):
    with pytest.raises(AIError) as caught:
        if method == "create":
            await library.create(READER, {"title": "x", "content": "x"})
        elif method == "update":
            await library.update(READER, str(uuid.uuid4()), {"title": "x", "content": "x", "version": 1})
        else:
            await library.delete(READER, str(uuid.uuid4()), 1)
    assert caught.value.status == 403 and not library.store.calls


@pytest.mark.parametrize("data", [None, {}, {"title": "", "content": "x"}, {"title": "x", "content": " "},
                                  {"title": "x" * 201, "content": "x"}, {"title": "x", "content": "x" * 20001},
                                  {"title": 7, "content": "x"}, {"title": "x", "content": {}}])
@pytest.mark.asyncio
async def test_empty_or_oversized_fields_are_rejected_before_persistence(library, data):
    with pytest.raises(AIError):
        await library.create(WRITER, data)
    assert not library.store.calls


@pytest.mark.asyncio
async def test_title_at_input_limit_cannot_exceed_database_limit_after_redaction(library):
    credential = " password=x"
    title = "t" * (200 - len(credential)) + credential
    assert len(title) == 200 and len(redact_memory_text(title)) > 200
    with pytest.raises(AIError) as caught:
        await library.create(WRITER, {"title": title, "content": "有效正文"})
    assert caught.value.status == 400 and caught.value.code == "invalid_memory"
    assert not library.store.calls


@pytest.mark.asyncio
async def test_content_at_input_limit_cannot_exceed_database_limit_after_redaction(library):
    credential = " password=x"
    content = "c" * (20000 - len(credential)) + credential
    assert len(content) == 20000 and len(redact_memory_text(content)) > 20000
    with pytest.raises(AIError) as caught:
        await library.create(WRITER, {"title": "有效标题", "content": content})
    assert caught.value.status == 400 and caught.value.code == "invalid_memory"
    assert not library.store.calls


@pytest.mark.asyncio
async def test_edits_and_deletes_use_optimistic_versions_and_keep_authorship(library):
    created = await library.create(WRITER, {"title": " 原标题 ", "content": " 内容 "})
    assert created["title"] == "原标题" and created["version"] == 1
    changed = await library.update(Identity("editor", "rw"), created["id"], {"title": "新标题", "content": "新正文", "version": 1})
    assert changed["version"] == 2 and changed["created_by"] == "operator" and changed["updated_by"] == "editor"
    with pytest.raises(AIError) as stale:
        await library.update(WRITER, created["id"], {"title": "覆盖", "content": "旧版本", "version": 1})
    assert stale.value.status == 409 and stale.value.details == {"current_version": 2}
    with pytest.raises(AIError) as stale_delete:
        await library.delete(WRITER, created["id"], 1)
    assert stale_delete.value.code == "memory_version_conflict"
    assert (await library.get(created["id"]))["content"] == "新正文"
    assert await library.delete(WRITER, created["id"], 2) == {"success": True}
    with pytest.raises(AIError) as missing:
        await library.get(created["id"])
    assert missing.value.status == 404
    with pytest.raises(AIError) as missing_update:
        await library.update(WRITER, created["id"], {"title": "x", "content": "x", "version": 2})
    assert missing_update.value.status == 404


@pytest.mark.parametrize("version", [None, 0, -1, True, "1", 1.5])
@pytest.mark.asyncio
async def test_edit_requires_positive_integer_version(library, version):
    with pytest.raises(AIError) as caught:
        await library.update(WRITER, str(uuid.uuid4()), {"title": "x", "content": "x", "version": version})
    assert caught.value.code == "invalid_memory_version" and not library.store.calls


@pytest.mark.parametrize("version", [None, True, "-1", "1.0", "1;DROP TABLE", "" , "1" * 11])
@pytest.mark.asyncio
async def test_delete_rejects_invalid_query_versions_before_db(library, version):
    with pytest.raises(AIError) as caught:
        await library.delete(WRITER, str(uuid.uuid4()), version)
    assert caught.value.code == "invalid_memory_version" and not library.store.calls


@pytest.mark.asyncio
async def test_delete_accepts_http_query_string_version(library):
    created = await library.create(WRITER, {"title": "x", "content": "x"})
    assert await library.delete(WRITER, created["id"], "1") == {"success": True}


@pytest.mark.asyncio
async def test_resolve_uses_authoritative_content_deduplicates_and_preserves_selection_order(library):
    one = await library.create(WRITER, {"title": "1", "content": "body1"})
    two = await library.create(WRITER, {"title": "2", "content": "body2"})
    library.store.calls.clear()
    assert await library.resolve([]) == []
    assert await library.resolve(None) == []
    assert not library.store.calls
    result = await library.resolve([two["id"].upper(), one["id"], two["id"]])
    assert [item["id"] for item in result] == [two["id"], one["id"]]
    assert result[0] == {"id": two["id"], "title": "2", "content": "body2", "version": 1}
    result[0]["content"] = "client tampered"
    assert (await library.resolve([two["id"]]))[0]["content"] == "body2"


@pytest.mark.parametrize("ids", ["id", {}, ["not-uuid"], [False], [str(uuid.uuid4())] * 11,
                                 [{"id": str(uuid.uuid4()), "content": "client content"}]])
@pytest.mark.asyncio
async def test_invalid_selection_does_not_query_store(library, ids):
    with pytest.raises(AIError):
        await library.resolve(ids)
    assert not library.store.calls


@pytest.mark.asyncio
async def test_selection_is_atomic_if_any_memory_missing_and_limits_total_text(library):
    one = await library.create(WRITER, {"title": "1", "content": "x" * 20000})
    two = await library.create(WRITER, {"title": "2", "content": "x" * 12001})
    with pytest.raises(AIError) as missing:
        await library.resolve([one["id"], str(uuid.uuid4())])
    assert missing.value.status == 404
    with pytest.raises(AIError) as large:
        await library.resolve([one["id"], two["id"]])
    assert large.value.code == "memory_content_too_large"
    two = await library.update(WRITER, two["id"], {"title": "2", "content": "x" * 12000, "version": 1})
    assert len(await library.resolve([one["id"], two["id"]])) == 2


@pytest.mark.parametrize("value,secret", [
    ("api_key: abcdefg", "abcdefg"), ('"apikey": "json-secret"', "json-secret"),
    ("API key=human-secret", "human-secret"), ("password: database-pass", "database-pass"),
    ("Bearer token-abc123", "token-abc123"), ("Basic YWJjMTIzNA==", "YWJjMTIzNA=="),
    ("sk-testtemporary0123456789abcdef", "sk-testtemporary0123456789abcdef"),
    ("client-key-data: YWJjMTIzNA==", "YWJjMTIzNA=="),
    ("postgres://user:password-secret@database/db", "password-secret"),
    ("-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----", "SECRET"),
])
def test_credential_literals_are_scrubbed_from_public_memory(value, secret):
    result = redact_memory_text(value)
    assert secret not in result and "[redacted" in result


@pytest.mark.asyncio
async def test_create_get_and_resolve_scrub_credentials(library):
    row = await library.create(WRITER, {"title": "apikey=title-secret", "content": "Bearer body-secret"})
    assert "title-secret" not in json.dumps(row) and "body-secret" not in json.dumps(row)
    library.store.rows[row["id"]]["content"] = "legacy arbitrary-provider-secret"
    assert "arbitrary-provider-secret" not in (await library.resolve([row["id"]], secrets=("arbitrary-provider-secret",)))[0]["content"]
    library.store.rows[row["id"]]["content"] = "legacy password=private"
    assert "private" not in (await library.get(row["id"]))["content"]


def completed_message(content="操作成功", **changes):
    return {"id": str(uuid.uuid4()), "role": "assistant", "content": content, "scope": {"env": "cluster-a"},
            "tools": [{"name": "restart_deployment", "state": "completed", "result": {"success": True}}],
            "memories": [], "created_at": 1, "status": "completed", **changes}


@pytest.mark.asyncio
async def test_summary_uses_private_completed_evidence_and_creates_only_editable_draft():
    store, model = MemoryStore(), SummaryModel()
    store.messages = [completed_message("用户问题", role="user"), completed_message("已核验",
        memories=[{"id": str(uuid.uuid4()), "title": "前次经验", "content": "选择的记忆", "version": 1}]),
        completed_message("失败的轮次不能当成功", status="failed"), completed_message("取消的轮次不能当成功", status="cancelled")]
    providers = []
    library = MemoryLibrary(store, model_factory=lambda provider: providers.append(provider) or model)
    draft = await library.summarize(WRITER, "own-session", PROVIDER)
    assert draft == {"title": "排障经验", "content": "## 验证\n先查询，再修改。"}
    assert not store.rows
    messages, kwargs = model.calls[0]
    assert isinstance(messages[0], SystemMessage) and isinstance(messages[1], HumanMessage)
    assert "选择的记忆" in messages[1].content and "restart_deployment" in messages[1].content
    assert "失败的轮次不能当成功" not in messages[1].content and "取消的轮次不能当成功" not in messages[1].content
    assert messages[1].content.index("用户问题") < messages[1].content.index("已核验")
    assert "只获得批准" in messages[0].content and "不调用任何工具" in messages[0].content
    assert kwargs == {"config": {"callbacks": []}} and len(providers) == 1
    assert PROVIDER["api_key"] not in str(store.calls)
    assert not any(call[0] == "one" and "INSERT" in call[1] for call in store.calls)


@pytest.mark.asyncio
async def test_read_user_can_generate_draft_but_cannot_publish_it():
    store, model = MemoryStore(), SummaryModel()
    store.messages = [completed_message()]
    library = MemoryLibrary(store, lambda _: model)
    draft = await library.summarize(READER, "reader-session", PROVIDER)
    with pytest.raises(AIError) as caught:
        await library.create(READER, draft)
    assert caught.value.status == 403 and not store.rows


@pytest.mark.asyncio
async def test_summary_rejects_other_users_session_before_any_history_or_model_read():
    store, model = MemoryStore(), SummaryModel()
    with pytest.raises(AIError) as caught:
        await MemoryLibrary(store, lambda _: model).summarize(WRITER, "other-session", PROVIDER)
    assert caught.value.status == 404
    assert store.calls == [("session", "other-session", "operator")] and not model.calls


@pytest.mark.asyncio
async def test_summary_rejects_running_or_empty_conversations_before_model_call():
    store, model = MemoryStore(), SummaryModel()
    store.active = True
    with pytest.raises(AIError) as active:
        await MemoryLibrary(store, lambda _: model).summarize(WRITER, "own-session", PROVIDER)
    assert active.value.status == 409 and active.value.code == "memory_summary_active"
    assert not any(call[0] == "fetch" for call in store.calls) and not model.calls
    store.active = False
    with pytest.raises(AIError) as empty:
        await MemoryLibrary(store, lambda _: model).summarize(WRITER, "own-session", PROVIDER)
    assert empty.value.code == "memory_summary_empty" and not model.calls


@pytest.mark.asyncio
async def test_summary_redacts_input_and_provider_echo_from_output():
    store = MemoryStore()
    store.messages = [completed_message("Bearer private-value " + PROVIDER["api_key"], tools=[{"result": {"api_key": "other-secret"}}])]
    model = SummaryModel(AIMessage(content=json.dumps({"title": "title " + PROVIDER["api_key"],
                                                      "content": "password=generated-secret " + PROVIDER["api_key"]})))
    result = await MemoryLibrary(store, lambda _: model).summarize(WRITER, "own-session", PROVIDER)
    assert PROVIDER["api_key"] not in json.dumps(result) and "generated-secret" not in json.dumps(result)
    prompt = str(model.calls[0][0])
    assert PROVIDER["api_key"] not in prompt and "private-value" not in prompt and "other-secret" not in prompt


@pytest.mark.parametrize("truncation", ["count", "long", "total"])
@pytest.mark.asyncio
async def test_partial_summary_is_explicit_and_input_size_is_bounded(truncation):
    store, model = MemoryStore(), SummaryModel()
    if truncation == "count":
        store.messages = [completed_message(f"message-{n}") for n in range(90)]
    elif truncation == "long":
        store.messages = [completed_message("x" * 100000)]
    else:
        store.messages = [completed_message("x" * 10000) for _ in range(20)]
    draft = await MemoryLibrary(store, lambda _: model).summarize(WRITER, "own-session", PROVIDER)
    assert draft["content"].startswith("> 输入范围说明：") and "未总结全部会话" in draft["content"]
    prompt = model.calls[0][0][1].content
    assert len(prompt) < 49000
    if truncation == "count":
        assert "message-89" in prompt and "message-0\"" not in prompt


@pytest.mark.parametrize("output", ["not JSON", "{}", '[]', '{"title":"x","content":""}',
                                    json.dumps({"title": "x" * 201, "content": "body"})])
@pytest.mark.asyncio
async def test_invalid_summary_response_does_not_publish_anything(output):
    store, model = MemoryStore(), SummaryModel(AIMessage(content=output))
    store.messages = [completed_message()]
    with pytest.raises(AIError) as caught:
        await MemoryLibrary(store, lambda _: model).summarize(WRITER, "own-session", PROVIDER)
    assert caught.value.code == "memory_summary_invalid" and not store.rows


@pytest.mark.asyncio
async def test_fenced_and_content_block_json_are_supported_and_tool_calls_are_rejected():
    store = MemoryStore()
    store.messages = [completed_message()]
    model = SummaryModel(AIMessage(content=[{"type": "text", "text": '```json\n{"title":"标题","content":"正文"}\n```'}]))
    assert await MemoryLibrary(store, lambda _: model).summarize(WRITER, "own-session", PROVIDER) == {"title": "标题", "content": "正文"}
    model.output = AIMessage(content="", tool_calls=[{"id": "call1", "name": "kubedoor_tool", "args": {}}])
    with pytest.raises(AIError) as caught:
        await MemoryLibrary(store, lambda _: model).summarize(WRITER, "own-session", PROVIDER)
    assert caught.value.code == "memory_summary_invalid"


@pytest.mark.asyncio
async def test_summary_timeout_and_errors_hide_credentials_and_do_not_save(monkeypatch):
    import kubedoor_ai.memories as memories
    store, model = MemoryStore(), SummaryModel(error=ValueError(PROVIDER["api_key"]))
    store.messages = [completed_message()]
    with pytest.raises(AIError) as caught:
        await MemoryLibrary(store, lambda _: model).summarize(WRITER, "own-session", PROVIDER)
    assert caught.value.code == "memory_summary_failed" and PROVIDER["api_key"] not in str(caught.value)
    model.error, model.delay = None, 0.05
    monkeypatch.setattr(memories, "SUMMARY_TIMEOUT", 0.001)
    with pytest.raises(AIError) as timed_out:
        await MemoryLibrary(store, lambda _: model).summarize(WRITER, "own-session", PROVIDER)
    assert timed_out.value.status == 504 and timed_out.value.code == "memory_summary_timeout"
    assert not store.rows
