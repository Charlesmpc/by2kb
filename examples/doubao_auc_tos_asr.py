#!/usr/bin/env python3
"""Standalone Doubao AUC file-transcription adapter using private TOS staging.

Flow: local audio -> private Volcengine TOS object -> short-lived presigned URL
-> Doubao AUC submit/query -> transcript on stdout. The temporary TOS object
is deleted in a finally block on both success and failure.

This is a reference implementation for by2kb, not yet a wired provider.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import boto3
import requests
from botocore.client import Config

MAX_AUDIO_BYTES = 25 * 1024 * 1024
SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"


def load_env_file(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Environment file not found: {path}") from exc
    return values


def required(name: str, values: dict[str, str]) -> str:
    value = os.environ.get(name) or values.get(name)
    if not value:
        raise RuntimeError(f"Missing required setting: {name}")
    return value


def audio_description(path: Path) -> tuple[str, str | None, str]:
    ext = path.suffix.lower()
    if ext in {".ogg", ".oga", ".opus"}:
        return "ogg", "opus", "audio/ogg"
    if ext == ".mp3":
        return "mp3", None, "audio/mpeg"
    if ext == ".wav":
        return "wav", None, "audio/wav"
    if ext in {".m4a", ".mp4"}:
        return "mp4", None, "audio/mp4"
    if ext == ".flac":
        return "flac", None, "audio/flac"
    raise RuntimeError(f"Unsupported audio format: {ext or '<none>'}")


def api_headers(
    app_id: str,
    access_token: str,
    resource_id: str,
    request_id: str,
    *,
    submit: bool,
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Api-App-Key": app_id,
        "X-Api-Access-Key": access_token,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
    }
    if submit:
        headers["X-Api-Sequence"] = "-1"
    return headers


def response_status(response: requests.Response) -> tuple[str | None, str | None]:
    return response.headers.get("X-Api-Status-Code"), response.headers.get("X-Api-Message")


def transcribe(audio_path: Path, env_file: Path | None, timeout: float) -> str:
    if not audio_path.is_file():
        raise RuntimeError(f"Audio file not found: {audio_path}")
    size = audio_path.stat().st_size
    if size <= 0:
        raise RuntimeError("Audio file is empty")
    if size > MAX_AUDIO_BYTES:
        raise RuntimeError(f"Audio file exceeds {MAX_AUDIO_BYTES // (1024 * 1024)} MB limit")

    audio_format, audio_codec, content_type = audio_description(audio_path)
    values = load_env_file(env_file)

    access_key = required("VOLC_ACCESS_KEY_ID", values)
    secret_key = required("VOLC_SECRET_ACCESS_KEY", values)
    region = (os.environ.get("TOS_REGION") or values.get("TOS_REGION") or "ap-southeast-1").strip()
    bucket = required("TOS_BUCKET", values)
    endpoint = (
        os.environ.get("TOS_S3_ENDPOINT")
        or values.get("TOS_S3_ENDPOINT")
        or f"tos-s3-{region}.volces.com"
    ).strip()
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint

    app_id = required("DOUBAO_APPID", values)
    access_token = required("DOUBAO_ACCESS_TOKEN", values)
    resource_id = (
        os.environ.get("DOUBAO_RESOURCE_ID")
        or values.get("DOUBAO_RESOURCE_ID")
        or "volc.seedasr.auc"
    ).strip()

    s3 = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        endpoint_url=endpoint,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )

    object_key = f"by2kb-audio/{time.strftime('%Y%m%d')}/{uuid.uuid4().hex}{audio_path.suffix.lower()}"
    uploaded = False
    transcript = ""
    cleanup_error: Exception | None = None

    try:
        with audio_path.open("rb") as audio_file:
            s3.put_object(
                Bucket=bucket,
                Key=object_key,
                Body=audio_file,
                ContentType=content_type,
            )
        uploaded = True

        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=600,
        )

        request_id = str(uuid.uuid4())
        audio: dict[str, object] = {"format": audio_format, "url": presigned_url}
        if audio_codec:
            audio["codec"] = audio_codec
        payload = {
            "user": {"uid": "by2kb"},
            "audio": audio,
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
            },
        }

        with requests.Session() as session:
            submitted = session.post(
                SUBMIT_URL,
                headers=api_headers(app_id, access_token, resource_id, request_id, submit=True),
                data=json.dumps(payload),
                timeout=30,
            )
            status_code, message = response_status(submitted)
            if status_code != "20000000":
                raise RuntimeError(
                    f"Doubao submit failed: {status_code or submitted.status_code} "
                    f"{message or 'unknown error'}"
                )

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                time.sleep(1.5)
                queried = session.post(
                    QUERY_URL,
                    headers=api_headers(app_id, access_token, resource_id, request_id, submit=False),
                    data="{}",
                    timeout=30,
                )
                status_code, message = response_status(queried)
                if status_code == "20000000":
                    result = queried.json().get("result") or {}
                    transcript = str(result.get("text") or "").strip()
                    break
                if status_code == "20000001":
                    continue
                if status_code == "20000003":
                    transcript = ""
                    break
                raise RuntimeError(
                    f"Doubao query failed: {status_code or queried.status_code} "
                    f"{message or 'unknown error'}"
                )
            else:
                raise RuntimeError(f"Doubao query timed out after {timeout:g} seconds")
    finally:
        if uploaded:
            try:
                s3.delete_object(Bucket=bucket, Key=object_key)
            except Exception as exc:  # cleanup must not hide a completed transcript
                cleanup_error = exc

    if cleanup_error is not None:
        print(
            f"WARNING: temporary TOS object cleanup failed: {type(cleanup_error).__name__}",
            file=sys.stderr,
        )
    return transcript


def audio_duration(audio_path: Path) -> float | None:
    """Return media duration when ffprobe is available, otherwise None."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def transcribe_with_chunking(audio_path: Path, env_file: Path | None, timeout: float) -> str:
    """Split long audio so one slow AUC job cannot stall indefinitely."""
    duration = audio_duration(audio_path)
    if duration is None or duration <= 75:
        return transcribe(audio_path, env_file, timeout)

    with tempfile.TemporaryDirectory(prefix="by2kb-doubao-asr-") as temp_dir:
        pattern = str(Path(temp_dir) / "chunk-%03d.ogg")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(audio_path),
                    "-map",
                    "0:a:0",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "32k",
                    "-f",
                    "segment",
                    "-segment_time",
                    "60",
                    pattern,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Long-audio transcription requires ffmpeg") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"Failed to split long audio: {detail}") from exc

        chunks = sorted(Path(temp_dir).glob("chunk-*.ogg"))
        if not chunks:
            raise RuntimeError("ffmpeg produced no audio chunks")

        # map preserves source order while allowing two independent AUC jobs at once.
        chunk_timeout = min(timeout, 150.0)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            texts = list(pool.map(lambda p: transcribe(p, env_file, chunk_timeout), chunks))
        return "".join(text.strip() for text in texts if text.strip())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transcribe audio through Doubao AUC using private TOS staging"
    )
    parser.add_argument("audio_path", type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional KEY=VALUE file; process environment variables take precedence",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--output", type=Path, help="Optional transcript file; stdout is always populated")
    args = parser.parse_args()

    try:
        transcript = transcribe_with_chunking(
            args.audio_path.expanduser().resolve(),
            args.env_file.expanduser() if args.env_file else None,
            args.timeout,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(transcript, encoding="utf-8")
        sys.stdout.write(transcript)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
