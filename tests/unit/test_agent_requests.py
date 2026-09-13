import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from by2kb.agent_requests import operation_ticket, read_request_page
from by2kb.agent_runtime import AgentCallbackClient, AgentSessionStore
from by2kb.cli import app
from by2kb.errors import ConfigError
from by2kb.integrations.hermes import _load_operation
from by2kb.longform import TranscriptChunkPlanner, _chunk_text
from by2kb.config import LongFormConfig
from test_agent_provider import _external_job
from test_longform import _normalized


def test_million_character_request_is_lossless_and_ticket_is_small(tmp_path):
    operation = dict(id="operation", system_prompt="system", user_prompt="知识🙂\n" * 250_000,
                     timeout_s=600, max_output_bytes=1024)
    ticket = operation_ticket(tmp_path, operation)
    assert len(json.dumps(ticket).encode()) < 2000
    assert "user_prompt" not in ticket
    assert _load_operation(ticket) == operation
    assert json.loads(Path(ticket["request_file"]).read_text(encoding="utf-8")) == operation
    assert operation_ticket(tmp_path, operation) == ticket


def test_pages_reconstruct_unicode_and_detect_tampering(tmp_path):
    operation = dict(id="operation", system_prompt="指令", user_prompt='中文🙂\n"\\' * 1000,
                     timeout_s=600, max_output_bytes=1024)
    ticket = operation_ticket(tmp_path, operation)
    path = Path(ticket["request_file"])
    for field in ("system_prompt", "user_prompt"):
        offset, pages = 0, []
        while True:
            page = read_request_page(path, field, offset, 137)
            assert len(json.dumps(page).encode()) < 4000
            assert page["operation_id"] == operation["id"]
            pages.append(page["text"])
            if page["eof"]:
                break
            assert page["next_offset"] > offset
            offset = page["next_offset"]
        assert "".join(pages) == operation[field]
    for offset, limit in [(-1, 1), (0, 0), (0, 2001), (100000, 1)]:
        with pytest.raises(ConfigError):
            read_request_page(path, "user_prompt", offset, limit)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ConfigError, match="checksum"):
        read_request_page(path, "user_prompt", 0, 100)
    with pytest.raises(RuntimeError, match="size"):
        _load_operation(ticket)


def test_million_character_single_segment_is_covered_without_truncation():
    normalized = _normalized(1)
    original = "知识学习" * 250_000
    normalized.transcript.segments[0].text = original
    plan = TranscriptChunkPlanner(LongFormConfig()).plan(normalized)
    assert len(plan.chunks) > 200
    assert all(chunk.estimated_tokens <= 4000 for chunk in plan.chunks)
    pieces = [_chunk_text(normalized, chunk).split("] ", 1)[1] for chunk in plan.chunks]
    assert "".join(pieces) == original
    assert normalized.transcript.segments[0].text == original
    assert all(left.char_end == right.char_start for left, right in zip(plan.chunks, plan.chunks[1:]))


def test_cli_file_backed_cycle_and_legacy_compatibility(tmp_path, monkeypatch):
    config, job = _external_job(tmp_path, long=True)
    monkeypatch.setattr("by2kb.cli.load_config", lambda: config)
    runner = CliRunner()
    identity = ["--provider", "hermes", "--model", "test", "--json"]
    def run(args):
        result = runner.invoke(app, ["enrichment", *args])
        assert result.exit_code == 0, result.output
        assert len(result.stdout.encode()) < 12000
        return json.loads(result.stdout)
    claim = run(["claim", job, "--json"])
    assert claim["schema_version"] == 2 and "outputs" not in claim
    claim_body = json.loads(Path(claim["request_file"]).read_text(encoding="utf-8"))
    assert "outputs" in claim_body
    operations = []
    for i in range(40):
        step = run(["next", job, *identity])
        if step["status"] == "completed":
            break
        ticket = step["operation"]
        assert step["schema_version"] == 2
        operation = _load_operation(ticket)
        legacy = run(["next", job, *identity, "--inline-prompts"])
        assert legacy["operation"] == operation
        page = run(["read", "--request-file", ticket["request_file"], "--json"])
        assert page["operation_id"] == ticket["id"]
        output = tmp_path / "response.md"
        output.write_text(f"Grounded intermediate note {i}", encoding="utf-8")
        accepted = run(["submit", job, *identity, "--operation-id", ticket["id"], "--output-file", str(output)])
        assert accepted["status"] == "accepted"
        operations.append(ticket["id"])
    assert step["status"] == "completed"
    assert len(operations) > 2
    assert {"raw_md", "abstract_md", "updated_md"} <= step["artifacts"].keys()


@pytest.mark.asyncio
async def test_oversized_agent_prompt_fails_without_truncating_or_saving(tmp_path):
    session = AgentSessionStore(tmp_path, provider="host", model="test", runtime_version="")
    with pytest.raises(ConfigError, match="No input was truncated"):
        await AgentCallbackClient(session).complete("system", "知" * 24001)
    assert not session.path.exists()
