from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from by2kb.config import default_home
from by2kb.errors import ConfigError, TerminalProviderError, TransientProviderError
from by2kb.providers.asr import AsrOptions, AsrResult, AsrSegment
from by2kb.providers.base import LocalAudio

DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_DEVICE = "auto"
DEFAULT_COMPUTE_TYPE = "default"
REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")
CONFIG_KEYS = frozenset(
    {
        "model",
        "device",
        "compute_type",
        "model_dir",
        "vad_filter",
        "beam_size",
        "cpu_threads",
    }
)

WhisperModelFactory = Callable[["FasterWhisperConfig"], Any]


@dataclass(frozen=True)
class FasterWhisperConfig:
    model: str = DEFAULT_MODEL
    device: str = DEFAULT_DEVICE
    compute_type: str = DEFAULT_COMPUTE_TYPE
    model_root: Path = field(
        default_factory=lambda: default_home() / "models" / "faster-whisper"
    )
    vad_filter: bool = True
    beam_size: int = 5
    cpu_threads: int = 0

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, object] | None = None,
        *,
        home: Path | None = None,
    ) -> "FasterWhisperConfig":
        options = dict(values or {})
        unknown = sorted(set(options) - CONFIG_KEYS)
        if unknown:
            raise ConfigError(
                "unknown faster_whisper configuration: " + ", ".join(unknown)
            )

        def pick(key: str, default: object) -> object:
            env_name = f"BY2KB_WHISPER_{key.upper()}"
            return os.environ.get(env_name, options.get(key, default))

        model = str(pick("model", DEFAULT_MODEL)).strip()
        device = str(pick("device", DEFAULT_DEVICE)).strip().lower()
        compute_type = str(pick("compute_type", DEFAULT_COMPUTE_TYPE)).strip().lower()
        root_default = (home or default_home()) / "models" / "faster-whisper"
        model_root = Path(str(pick("model_dir", root_default))).expanduser()
        vad_filter = _parse_bool(pick("vad_filter", True), "vad_filter")
        beam_size = _parse_non_negative_int(pick("beam_size", 5), "beam_size", minimum=1)
        cpu_threads = _parse_non_negative_int(
            pick("cpu_threads", 0), "cpu_threads", minimum=0
        )

        if not model:
            raise ConfigError("faster_whisper model must not be empty")
        if device not in {"auto", "cpu", "cuda"}:
            raise ConfigError("faster_whisper device must be auto, cpu, or cuda")
        if not compute_type:
            raise ConfigError("faster_whisper compute_type must not be empty")
        return cls(
            model=model,
            device=device,
            compute_type=compute_type,
            model_root=model_root,
            vad_filter=vad_filter,
            beam_size=beam_size,
            cpu_threads=cpu_threads,
        )

    @property
    def model_path(self) -> Path:
        candidate = Path(self.model).expanduser()
        if candidate.is_absolute():
            return candidate
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "--", self.model).strip(".-")
        if not safe_name:
            raise ConfigError(f"invalid faster_whisper model name: {self.model}")
        return self.model_root / safe_name


