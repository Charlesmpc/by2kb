import httpx
import pytest

from by2kb.config import Config, LlmConfig
from by2kb.errors import TransientProviderError
from by2kb.jobs.runner import build_summary_artifacts
from by2kb.normalize import from_asr_result
from by2kb.providers.asr import AsrResult
from by2kb.providers.base import SourceIdentity
from by2kb.skills.model import find_skill
from by2kb.skills.runner import OpenAiCompatibleClient, build_prompts, run_skill
from by2kb.writers.updated import render_updated_md


def normalized_transcript():
    identity = SourceIdentity(
        platform="bilibili",
        video_id="BV1jmbD65EP2",
        canonical_url="https://www.bilibili.com/video/BV1jmbD65EP2/",
    )
    return from_asr_result(
        identity,
        title="测试视频",
        author="测试作者",
        duration_ms=60_000,
        asr_result=AsrResult(provider="test", model="asr", text="这是视频内容。"),
        fetched_at="2026-08-23T00:00:00Z",
    )


def test_packaged_summary_skills_are_available():
    abstract = find_skill("short-video-abstract", [])
    study = find_skill("default-video-digest", [])

    assert abstract is not None and "under a minute" in abstract.body
    assert study is not None and "Knowledge map" in study.body


def test_short_abstract_prompt_includes_grounding_and_source():
    skill = find_skill("short-video-abstract", [])
    system, user = build_prompts(skill, normalized_transcript(), "# raw\n\n正文")

    assert "Never invent" in system
    assert "short-video-abstract" in user
    assert "测试视频" in user
    assert "# raw" in user


async def test_openai_compatible_client_posts_chat_completion():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "# 摘要\n\n内容"}}]},
        )

    config = LlmConfig(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        model="test-model",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        llm = OpenAiCompatibleClient(config, client)
        skill = find_skill("short-video-abstract", [])
        result = await run_skill(skill, normalized_transcript(), "raw", llm)

    assert result == "# 摘要\n\n内容"
    assert str(requests[0].url) == "https://api.example.test/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert b'"model":"test-model"' in requests[0].content


async def test_openai_compatible_client_rejects_malformed_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    config = LlmConfig(api_key="key", base_url="https://example.test", model="model")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TransientProviderError, match="malformed"):
            await OpenAiCompatibleClient(config, client).complete("system", "user")


def test_summary_frontmatter_records_artifact_type():
    rendered = render_updated_md(
        normalized_transcript(),
        body="# 内容",
        skill_name="short-video-abstract",
        skill_version="1.0.0",
        model="test-model",
        provider="openai_compatible",
        artifact_type="short_abstract",
        raw_ref="raw.测试视频.md",
    )

    assert "artifact_type: short_abstract" in rendered
    assert "skills: short-video-abstract@1.0.0" in rendered
    assert "raw_ref: ./raw.测试视频.md" in rendered


async def test_build_summary_artifacts_creates_both_reading_depths(tmp_path):
    class RecordingLlm:
        provider = "test"
        model = "test-model"

        def __init__(self):
            self.prompts = []

        async def complete(self, system: str, user: str) -> str:
            self.prompts.append(user)
            return f"# Generated {len(self.prompts)}"

    raw_path = tmp_path / "raw.测试视频.md"
    raw_path.write_text("# raw\n\n正文", encoding="utf-8")
    config = Config(
        home=tmp_path / "home",
        library_root=tmp_path / "library",
        db_path=tmp_path / "jobs.db",
    )
    llm = RecordingLlm()

    artifacts = await build_summary_artifacts(
        config,
        normalized_transcript(),
        raw_path,
        tmp_path / "staging",
        llm,
    )

    assert set(artifacts) == {"abstract_md", "updated_md"}
    assert artifacts["abstract_md"].name == "short.测试视频.md"
    assert artifacts["updated_md"].name == "long.测试视频.md"
    assert "artifact_type: short_abstract" in artifacts["abstract_md"].read_text(
        encoding="utf-8"
    )
    assert "artifact_type: study_notes" in artifacts["updated_md"].read_text(
        encoding="utf-8"
    )
    assert "short-video-abstract" in llm.prompts[0]
    assert "default-video-digest" in llm.prompts[1]
