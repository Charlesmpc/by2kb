from __future__ import annotations

import asyncio
import hashlib
import re

import httpx

from by2kb.errors import ConfigError
from by2kb.normalize import NormalizedTranscript
from by2kb.operation_lock import exclusive_file
from by2kb.search.models import Candidate, clean_text
from by2kb.search.providers import default_registry
from by2kb.search.store import SearchStore


def scope_key(platform: str, chat: str, user: str, thread: str = "") -> str:
    import json
    return hashlib.sha256(json.dumps([platform, chat, user, thread]).encode()).hexdigest()


def topic_request(text: str) -> str | None:
    match = re.match(r"^\s*(?:/by2kb|by2kb)(?=\s|[:：]|$)\s*[:：]?\s*(.*)$", text, re.I | re.S)
    return match.group(1).strip() if match else None


def search_query(topic: str) -> str:
    return re.sub(r"^(?:我想(?:了解|学习|学)|想(?:了解|学习)|帮我(?:了解|学习))\s*(?:一下)?\s*", "", topic).strip() or topic


def relevance(candidate: Candidate, query: str) -> int:
    """Transparent lexical screening, not a claim of full-content quality ranking."""
    stop = {"tutorial", "tutorials", "learn", "learning", "how", "to", "a", "the", "of", "and", "about", "introduction"}
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", query) if w.lower() not in stop]
    title = candidate.title.casefold()
    metadata = (candidate.title + " " + candidate.description).casefold()
    # A named English subject must actually appear; generic vector results cannot stand in for TiDB.
    if words and not re.search(r"(?<![a-z0-9])" + re.escape(words[0]) + r"(?![a-z0-9])", metadata):
        return 0
    chinese = re.sub(r"教程|入门|知识|实操|讲解", "", query)
    grams = {s[i:i + 2] for s in re.findall(r"[\u4e00-\u9fff]+", chinese) for i in range(len(s) - 1)}
    score = sum(3 if word in title else 1 for word in words if word in metadata)
    score += sum(2 if gram in title else 1 for gram in grams if gram in metadata)
    return score if words or grams else 1


def parse_selection(text: str, count: int) -> list[int]:
    if not re.fullmatch(r"\s*\d+(?:\s*[,，、]\s*\d+)*\s*", text):
        raise ConfigError("reply with numbers such as 1 or 1,3")
    numbers = list(dict.fromkeys(int(p.strip()) for p in re.split(r"[,，、]", text.strip())))
    if any(n < 1 or n > count for n in numbers):
        raise ConfigError("selection number is outside the recommendation list")
    return numbers


async def discover(topic, config, *, scope="local", registry=None, client=None):
    topic = topic.strip()
    if not topic or len(topic) > 500:
        raise ConfigError("learning topic must contain 1..500 characters")
    registry = registry or default_registry()
    providers = registry.enabled(config.search)
    store = SearchStore(config.home)
    session = store.begin(topic, scope, config.search.session_ttl_s)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=config.search.timeout_s, follow_redirects=False)

    async def search(provider):
        try:
            async with asyncio.timeout(config.search.timeout_s):
                query = search_query(topic)
                candidates = await provider.search(query, 5, client)
                ranked = [(relevance(c, query), c) for c in candidates[:5]]
                ranked = sorted((item for item in ranked if item[0] > 0), key=lambda item: item[0], reverse=True)
                return [c for _, c in ranked], None
        except Exception:
            return [], f"{provider.name}: 搜索未完成（网络、限流或工具不可用），其他来源继续。"

    try:
        batches = await asyncio.gather(*(search(p) for p in providers))
        session["warnings"] = [warning for _, warning in batches if warning]
        # Interleave ties, but let a stronger topic match outrank a generic result from another source.
        chosen, seen = [], set()
        for rank in range(config.search.max_candidates):
            for candidates, _ in batches:
                if rank < len(candidates) and candidates[rank].url not in seen:
                    candidate = candidates[rank]
                    chosen.append(candidate.model_copy(deep=True))
                    seen.add(candidate.url)
        chosen = sorted(chosen, key=lambda c: relevance(c, search_query(topic)), reverse=True)[:config.search.max_candidates]
        by_name = {p.name: p for p in providers}

        async def preview(number, candidate):
            if config.search.preview_mode == "captions_only":
                try:
                    async with asyncio.timeout(config.search.preview_timeout_s):
                        normalized = await by_name[candidate.provider].preview(
                            candidate, config.preferred_languages, client, config.search.preview_max_bytes)
                    if normalized is not None and normalized.transcript.segments:
                        if normalized.source.canonical_url != candidate.url:
                            raise ValueError("caption identity mismatch")
                        candidate.caption_sha256 = store.put_caption(session["session_id"], number, normalized)
                        candidate.preview_status = "captions_ready"
                        candidate.preview_excerpt = clean_text(" ".join(s.text for s in normalized.transcript.segments), 1200)
                    else:
                        candidate.preview_status = "no_accessible_captions"
                except Exception:
                    candidate.preview_status = "preview_failed"
            basis = candidate.preview_excerpt if candidate.preview_status == "captions_ready" else candidate.description
            candidate.reason = ("标题或简介匹配学习主题，已取得字幕，可进一步评估。" if candidate.preview_status == "captions_ready"
                                else "标题或简介匹配学习主题，内容深度待核验。")
            if basis:
                candidate.reason += "内容线索：" + clean_text(basis, 180)

        await asyncio.gather(*(preview(i, c) for i, c in enumerate(chosen, 1)))
        session["candidates"] = [c.model_dump() for c in chosen]
        session["status"] = "awaiting_selection" if chosen else "no_results"
        store.finish(session)
        # A later request wins even when an earlier slow provider finishes afterwards.
        latest = store.latest(scope)
        if latest is None or latest["session_id"] != session["session_id"]:
            session["status"] = "superseded"
        return session
    finally:
        if owns_client:
            await client.aclose()


