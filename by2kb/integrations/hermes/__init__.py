"""Hermes gateway integration for by2kb."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

_VIDEO_URL = re.compile(
    r"https?://(?:www\.)?(?:bilibili\.com/video/[^\s]+|b23\.tv/[^\s]+)",
    re.IGNORECASE,
)


def register(ctx):
    skill = Path(__file__).parent / "skills" / "video-to-knowledge" / "SKILL.md"
    ctx.register_skill(
        "video-to-knowledge",
        skill,
        description="Transcribe and summarize a Bilibili video with by2kb.",
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

        task = _run_by2kb(["enrichment", "claim", job_id, "--json"])
        results = {}
        providers = []
        models = []
        for kind, max_tokens in (("abstract_md", 800), ("updated_md", 6000)):
            output = task["outputs"][kind]
            result = ctx.llm.complete(
                messages=[
                    {"role": "system", "content": output["system_prompt"]},
                    {"role": "user", "content": output["user_prompt"]},
                ],
                max_tokens=max_tokens,
                purpose=f"by2kb.{kind}",
            )
            results[kind] = result.text
            providers.append(str(result.provider or "hermes"))
            models.append(str(result.model or "host-model"))

        with tempfile.TemporaryDirectory(prefix="by2kb-") as temporary:
            root = Path(temporary)
            abstract_file = root / "abstract.md"
            study_file = root / "study.md"
            abstract_file.write_text(results["abstract_md"], encoding="utf-8")
            study_file.write_text(results["updated_md"], encoding="utf-8")
            complete = _run_by2kb(
                [
                    "enrichment",
                    "complete",
                    job_id,
                    "--abstract-file",
                    str(abstract_file),
                    "--study-file",
                    str(study_file),
                    "--provider",
                    ",".join(dict.fromkeys(providers)),
                    "--model",
                    ",".join(dict.fromkeys(models)),
                    "--json",
                ]
            )
        _send(
            loop,
            adapter,
            chat_id,
            _success_message(
                complete.get("artifacts") or {},
                results["abstract_md"],
            ),
            reply_to,
        )
    except Exception as exc:
        if job_id:
            try:
                _run_by2kb(
                    [
                        "enrichment",
                        "fail",
                        job_id,
                        "--message",
                        str(exc),
                        "--retryable",
                        "--json",
                    ]
                )
            except Exception:
                pass
        _send(loop, adapter, chat_id, f"视频处理失败：{exc}", reply_to)


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
