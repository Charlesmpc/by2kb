from types import SimpleNamespace

import pytest

from by2kb.integrations import hermes


class Context:
    def register_skill(self, *args, **kwargs):
        pass
    def register_hook(self, name, hook):
        self.hook = hook


def event(text, user="alice", chat="room", thread=""):
    return SimpleNamespace(text=text, message_id="message", source=SimpleNamespace(
        platform="telegram", chat_id=chat, user_id=user, thread_id=thread))


class InlineThread:
    def __init__(self, target, args, **kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


@pytest.mark.asyncio
async def test_explicit_topic_then_numeric_reply_scope_and_enrichment(monkeypatch):
    calls, replies, scopes = [], [], []
    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:2] == ["search", "discover"]:
            scopes.append(arguments[arguments.index("--scope") + 1])
            return {"session_id": "session", "status": "awaiting_selection", "message": "1. Real candidate"}
        if arguments[:2] == ["search", "latest"]:
            scopes.append(arguments[arguments.index("--scope") + 1])
            return {"session_id": "session", "status": "awaiting_selection", "topic": "TiDB"}
        if arguments[:2] == ["search", "select"]:
            scopes.append(arguments[arguments.index("--scope") + 1])
            return {"status": "selected", "results": [{"number": 1, "status": "enrichment_pending", "job_id": "job"}]}
        raise AssertionError(arguments)
    monkeypatch.setattr(hermes, "_run_by2kb", run)
    monkeypatch.setattr(hermes.threading, "Thread", InlineThread)
    monkeypatch.setattr(hermes, "_send", lambda loop, adapter, chat, text, reply: replies.append(text))
    monkeypatch.setattr(hermes, "_run_staged_enrichment", lambda ctx, job: {"artifacts": {}})
    monkeypatch.setattr(hermes, "_read_abstract", lambda artifacts: "summary")
    context = Context()
    hermes.register(context)
    gateway = SimpleNamespace(_is_user_authorized=lambda source: True, adapters={"telegram": object()})
    assert context.hook(event("by2kb 我想了解 TiDB"), gateway)["action"] == "skip"
    assert context.hook(event("1"), gateway)["action"] == "skip"
    assert len(set(scopes)) == 1
    assert any("Real candidate" in reply for reply in replies)
    assert any("summary" in reply for reply in replies)
    assert calls[0][2] == "我想了解 TiDB"
    assert any(call[:2] == ["search", "select"] for call in calls)


@pytest.mark.asyncio
async def test_ordinary_chat_no_pending_selection_and_authorization(monkeypatch):
    calls = []
    monkeypatch.setattr(hermes, "_run_by2kb", lambda args, **kw: calls.append(args))
    context = Context()
    hermes.register(context)
    gateway = SimpleNamespace(_is_user_authorized=lambda source: True, adapters={"telegram": object()})
    assert context.hook(event("我想了解一下 TiDB"), gateway) is None
    assert context.hook(event("1"), gateway) is None
    assert calls[0][:2] == ["search", "latest"]
    calls.clear()
    gateway._is_user_authorized = lambda source: False
    assert context.hook(event("by2kb TiDB"), gateway) is None
    assert not calls
    gateway._is_user_authorized = lambda source: True
    assert context.hook(event("by2kb TiDB", user=None), gateway) is None


@pytest.mark.asyncio
async def test_different_users_and_threads_get_different_scope(monkeypatch):
    scopes = []
    def run(args, **kwargs):
        scopes.append(args[args.index("--scope") + 1])
        return None
    monkeypatch.setattr(hermes, "_run_by2kb", run)
    context = Context()
    hermes.register(context)
    gateway = SimpleNamespace(_is_user_authorized=lambda source: True, adapters={"telegram": object()})
    for message in (event("1"), event("1", user="bob"), event("1", thread="other")):
        assert context.hook(message, gateway) is None
    assert len(set(scopes)) == 3
