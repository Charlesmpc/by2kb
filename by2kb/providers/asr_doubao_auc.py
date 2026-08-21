from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from by2kb.errors import ConfigError, TerminalProviderError, TransientProviderError
from by2kb.providers.asr import AsrOptions, AsrResult
from by2kb.providers.base import LocalAudio

MAX_AUDIO_BYTES = 25 * 1024 * 1024
SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
STATUS_SUCCESS = "20000000"
STATUS_PROCESSING = "20000001"
STATUS_SILENT = "20000003"
CHUNK_THRESHOLD_S = 75.0
CHUNK_SECONDS = 60
MAX_CONCURRENT_CHUNKS = 2

_FORMATS = {
    "ogg": ("ogg", "opus", "audio/ogg"),
    "oga": ("ogg", "opus", "audio/ogg"),
    "opus": ("ogg", "opus", "audio/ogg"),
    "mp3": ("mp3", None, "audio/mpeg"),
    "wav": ("wav", None, "audio/wav"),
    "m4a": ("mp4", None, "audio/mp4"),
    "mp4": ("mp4", None, "audio/mp4"),
    "flac": ("flac", None, "audio/flac"),
}


@dataclass
class DoubaoAucConfig:
    access_key: str
    secret_key: str
    bucket: str
    app_id: str
    access_token: str
    region: str = "ap-southeast-1"
    endpoint: str = ""
    resource_id: str = "volc.seedasr.auc"

    @classmethod
    def from_env(cls) -> "DoubaoAucConfig":
        def required(name: str) -> str:
            value = os.environ.get(name)
            if not value:
                raise ConfigError(f"missing required environment variable: {name}")
            return value

        region = os.environ.get("TOS_REGION") or "ap-southeast-1"
        endpoint = os.environ.get("TOS_S3_ENDPOINT") or f"tos-s3-{region}.volces.com"
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"https://{endpoint}"
        return cls(
            access_key=required("VOLC_ACCESS_KEY_ID"),
            secret_key=required("VOLC_SECRET_ACCESS_KEY"),
            bucket=required("TOS_BUCKET"),
            app_id=required("DOUBAO_APPID"),
            access_token=required("DOUBAO_ACCESS_TOKEN"),
            region=region,
            endpoint=endpoint,
            resource_id=os.environ.get("DOUBAO_RESOURCE_ID") or "volc.seedasr.auc",
        )


def _describe(audio_format: str) -> tuple[str, str | None, str]:
    key = audio_format.lower().lstrip(".")
    if key not in _FORMATS:
        raise TerminalProviderError(
            f"unsupported audio format: {audio_format}", provider="doubao_auc"
        )
    return _FORMATS[key]


def _headers(config: DoubaoAucConfig, request_id: str, *, submit: bool) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Api-App-Key": config.app_id,
        "X-Api-Access-Key": config.access_token,
        "X-Api-Resource-Id": config.resource_id,
        "X-Api-Request-Id": request_id,
    }
    if submit:
        headers["X-Api-Sequence"] = "-1"
    return headers


def _response_status(response: httpx.Response) -> tuple[str | None, str | None]:
    return response.headers.get("X-Api-Status-Code"), response.headers.get("X-Api-Message")