class FasterWhisperAsrProvider:
    name = "faster_whisper"

    def __init__(
        self,
        config: FasterWhisperConfig,
        *,
        model_factory: WhisperModelFactory | None = None,
    ) -> None:
        self._config = config
        self._model_factory = model_factory or _load_model
        self._model = None

    async def transcribe(self, audio: LocalAudio, options: AsrOptions) -> AsrResult:
        path = Path(audio.path)
        if not path.is_file():
            raise TerminalProviderError(
                f"audio file not found: {path}", provider=self.name
            )
        if path.stat().st_size <= 0:
            raise TerminalProviderError("audio file is empty", provider=self.name)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._transcribe_sync, path, options),
                timeout=options.timeout_s,
            )
        except TimeoutError as exc:
            raise TransientProviderError(
                f"faster_whisper timed out after {options.timeout_s:.0f}s",
                provider=self.name,
            ) from exc
        except ConfigError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise TerminalProviderError(
                f"faster_whisper failed: {exc}", provider=self.name
            ) from exc

    def _transcribe_sync(self, path: Path, options: AsrOptions) -> AsrResult:
        model = self._model
        if model is None:
            model = self._model_factory(self._config)
            self._model = model
        generated, info = model.transcribe(
            str(path),
            language=options.language,
            beam_size=self._config.beam_size,
            vad_filter=self._config.vad_filter,
        )
        segments = [
            AsrSegment(start=float(item.start), end=float(item.end), text=str(item.text))
            for item in generated
            if str(item.text).strip()
        ]
        text = "".join(segment.text for segment in segments).strip()
        language = getattr(info, "language", None) or options.language
        provenance = {
            "media_source": "audio_fallback",
            "runtime": "faster-whisper",
            "device": self._config.device,
            "compute_type": self._config.compute_type,
            "vad_filter": self._config.vad_filter,
            "beam_size": self._config.beam_size,
            "model_source": (
                "local_path"
                if Path(self._config.model).expanduser().is_absolute()
                else "by2kb_cache"
            ),
        }
        language_probability = getattr(info, "language_probability", None)
        if language_probability is not None:
            provenance["language_probability"] = float(language_probability)
        duration_after_vad = getattr(info, "duration_after_vad", None)
        if duration_after_vad is not None:
            provenance["duration_after_vad_s"] = float(duration_after_vad)
        return AsrResult(
            provider=self.name,
            model=self._config.model,
            language=language,
            text=text,
            segments=segments,
            provenance=provenance,
        )


def faster_whisper_status(config: FasterWhisperConfig) -> dict[str, object]:
    model_path = config.model_path
    missing_files = [
        filename
        for filename in REQUIRED_MODEL_FILES
        if not (model_path / filename).is_file()
    ]
    return {
        "provider": "faster_whisper",
        "dependency_installed": find_spec("faster_whisper") is not None,
        "model": config.model,
        "model_path": str(model_path),
        "model_installed": model_path.is_dir() and not missing_files,
        "missing_files": missing_files,
        "device": config.device,
        "compute_type": config.compute_type,
    }


def require_faster_whisper_ready(config: FasterWhisperConfig) -> None:
    status = faster_whisper_status(config)
    if not status["dependency_installed"]:
        raise ConfigError(
            "faster_whisper requires its optional dependency: "
            "pipx inject by2kb 'faster-whisper>=1.2.1,<2'"
        )
    if not status["model_installed"]:
        raise ConfigError(
            f"faster_whisper model '{config.model}' is not installed; run: "
            f"by2kb models install {config.model}"
        )


def install_faster_whisper_model(config: FasterWhisperConfig) -> Path:
    if Path(config.model).expanduser().is_absolute():
        raise ConfigError(
            "cannot install an absolute local model path; "
            "select a model name such as large-v3-turbo"
        )
    if find_spec("faster_whisper") is None:
        raise ConfigError(
            "faster_whisper requires its optional dependency: "
            "pipx inject by2kb 'faster-whisper>=1.2.1,<2'"
        )
    try:
        from faster_whisper import download_model
    except ImportError as exc:
        raise ConfigError("failed to import faster_whisper") from exc
    target = config.model_path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        downloaded = download_model(config.model, output_dir=str(target))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"failed to download faster_whisper model: {exc}") from exc
    return Path(downloaded)


def _load_model(config: FasterWhisperConfig):
    require_faster_whisper_ready(config)
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ConfigError("failed to import faster_whisper") from exc
    return WhisperModel(
        str(config.model_path),
        device=config.device,
        compute_type=config.compute_type,
        cpu_threads=config.cpu_threads,
        local_files_only=True,
    )


def _parse_bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"faster_whisper {name} must be true or false")


def _parse_non_negative_int(value: object, name: str, *, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"faster_whisper {name} must be an integer") from exc
    if parsed < minimum:
        raise ConfigError(f"faster_whisper {name} must be at least {minimum}")
    return parsed
