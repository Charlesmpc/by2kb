from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from by2kb.errors import ConfigError, TerminalProviderError
from by2kb.providers import asr_faster_whisper as whisper_module
from by2kb.providers.asr import AsrOptions
from by2kb.providers.asr_faster_whisper import (
    FasterWhisperAsrProvider,
    FasterWhisperConfig,
    faster_whisper_status,
    install_faster_whisper_model,
    require_faster_whisper_ready,
)
from by2kb.providers.base import LocalAudio


def _clear_whisper_env(monkeypatch):
    for name in (
        "MODEL",
        "DEVICE",
        "COMPUTE_TYPE",
        "MODEL_DIR",
        "VAD_FILTER",
        "BEAM_SIZE",
        "CPU_THREADS",
    ):
        monkeypatch.delenv(f"BY2KB_WHISPER_{name}", raising=False)


def _install_fake_model(config: FasterWhisperConfig) -> None:
    config.model_path.mkdir(parents=True)
    for filename in whisper_module.REQUIRED_MODEL_FILES:
        (config.model_path / filename).write_text("fixture", encoding="utf-8")


def test_config_uses_mapping_and_environment_overrides(tmp_path, monkeypatch):
    _clear_whisper_env(monkeypatch)
    monkeypatch.setenv("BY2KB_WHISPER_DEVICE", "cpu")
    config = FasterWhisperConfig.from_mapping(
        {
            "model": "large-v3",
            "device": "cuda",
            "compute_type": "int8",
            "vad_filter": "false",
            "beam_size": 3,
            "cpu_threads": 4,
        },
        home=tmp_path,
    )

    assert config.model == "large-v3"
    assert config.device == "cpu"
    assert config.compute_type == "int8"
    assert config.vad_filter is False
    assert config.beam_size == 3
    assert config.cpu_threads == 4
    assert config.model_path == tmp_path / "models" / "faster-whisper" / "large-v3"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"device": "tpu"}, "device must be"),
        ({"vad_filter": "sometimes"}, "vad_filter must be true or false"),
        ({"beam_size": 0}, "beam_size must be at least 1"),
        ({"cpu_threads": -1}, "cpu_threads must be at least 0"),
        ({"typo": True}, "unknown faster_whisper configuration: typo"),
    ],
)
def test_config_rejects_invalid_options(tmp_path, monkeypatch, options, message):
    _clear_whisper_env(monkeypatch)
    with pytest.raises(ConfigError, match=message):
        FasterWhisperConfig.from_mapping(options, home=tmp_path)


def test_status_reports_dependency_and_model_files(tmp_path, monkeypatch):
    _clear_whisper_env(monkeypatch)
    config = FasterWhisperConfig.from_mapping(home=tmp_path)
    monkeypatch.setattr(whisper_module, "find_spec", lambda _name: object())

    missing = faster_whisper_status(config)
    assert missing["dependency_installed"] is True
    assert missing["model_installed"] is False
    assert "model.bin" in missing["missing_files"]

    _install_fake_model(config)
    installed = faster_whisper_status(config)
    assert installed["model_installed"] is True
    assert installed["missing_files"] == []


def test_absolute_model_path_is_used_without_rewriting(tmp_path, monkeypatch):
    _clear_whisper_env(monkeypatch)
    model_path = (tmp_path / "custom-model").resolve()
    config = FasterWhisperConfig.from_mapping(
        {"model": str(model_path)},
        home=tmp_path / "home",
    )
    monkeypatch.setattr(whisper_module, "find_spec", lambda _name: object())
    _install_fake_model(config)

    assert config.model_path == model_path
    assert faster_whisper_status(config)["model_installed"] is True


def test_ready_check_never_downloads_implicitly(tmp_path, monkeypatch):
    _clear_whisper_env(monkeypatch)
    config = FasterWhisperConfig.from_mapping(home=tmp_path)
    monkeypatch.setattr(whisper_module, "find_spec", lambda _name: object())

    with pytest.raises(ConfigError, match="by2kb models install"):
        require_faster_whisper_ready(config)


