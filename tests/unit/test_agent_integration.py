from pathlib import Path

import pytest
from typer.testing import CliRunner

from by2kb import cli
from by2kb.agent_install import install_hermes_plugin
from by2kb.config import Config, load_config
from by2kb.jobs.enrichment_service import (
    claim_external_enrichment,
    complete_external_enrichment,
)
from by2kb.jobs.model import Job, JobStatus
from by2kb.jobs.store import JobStore
from by2kb.integrations.hermes import _video_skill_path
from by2kb.normalize import from_asr_result
from by2kb.providers.asr import AsrResult
from by2kb.providers.base import SourceIdentity
from by2kb.setup import InitSettings, write_initial_config
from by2kb.writers.raw import content_hash, write_artifacts


def _settings(library_root: Path) -> InitSettings:
    return InitSettings(
        library_root=library_root,
        asr_provider="doubao_auc",
        tos_access_key="tos-key",
        tos_secret_key="tos-secret",
        tos_bucket="private-audio",
        doubao_api_key="asr-key",
    )


def test_init_settings_default_to_local_whisper(tmp_path):
    settings = InitSettings(library_root=tmp_path / "kb")

    settings.validate()

    assert settings.asr_provider == "faster_whisper"


def test_config_without_asr_selection_defaults_to_local_whisper(tmp_path):
    config = load_config(tmp_path / "empty-home")

    assert config.asr_provider == "faster_whisper"


def test_agent_local_preset_is_non_interactive_and_cloud_free(tmp_path):
    home = tmp_path / "home"
    library = tmp_path / "notes"

    result = CliRunner().invoke(
        cli.app,
        [
            "init",
            "--preset",
            "agent-local",
            "--home",
            str(home),
            "--library-root",
            str(library),
        ],
    )

    assert result.exit_code == 0, result.output
    config = (home / "config.toml").read_text(encoding="utf-8")
    secrets = (home / ".env").read_text(encoding="utf-8")
    assert 'providers = ["bilibili_native", "yt_dlp"]' in config
    assert 'provider = "faster_whisper"' in config
    assert 'executor = "external_agent"' in config
    assert "DOUBAO" not in secrets
    assert "VOLC" not in secrets
    assert library.is_dir()


def test_init_does_not_overwrite_personalized_configuration(tmp_path):
    home = tmp_path / "home"
    library = tmp_path / "notes"
    original = InitSettings(
        library_root=library,
        asr_provider="faster_whisper",
        whisper_model="personal-model",
        whisper_device="cpu",
        whisper_compute_type="int8",
    )
    config_path, env_path = write_initial_config(home, original)
    original_config = config_path.read_bytes()
    original_env = env_path.read_bytes()
    personal_skill = home / "skills" / "my-study" / "SKILL.md"
    personal_skill.parent.mkdir(parents=True)
    personal_skill.write_text("personal instructions", encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        ["init", "--preset", "agent-local", "--home", str(home)],
    )

    assert result.exit_code != 0
    assert config_path.read_bytes() == original_config
    assert env_path.read_bytes() == original_env
    assert personal_skill.read_text(encoding="utf-8") == "personal instructions"


def test_hermes_uses_personalized_skill_outside_managed_plugin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    personalized = home / "skills" / "video-to-knowledge" / "SKILL.md"
    personalized.parent.mkdir(parents=True)
    personalized.write_text("personal video workflow", encoding="utf-8")
    monkeypatch.setenv("BY2KB_HOME", str(home))
    monkeypatch.delenv("BY2KB_HERMES_SKILL", raising=False)

    assert _video_skill_path() == personalized