def format_recommendations(session):
    if session["status"] == "cancelled":
        return "已取消这份推荐清单。"
    if session["status"] == "superseded":
        return "这次搜索已被后续请求替代，请使用最新推荐清单。"
    candidates = session["candidates"]
    if not candidates:
        return "没有找到可用候选，请换个关键词或检查已启用的检索来源。\n" + "\n".join(session["warnings"])
    lines = [f"学习主题：{clean_text(session['topic'], 500)}", f"找到 {len(candidates)} 个候选："]
    labels = {"metadata_only": "根据标题和简介推荐，尚未转录",
              "captions_ready": "已取得字幕预览", "no_accessible_captions": "未取得可访问字幕，尚未转录",
              "preview_failed": "字幕预览未完成，根据元数据推荐"}
    for i, item in enumerate(candidates, 1):
        duration = f"{item['duration_s'] / 60:.0f} 分钟" if item.get("duration_s") else "时长未知"
        lines.extend(["", f"{i}. {item['title']}",
                      f"{item['provider']} · {item['author'] or '作者未知'} · {duration}",
                      f"推荐依据：{item['reason']}", f"核验：{labels[item['preview_status']]}", item["url"]])
    lines.extend(["", "优先查看第 1 项；排序依据主题词匹配和来源搜索顺序，尚未进行完整质量评审。",
                  "回复数字选择，如 1 或 1,3。也可回复：换一批、取消；调整主题请发送 by2kb 新主题。"])
    lines.extend(session["warnings"])
    return "\n".join(lines)


async def select_and_ingest(session_id, selection, config, *, scope="local", enricher=None):
    from by2kb.jobs.runner import ingest_source
    store = SearchStore(config.home)
    # Serialize submission, not host LLM operations; retrying the same selection resumes outcomes.
    key = hashlib.sha256(session_id.encode()).hexdigest()
    with exclusive_file(config.home / "locks" / f"search-{key}.lock"):
        session = store.load(session_id, scope)
        if session["status"] not in {"awaiting_selection", "selected"}:
            raise ConfigError("search session is not awaiting a selection")
        numbers = parse_selection(selection, len(session["candidates"]))
        if session["selected"] and session["selected"] != numbers:
            raise ConfigError("this list already has a selection; search again to choose different items")
        session["selected"] = numbers
        session["status"] = "selected"
        store.save(session)
        results = []
        for number in numbers:
            previous = session["outcomes"].get(str(number))
            if previous and previous["status"] in {"completed", "duplicate", "enrichment_pending"}:
                results.append(previous)
                continue
            candidate = Candidate.model_validate(session["candidates"][number - 1])
            normalized = None
            cache = store.cache_path(session_id, number)
            if candidate.preview_status == "captions_ready" and cache.is_file():
                try:
                    content = cache.read_bytes()
                    if hashlib.sha256(content).hexdigest() != candidate.caption_sha256:
                        raise ValueError("caption cache checksum mismatch")
                    normalized = NormalizedTranscript.model_validate_json(content)
                    if normalized.source.canonical_url != candidate.url:
                        raise ValueError("caption cache identity mismatch")
                except ValueError:
                    normalized = None
            outcome = await ingest_source(candidate.url, config, enricher=enricher,
                                          requested_by=scope, cached_transcript=normalized,
                                          learning_topic=session["topic"])
            payload = outcome.to_dict()
            payload["number"] = number
            results.append(payload)
            session["outcomes"][str(number)] = payload
            store.save(session)
        return {"schema_version": 1, "session_id": session_id, "status": "selected", "results": results}
