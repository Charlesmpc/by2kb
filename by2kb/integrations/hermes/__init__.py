"""Hermes gateway integration for by2kb."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

_VIDEO_URL = re.compile(
    r"https?://(?:"
    r"(?:www\.)?bilibili\.com/video/[^\s]+|b23\.tv/[^\s]+|"
    r"(?:(?:www|m|music)\.)?youtube\.com/"
    r"(?:watch\?[^\s]*v=|shorts/)[^\s]+|youtu\.be/[^\s]+)",
    re.IGNORECASE,
)


def register(ctx):
    skill = _video_skill_path()
    ctx.register_skill(
        "video-to-knowledge",
        skill,
        description="Transcribe and summarize a Bilibili or YouTube video with by2kb.",
    )

    def intercept(event, gateway, **kwargs):
        del kwargs
        match = _VIDEO_URL.search(event.text or "")
        if not match:
            return None
        try:
            if not gateway._is_user_authorized(event.source):
                return None
        except Exception:
            return None
        platform = event.source.platform
        adapter = gateway.adapters.get(platform)
        if adapter is None:
            return None
        loop = asyncio.get_running_loop()
        reply_to = getattr(event, "message_id", None)
        chat_id = event.source.chat_id
        _send(loop, adapter, chat_id, "收到，正在转录并整理这段视频。", reply_to)
        threading.Thread(
            target=_process,
            args=(ctx, loop, adapter, chat_id, reply_to, match.group(0)),
            name="by2kb-enrichment",
            daemon=True,
        ).start()
        return {"action": "skip", "reason": "by2kb-video"}

    ctx.register_hook("pre_gateway_dispatch", intercept)


def _video_skill_path() -> Path:
    configured = os.environ.get("BY2KB_HERMES_SKILL")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate
    home = Path(os.environ.get("BY2KB_HOME") or (Path.home() / ".by2kb"))
    personalized = home / "skills" / "video-to-knowledge" / "SKILL.md"
    if personalized.is_file():
        return personalized
    return Path(__file__).parent / "skills" / "video-to-knowledge" / "SKILL.md"


def _process(ctx, loop, adapter, chat_id, reply_to, url):
    job_id = None
    try:
        ingest = _run_by2kb(
            ["ingest", url, "--enricher", "external_agent", "--json"],
            allow_codes={0, 4},
        )
        job_id = ingest.get("job_id")
        if not job_id:
            raise RuntimeError(ingest.get("message") or "by2kb did not return a job id")
        artifacts = ingest.get("artifacts") or {}
        if ingest.get("status") in {"completed", "duplicate"} and {
            "abstract_md",
            "updated_md",
        }.issubset(artifacts):
            _send(
                loop,
                adapter,
                chat_id,
                _success_message(artifacts, _read_abstract(artifacts)),
                reply_to,
            )
            return

        complete = _run_staged_enrichment(ctx, job_id)
        _send(
            loop,
            adapter,
            chat_id,
            _success_message(
                complete.get("artifacts") or {},
                _read_abstract(complete.get("artifacts") or {}),
            ),
            reply_to,
        )
    except Exception as exc:
        if job_id:
            try:
                snapshot = _run_by2kb(["status", job_id, "--json"])
                if snapshot.get("state") == "completed":
                    artifacts = snapshot.get("artifacts") or {}
                    if isinstance(artifacts, list):
                        artifacts = {a["kind"]: a["path"] for a in artifacts}
                    _send(
                        loop, adapter, chat_id,
                        _success_message(artifacts, _read_abstract(artifacts)), reply_to,
                    )
                    return
                if snapshot.get("state") == "cancelled":
                    _send(loop, adapter, chat_id, "视频处理任务已取消。", reply_to)
                    return
            except Exception:
                pass
            # A host-side step failure is not authority to overwrite shared job
            # state: another worker may have advanced or completed it already.
            _send(
                loop, adapter, chat_id,
                f"本次处理步骤未完成，已保存的转写和摘要进度不会清除。"
                f"任务 {job_id}，请查询状态或重试。",
                reply_to,
            )
        else:
            _send(loop, adapter, chat_id, f"视频处理失败：{exc}", reply_to)


def _run_staged_enrichment(ctx, job_id):
    resyncs = 0
    provider = str(getattr(ctx.llm, "provider", None) or "hermes")
    model = str(getattr(ctx.llm, "model", None) or "host-profile")
    runtime_version = str(getattr(ctx.llm, "runtime_version", None) or "")
    identity = [
        "--provider",
        provider,
        "--model",
        model,
        "--runtime-version",
        runtime_version,
    ]
    for _operation_count in range(512):
        snapshot = _run_by2kb(["status", job_id, "--json"])
        if snapshot.get("terminal") and snapshot.get("state") != "completed":
            raise RuntimeError(
                snapshot.get("message") or f"by2kb job {snapshot.get('state')}"
            )
        step = _run_by2kb(
            ["enrichment", "next", job_id, *identity, "--json"]
        )
        if step.get("status") == "completed":
            return step
        operation = step.get("operation")
        if step.get("status") != "needs_input" or not isinstance(operation, dict):
            raise RuntimeError("by2kb returned an invalid Agent operation")
        operation = _load_operation(operation)
        result = _bounded_host_completion(ctx, operation)
        text = str(getattr(result, "text", "") or "")
        encoded = text.encode("utf-8")
        if not text.strip():
            raise RuntimeError("Hermes returned an empty enrichment operation")
        if len(encoded) > int(operation["max_output_bytes"]):
            raise RuntimeError("Hermes enrichment operation exceeded the output limit")
        with tempfile.TemporaryDirectory(prefix="by2kb-agent-") as temporary:
            output = Path(temporary) / "operation.md"
            output.write_text(text, encoding="utf-8")
            submitted = _run_by2kb(
                [
                    "enrichment",
                    "submit",
                    job_id,
                    "--operation-id",
                    str(operation["id"]),
                    "--output-file",
                    str(output),
                    *identity,
                    "--json",
                ]
            )
        submission_status = submitted.get("status")
        if submission_status == "completed":
            return submitted
        if submission_status == "operation_mismatch":
            resyncs += 1
            if resyncs > 3:
                raise RuntimeError(
                    "Agent operation state changed repeatedly; stop and query task status."
                )
            # Discard the old output. Obtain a fresh operation and generate its
            # own content; never attach old text to the new operation ID.
            continue
        if submission_status not in {"accepted", "already_accepted"}:
            raise RuntimeError(
                "Agent operation submission was not accepted; query task status."
            )
    raise RuntimeError("by2kb Agent enrichment exceeded 512 bounded operations")


def _bounded_host_completion(ctx, operation):
    max_tokens = 800 if "short-video-abstract" in operation["user_prompt"] else 6000
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        ctx.llm.complete,
        messages=[
            {"role": "system", "content": operation["system_prompt"]},
            {"role": "user", "content": operation["user_prompt"]},
        ],
        max_tokens=max_tokens,
        purpose=f"by2kb.operation.{str(operation['id'])[:12]}",
    )
    try:
        return future.result(timeout=int(operation["timeout_s"]))
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise RuntimeError("Hermes enrichment operation timed out") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _load_operation(ticket):
    if "request_file" not in ticket:
        return ticket  # Pre-0.5.3 CLI compatibility.
    with Path(ticket["request_file"]).open("rb") as stream:
        encoded = stream.read(32 * 1024 * 1024 + 1)
    if len(encoded) != ticket["request_bytes"] or len(encoded) > 32 * 1024 * 1024:
        raise RuntimeError("Agent request size mismatch")
    if hashlib.sha256(encoded).hexdigest() != ticket["request_sha256"]:
        raise RuntimeError("Agent request checksum mismatch")
    operation = json.loads(encoded)
    for key in ("id", "max_output_bytes", "timeout_s"):
        if operation.get(key) != ticket[key]:
            raise RuntimeError("Agent request identity or limits mismatch")
    if not all(isinstance(operation.get(k), str) for k in ("system_prompt", "user_prompt")):
        raise RuntimeError("Agent request prompts are invalid")
    return operation


def _run_by2kb(arguments, *, allow_codes={0}):
    executable = os.environ.get("BY2KB_COMMAND") or shutil.which("by2kb")
    if not executable:
        raise RuntimeError("by2kb command not found; install it with pipx first")
    completed = subprocess.run(
        [executable, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=7200,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode not in allow_codes:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail or f"by2kb exited with {completed.returncode}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("by2kb returned invalid JSON") from exc


def _send(loop, adapter, chat_id, content, reply_to):
    asyncio.run_coroutine_threadsafe(
        adapter.send(chat_id, content, reply_to=reply_to),
        loop,
    )


def _success_message(artifacts, abstract=""):
    lines = ["视频已经整理完成。"]
    if abstract.strip():
        lines.extend(["", "短摘要：", abstract.strip()])
    lines.extend(
        [
            "",
            "知识库文件：",
            f"- 逐字稿：{artifacts.get('raw_md', '未返回')}",
            f"- 短摘要：{artifacts.get('abstract_md', '未返回')}",
            f"- 深度整理：{artifacts.get('updated_md', '未返回')}",
        ]
    )
    return "\n".join(lines)


def _read_abstract(artifacts):
    path = Path(artifacts.get("abstract_md", ""))
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    if text.startswith("---"):
        _frontmatter, separator, body = text[3:].partition("\n---\n")
        if separator:
            return body.strip()
    return text.strip()
