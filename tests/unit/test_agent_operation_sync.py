from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from by2kb.agent_runtime import AgentOperation, AgentSessionStore, OperationConflict
from by2kb.errors import ConfigError
from by2kb.integrations import hermes
from by2kb.jobs.enrichment_service import (
    fail_external_enrichment,
    next_external_enrichment_operation,
    submit_external_enrichment_operation,
)
from by2kb.jobs.model import JobStatus
from by2kb.jobs.store import JobStore
from by2kb.operation_lock import exclusive_file
from test_agent_provider import _external_job


def test_session_duplicate_preserves_new_pending_and_rejects_changed_content(tmp_path):
    session = AgentSessionStore(tmp_path, provider="hermes", model="test", runtime_version="")
    first = AgentOperation("first", "system", "first prompt")
    second = AgentOperation("second", "system", "second prompt")
    session.set_pending(first)
    assert session.submit("first", "first answer") == "accepted"
    assert session.submit("first", "first answer") == "already_accepted"
    session.set_pending(second)
    before = session.path.read_bytes()
    assert session.submit("first", "first answer") == "already_accepted"
    with pytest.raises(OperationConflict, match="operation_content_conflict"):
        session.submit("first", "different answer")
    with pytest.raises(OperationConflict, match="operation_mismatch") as error:
        session.submit("unknown", "old answer")
    assert error.value.expected_id == "second"
    assert session.path.read_bytes() == before
    assert session.pending() == second


def test_lock_excludes_other_process_and_releases_on_exit(tmp_path):
    path = tmp_path / "operation.lock"
    script = (
        "from pathlib import Path\n"
        "from by2kb.operation_lock import exclusive_file\n"
        "from by2kb.errors import ConfigError\n"
        "import sys\n"
        "try:\n"
        "    with exclusive_file(Path(sys.argv[1])): pass\n"
        "except ConfigError: sys.exit(23)\n"
    )
    def probe():
        return subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True, text=True, timeout=15,
        )
    with exclusive_file(path):
        result = probe()
        assert result.returncode == 23, result.stderr
    result = probe()
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_job_lock_blocks_next_and_fail_without_mutation(tmp_path):
    config, job_id = _external_job(tmp_path, long=False)
    key = hashlib.sha256(job_id.encode()).hexdigest()
    with exclusive_file(config.home / "locks" / f"enrichment-{key}.lock"):
        with pytest.raises(ConfigError, match="busy"):
            await next_external_enrichment_operation(config, job_id, provider="hermes", model="test")
        with pytest.raises(ConfigError, match="busy"):
            fail_external_enrichment(config, job_id, message="concurrent failure")
    store = JobStore(config.db_path)
    try:
        assert store.get_job(job_id).status == JobStatus.ENRICHMENT_PENDING
        assert store.get_enrichment_task(job_id)["status"] == "pending"
    finally:
        store.close()
    step = await next_external_enrichment_operation(config, job_id, provider="hermes", model="test")
    assert step["status"] == "needs_input"


@pytest.mark.asyncio
async def test_stale_submit_and_late_failure_preserve_completed_job(tmp_path):
    config, job_id = _external_job(tmp_path, long=False)
    identity = dict(provider="hermes", model="test")
    output = tmp_path / "output.md"
    output.write_text("# Grounded summary", encoding="utf-8")
    first = await next_external_enrichment_operation(config, job_id, **identity)
    rejected = submit_external_enrichment_operation(
        config, job_id, operation_id="stale", output_path=output, **identity
    )
    assert rejected["status"] == "operation_mismatch"
    assert rejected["error"]["expected_operation_id"] == first["operation"]["id"]
    store = JobStore(config.db_path)
    try:
        assert store.get_job(job_id).status == JobStatus.ENRICHING
    finally:
        store.close()
    step = first
    for _ in range(10):
        if step["status"] == "completed":
            break
        operation_id = step["operation"]["id"]
        accepted = submit_external_enrichment_operation(
            config, job_id, operation_id=operation_id, output_path=output, **identity
        )
        assert accepted["status"] == "accepted"
        step = await next_external_enrichment_operation(config, job_id, **identity)
    assert step["status"] == "completed"
    before = {kind: Path(path).read_bytes() for kind, path in step["artifacts"].items()}
    late = fail_external_enrichment(config, job_id, message="stale worker failed")
    assert late.status == "completed"
    repeated = submit_external_enrichment_operation(
        config, job_id, operation_id=operation_id, output_path=output, **identity
    )
    assert repeated["status"] == "completed"
    output.write_text("# Changed summary", encoding="utf-8")
    rejected = submit_external_enrichment_operation(
        config, job_id, operation_id=operation_id, output_path=output, **identity
    )
    assert rejected["status"] == "operation_content_conflict"
    store = JobStore(config.db_path)
    try:
        assert store.get_job(job_id).status == JobStatus.COMPLETED
        assert store.get_enrichment_task(job_id)["status"] == "completed"
    finally:
        store.close()
    assert {kind: Path(path).read_bytes() for kind, path in step["artifacts"].items()} == before