def test_init_writes_agent_first_configuration(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_path, env_path = write_initial_config(home, _settings(tmp_path / "kb"))

    assert config_path.is_file()
    assert env_path.is_file()
    assert 'executor = "external_agent"' in config_path.read_text(encoding="utf-8")
    assert "VOLC_SECRET_ACCESS_KEY=tos-secret" in env_path.read_text(encoding="utf-8")

    monkeypatch.setenv("BY2KB_HOME", str(home))
    config = load_config()
    assert config.library_root == tmp_path / "kb"
    assert config.resolved_enrichment_executor() == "external_agent"


def test_init_allows_local_whisper_without_cloud_asr_secrets(tmp_path):
    home = tmp_path / "home"
    settings = InitSettings(
        library_root=tmp_path / "kb",
        asr_provider="faster_whisper",
        enrichment_executor="disabled",
        whisper_model="large-v3",
        whisper_device="cpu",
        whisper_compute_type="int8",
    )

    config_path, env_path = write_initial_config(home, settings)

    assert 'provider = "faster_whisper"' in config_path.read_text(encoding="utf-8")
    rendered = config_path.read_text(encoding="utf-8")
    assert 'model = "large-v3"' in rendered
    assert 'device = "cpu"' in rendered
    assert 'compute_type = "int8"' in rendered
    assert "VOLC_ACCESS_KEY_ID" not in env_path.read_text(encoding="utf-8")
    assert settings.library_root.is_dir()


def test_load_config_preserves_asr_provider_options(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        """
[asr]
provider = "faster_whisper"
model = "large-v3"
device = "cpu"
compute_type = "int8"
vad_filter = true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("BY2KB_HOME", str(home))

    config = load_config()

    assert config.asr_provider == "faster_whisper"
    assert config.asr_options == {
        "model": "large-v3",
        "device": "cpu",
        "compute_type": "int8",
        "vad_filter": True,
    }


@pytest.mark.asyncio
async def test_external_agent_claim_and_complete(tmp_path):
    config = Config(
        home=tmp_path / "home",
        library_root=tmp_path / "library",
        db_path=tmp_path / "home" / "by2kb.db",
        enrichment_executor="external_agent",
    )
    identity = SourceIdentity(
        platform="bilibili",
        video_id="BV1agent123",
        canonical_url="https://www.bilibili.com/video/BV1agent123",
    )
    normalized = from_asr_result(
        identity,
        title="Agent integration",
        author="by2kb",
        duration_ms=1000,
        asr_result=AsrResult(provider="doubao_auc", model="bigmodel", text="正文"),
        fetched_at="2026-08-23T00:00:00Z",
    )
    inputs = write_artifacts(
        tmp_path / "published",
        source_payload={"view": {}},
        normalized=normalized,
    )
    store = JobStore(config.db_path)
    job = Job(
        id="job-agent",
        platform="bilibili",
        video_id="BV1agent123",
        status=JobStatus.ENRICHMENT_PENDING,
    )
    store.create_job(job)
    for kind, path in inputs.items():
        store.add_artifact(job.id, kind, str(path), content_hash(path))
    store.upsert_enrichment_task(
        job.id,
        status="pending",
        executor="external_agent",
        abstract_profile="short-video-abstract",
        study_profile="default-video-digest",
    )
    store.close()

    manifest = claim_external_enrichment(config, job.id)
    assert manifest["status"] == "claimed"
    assert set(manifest["outputs"]) == {"abstract_md", "updated_md"}
    assert "正文" in manifest["outputs"]["abstract_md"]["user_prompt"]

    abstract = tmp_path / "abstract-body.md"
    study = tmp_path / "study-body.md"
    abstract.write_text("值得阅读的短摘要。", encoding="utf-8")
    study.write_text("# 深度整理\n\n知识结构。", encoding="utf-8")
    result = await complete_external_enrichment(
        config,
        job.id,
        abstract_path=abstract,
        study_path=study,
        provider="hermes",
        model="host-model",
    )

    assert result.status == "completed"
    assert Path(result.artifacts["abstract_md"]).is_file()
    assert Path(result.artifacts["updated_md"]).is_file()
    assert "provider: hermes" in Path(result.artifacts["abstract_md"]).read_text(
        encoding="utf-8"
    )


def test_install_hermes_plugin_without_enabling(tmp_path):
    target = install_hermes_plugin(
        hermes_home=tmp_path / "hermes",
        enable=False,
    )
    assert (target / "plugin.yaml").is_file()
    assert (target / "__init__.py").is_file()
    assert (target / "skills" / "video-to-knowledge" / "SKILL.md").is_file()