class DoubaoAucAsrProvider:
    name = "doubao_auc"
    model = "bigmodel"

    def __init__(self, config: DoubaoAucConfig, client: httpx.AsyncClient | None = None):
        self._config = config
        self._client = client
        self._s3 = None

    def _boto3_client(self):
        if self._s3 is None:
            try:
                import boto3
                from botocore.client import Config as BotoConfig
            except ImportError as exc:
                raise ConfigError(
                    "doubao_auc requires boto3: pip install by2kb[asr-doubao]"
                ) from exc
            self._s3 = boto3.client(
                "s3",
                region_name=self._config.region,
                aws_access_key_id=self._config.access_key,
                aws_secret_access_key=self._config.secret_key,
                endpoint_url=self._config.endpoint,
                config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
            )
        return self._s3

    async def transcribe(self, audio: LocalAudio, options: AsrOptions) -> AsrResult:
        path = Path(audio.path)
        if not path.is_file():
            raise TerminalProviderError(f"audio file not found: {path}", provider=self.name)
        size = path.stat().st_size
        if size <= 0:
            raise TerminalProviderError("audio file is empty", provider=self.name)
        if size > MAX_AUDIO_BYTES:
            raise TerminalProviderError(
                f"audio file exceeds {MAX_AUDIO_BYTES // (1024 * 1024)} MiB limit",
                provider=self.name,
            )

        duration = audio.duration_s or await _probe_duration(path)
        if duration is not None and duration > CHUNK_THRESHOLD_S:
            text = await self._transcribe_chunked(path, options)
        else:
            text = await self._transcribe_one(path, audio.format, options)
        return AsrResult(
            provider=self.name,
            model=self.model,
            language=options.language,
            text=text,
            segments=[],
            provenance={
                "media_source": "audio_fallback",
                "staging": "private_tos_presigned_url",
            },
        )

    async def _transcribe_chunked(self, path: Path, options: AsrOptions) -> str:
        with tempfile.TemporaryDirectory(prefix="by2kb-doubao-asr-") as temp_dir:
            pattern = str(Path(temp_dir) / "chunk-%03d.ogg")
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-y", "-i", str(path),
                "-map", "0:a:0", "-c:a", "libopus", "-b:a", "32k",
                "-f", "segment", "-segment_time", str(CHUNK_SECONDS), pattern,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise TerminalProviderError(
                    f"failed to split long audio: {stderr.decode('utf-8', 'replace').strip()}",
                    provider=self.name,
                )
            chunks = sorted(Path(temp_dir).glob("chunk-*.ogg"))
            if not chunks:
                raise TerminalProviderError("ffmpeg produced no audio chunks", provider=self.name)

            semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHUNKS)
            chunk_options = options.model_copy(
                update={"timeout_s": min(options.timeout_s, 150.0)}
            )

            async def run(chunk: Path) -> str:
                async with semaphore:
                    return await self._transcribe_one(chunk, "ogg", chunk_options)

            texts = await asyncio.gather(*(run(chunk) for chunk in chunks))
            return "".join(text.strip() for text in texts if text.strip())

    async def _transcribe_one(self, path: Path, audio_format: str, options: AsrOptions) -> str:
        config = self._config
        fmt, codec, content_type = _describe(audio_format)
        s3 = self._boto3_client()
        object_key = f"by2kb-audio/{time.strftime('%Y%m%d')}/{uuid.uuid4().hex}{path.suffix.lower()}"
        uploaded = False

        try:
            await asyncio.to_thread(
                s3.put_object,
                Bucket=config.bucket,
                Key=object_key,
                Body=path.read_bytes(),
                ContentType=content_type,
            )
            uploaded = True
            presigned_url = await asyncio.to_thread(
                s3.generate_presigned_url,
                "get_object",
                Params={"Bucket": config.bucket, "Key": object_key},
                ExpiresIn=600,
            )

            request_id = str(uuid.uuid4())
            audio_payload: dict = {"format": fmt, "url": presigned_url}
            if codec:
                audio_payload["codec"] = codec
            payload = {
                "user": {"uid": "by2kb"},
                "audio": audio_payload,
                "request": {
                    "model_name": self.model,
                    "enable_itn": True,
                    "enable_punc": True,
                },
            }
            client = self._client or httpx.AsyncClient(timeout=30)
            try:
                submitted = await client.post(
                    SUBMIT_URL,
                    headers=_headers(config, request_id, submit=True),
                    content=json.dumps(payload),
                )
                status_code, message = _response_status(submitted)
                if status_code != STATUS_SUCCESS:
                    raise TerminalProviderError(
                        f"doubao submit failed: {status_code or submitted.status_code} "
                        f"{message or 'unknown error'}",
                        provider=self.name,
                    )

                deadline = time.monotonic() + options.timeout_s
                while time.monotonic() < deadline:
                    await asyncio.sleep(1.5)
                    queried = await client.post(
                        QUERY_URL,
                        headers=_headers(config, request_id, submit=False),
                        content="{}",
                    )
                    status_code, message = _response_status(queried)
                    if status_code == STATUS_SUCCESS:
                        result = queried.json().get("result") or {}
                        return str(result.get("text") or "").strip()
                    if status_code == STATUS_PROCESSING:
                        continue
                    if status_code == STATUS_SILENT:
                        return ""
                    raise TerminalProviderError(
                        f"doubao query failed: {status_code or queried.status_code} "
                        f"{message or 'unknown error'}",
                        provider=self.name,
                    )
                raise TransientProviderError(
                    f"doubao query timed out after {options.timeout_s:g} seconds",
                    provider=self.name,
                )
            except httpx.HTTPError as exc:
                raise TransientProviderError(
                    f"doubao request failed: {exc}", provider=self.name
                ) from exc
        finally:
            if uploaded:
                try:
                    await asyncio.to_thread(
                        s3.delete_object, Bucket=config.bucket, Key=object_key
                    )
                except Exception:
                    pass


async def _probe_duration(path: Path) -> float | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        return float(stdout.decode().strip())
    except (OSError, ValueError):
        return None