def test_late_failure_does_not_overwrite_cancelled_job(tmp_path):
    config, job_id = _external_job(tmp_path)
    store = JobStore(config.db_path)
    try:
        store.update_status(job_id, JobStatus.CANCELLED)
        assert fail_external_enrichment(config, job_id, message="late").status == "cancelled"
        assert store.get_job(job_id).status == JobStatus.CANCELLED
    finally:
        store.close()


@pytest.mark.parametrize("always_conflict", [False, True])
def test_hermes_resync_generates_fresh_content_and_is_bounded(monkeypatch, always_conflict):
    prompts, submitted = [], []
    def run(arguments, **kwargs):
        if arguments[0] == "status":
            return {"state": "enriching", "terminal": False}
        if arguments[1] == "next":
            number = len(submitted)
            return {"status": "needs_input", "operation": {
                "id": str(number), "system_prompt": "system",
                "user_prompt": f"prompt-{number}", "max_output_bytes": 1024,
                "timeout_s": 10,
            }}
        assert arguments[1] == "submit"
        submitted.append((
            arguments[arguments.index("--operation-id") + 1],
            Path(arguments[arguments.index("--output-file") + 1]).read_text(encoding="utf-8"),
        ))
        return {"status": "operation_mismatch" if always_conflict or len(submitted) == 1 else "completed"}
    def complete(ctx, operation):
        prompts.append(operation["user_prompt"])
        return SimpleNamespace(text=operation["user_prompt"] + " answer")
    monkeypatch.setattr(hermes, "_run_by2kb", run)
    monkeypatch.setattr(hermes, "_bounded_host_completion", complete)
    ctx = SimpleNamespace(llm=SimpleNamespace())
    if always_conflict:
        with pytest.raises(RuntimeError, match="changed repeatedly"):
            hermes._run_staged_enrichment(ctx, "job")
        assert len(submitted) == 4
    else:
        assert hermes._run_staged_enrichment(ctx, "job")["status"] == "completed"
        assert submitted == [("0", "prompt-0 answer"), ("1", "prompt-1 answer")]


@pytest.mark.parametrize("state,phrase", [
    ("completed", "视频已经整理完成"),
    ("enriching", "本次处理步骤未完成"),
    ("cancelled", "已取消"),
])
def test_hermes_checks_status_before_notifying_and_never_marks_job_failed(monkeypatch, state, phrase):
    calls, messages = [], []
    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "ingest":
            return {"job_id": "job", "status": "enrichment_pending"}
        assert arguments[0] == "status"
        return {"state": state, "artifacts": []}
    def fail(*args):
        raise RuntimeError("operation ID mismatch")
    monkeypatch.setattr(hermes, "_run_by2kb", run)
    monkeypatch.setattr(hermes, "_run_staged_enrichment", fail)
    monkeypatch.setattr(hermes, "_send", lambda loop, adapter, chat, content, reply: messages.append(content))
    hermes._process(None, None, None, "chat", None, "https://b23.tv/example")
    assert len(messages) == 1
    assert phrase in messages[0]
    assert all("fail" not in call for call in calls)
