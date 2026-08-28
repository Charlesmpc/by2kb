from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

import httpx

from by2kb.config import LlmConfig
from by2kb.errors import TransientProviderError
from by2kb.normalize import NormalizedTranscript
from by2kb.skills.model import Skill


@runtime_checkable
class LlmClient(Protocol):
    provider: str
    model: str

    async def complete(self, system: str, user: str) -> str: ...


class OpenAiCompatibleClient:
    provider = "openai_compatible"

    def __init__(self, config: LlmConfig, client: httpx.AsyncClient | None = None):
        self._config = config
        self._client = client
        self._owns_client = client is None

    @property
    def model(self) -> str:
        return self._config.model or ""

    async def complete(self, system: str, user: str) -> str:
        client = self._client or httpx.AsyncClient(timeout=180)
        try:
            url = self._config.base_url.rstrip("/") + "/chat/completions"
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                json={
                    "model": self._config.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"LLM request failed: {exc}", provider="llm") from exc
        finally:
            if self._owns_client and self._client is None:
                await client.aclose()
        if response.status_code != 200:
            raise TransientProviderError(
                f"LLM request failed: HTTP {response.status_code}", provider="llm"
            )
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TransientProviderError(
                "LLM request failed: malformed chat-completions response",
                provider="llm",
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise TransientProviderError(
                "LLM request failed: empty completion", provider="llm"
            )
        return content


def build_prompts(
    skill: Skill, normalized: NormalizedTranscript, raw_md: str
) -> tuple[str, str]:
    system = (
        "You are a video-knowledge processor. Follow the skill instructions exactly "
        "and answer with Markdown only. Never invent facts, speakers, or timestamps "
        "that are not present in the transcript."
    )
    quality = normalized.transcript.quality
    quality_context = (
        json.dumps(quality.model_dump(mode="json"), ensure_ascii=False, indent=2)
        if quality
        else "not assessed"
    )
    user = (
        f"# Skill: {skill.name} v{skill.version}\n\n"
        f"{skill.body}\n\n"
        f"# Source metadata\n\n"
        f"- platform: {normalized.source.platform}\n"
        f"- video_id: {normalized.source.video_id}\n"
        f"- title: {normalized.source.title}\n"
        f"- author: {normalized.source.author}\n"
        f"- transcript_kind: {normalized.transcript.kind}\n\n"
        f"# Deterministic transcript quality assessment\n\n{quality_context}\n\n"
        f"# Raw transcript (Markdown)\n\n{raw_md}"
    )
    return system, user


async def run_skill(
    skill: Skill,
    normalized: NormalizedTranscript,
    raw_md: str,
    llm: LlmClient,
) -> str:
    system, user = build_prompts(skill, normalized, raw_md)
    return await llm.complete(system, user)