def test_explicit_install_uses_selected_cache_directory(tmp_path, monkeypatch):
    _clear_whisper_env(monkeypatch)
    config = FasterWhisperConfig.from_mapping({"model": "large-v3"}, home=tmp_path)
    calls = []

    def download_model(model, *, output_dir):
        calls.append((model, output_dir))
        target = Path(output_dir)
        target.mkdir(parents=True)
        for filename in whisper_module.REQUIRED_MODEL_FILES:
            (target / filename).write_text("fixture", encoding="utf-8")
        return str(target)

    monkeypatch.setattr(whisper_module, "find_spec", lambda _name: object())
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(download_model=download_model),
    )

    installed = install_faster_whisper_model(config)

    assert installed == config.model_path
    assert calls == [("large-v3", str(config.model_path))]


def test_explicit_install_rejects_an_absolute_local_model_path(tmp_path, monkeypatch):
    _clear_whisper_env(monkeypatch)
    config = FasterWhisperConfig.from_mapping(
        {"model": str((tmp_path / "custom-model").resolve())},
        home=tmp_path / "home",
    )
    monkeypatch.setattr(whisper_module, "find_spec", lambda _name: object())

    with pytest.raises(ConfigError, match="cannot install an absolute local model path"):
        install_faster_whisper_model(config)


@pytest.mark.asyncio
async def test_provider_returns_timestamped_segments_and_provenance(
    tmp_path, monkeypatch
):
    _clear_whisper_env(monkeypatch)
    config = FasterWhisperConfig.from_mapping(
        {"model": "large-v3", "device": "cpu", "compute_type": "int8"},
        home=tmp_path,
    )
    audio_path = tmp_path / "speech.wav"
    audio_path.write_bytes(b"audio")
    calls = []

    class FakeModel:
        def transcribe(self, path, **options):
            calls.append((path, options))
            segments = [
                SimpleNamespace(start=0.0, end=1.25, text="你好，"),
                SimpleNamespace(start=1.25, end=2.5, text="世界。"),
            ]
            info = SimpleNamespace(
                language="zh",
                language_probability=0.98,
                duration_after_vad=2.5,
            )
            return iter(segments), info

    provider = FasterWhisperAsrProvider(config, model_factory=lambda _config: FakeModel())
    result = await provider.transcribe(
        LocalAudio(
            path=audio_path,
            format="wav",
            duration_s=2.5,
            size_bytes=audio_path.stat().st_size,
        ),
        AsrOptions(timeout_s=5),
    )

    assert result.provider == "faster_whisper"
    assert result.model == "large-v3"
    assert result.language == "zh"
    assert result.text == "你好，世界。"
    assert [(segment.start, segment.end) for segment in result.segments] == [
        (0.0, 1.25),
        (1.25, 2.5),
    ]
    assert result.provenance["device"] == "cpu"
    assert result.provenance["compute_type"] == "int8"
    assert result.provenance["model_source"] == "by2kb_cache"
    assert "model_path" not in result.provenance
    assert result.provenance["language_probability"] == 0.98
    assert calls[0][1]["vad_filter"] is True


@pytest.mark.asyncio
async def test_provider_rejects_empty_audio(tmp_path, monkeypatch):
    _clear_whisper_env(monkeypatch)
    audio_path = tmp_path / "empty.wav"
    audio_path.touch()
    provider = FasterWhisperAsrProvider(
        FasterWhisperConfig.from_mapping(home=tmp_path),
        model_factory=lambda _config: None,
    )

    with pytest.raises(TerminalProviderError, match="audio file is empty"):
        await provider.transcribe(
            LocalAudio(path=audio_path, format="wav", size_bytes=0),
            AsrOptions(),
        )
