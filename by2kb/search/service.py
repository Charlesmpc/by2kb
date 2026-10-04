from __future__ import annotations

import asyncio
import hashlib
import re
import time
import random
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import httpx

from by2kb.errors import ConfigError, RateLimited, TransientProviderError
from by2kb.search.planning import clean_query, fallback_plan, validate_plan, intent_score
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
    return clean_query(topic)


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


def _retry_error(error):
    status, wait = None, None
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        category = "rate_limited" if status in {412, 429} else "unavailable"
        retry = status in {412, 429, 500, 502, 503, 504}
        header = error.response.headers.get("Retry-After")
        if header and retry:
            try:
                wait = max(0, float(header))
                if not __import__("math").isfinite(wait): wait = None
            except ValueError:
                try: wait = max(0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError): pass
        return category, retry, status, wait
    if isinstance(error, (RateLimited, TransientProviderError, httpx.RequestError, TimeoutError)):
        return "rate_limited" if isinstance(error, RateLimited) else "unavailable", True, status, wait
    return "invalid_response" if isinstance(error, (ValueError, TypeError, KeyError)) else "unavailable", False, status, wait


async def discover(topic, config, *, scope="local", registry=None, client=None, plan=None, budget_s=None, session_id=None):
    topic = topic.strip()
    if not topic or len(topic) > 500:
        raise ConfigError("learning topic must contain 1..500 characters")
    registry = registry or default_registry()
    providers = registry.enabled(config.search)
    plan = validate_plan(plan, topic) if plan is not None else fallback_plan(topic)
    if budget_s is not None and (budget_s <= 0 or not __import__("math").isfinite(budget_s)):
        raise ConfigError("search time budget exhausted")
    budget = config.search.total_timeout_s if budget_s is None else min(config.search.total_timeout_s, budget_s)
    if budget <= 0 or not __import__("math").isfinite(budget):
        raise ConfigError("search time budget exhausted")
    started = time.monotonic()
    deadline = started + budget
    search_deadline = min(started + config.search.timeout_s, deadline - min(5, budget / 4))
    store = SearchStore(config.home)
    session = store.load(session_id, scope) if session_id else store.begin(topic, scope, config.search.session_ttl_s)
    if session["status"] != "searching" or session["topic"] != topic:
        raise ConfigError("search session does not match pending request")
    session["query_plan"] = plan
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=config.search.attempt_timeout_s, follow_redirects=False)

    async def search(provider):
        pool, seen, attempts = [], set(), []
        query_index, delay, state = 0, 0, "empty"
        last_error = None
        for number in range(1, config.search.max_attempts + 1):
            remaining = search_deadline - time.monotonic()
            if delay >= remaining or remaining <= 0: break
            if delay: await asyncio.sleep(delay)
            remaining = search_deadline - time.monotonic()
            if remaining <= 0: break
            query = plan["queries"][query_index]
            tick = time.monotonic()
            try:
                async with asyncio.timeout(min(config.search.attempt_timeout_s, remaining)):
                    rows = await provider.search(query, 20 if provider.name == "bilibili" else 10, client)
                ranked = [(relevance(c, plan["subject"]), c) for c in rows]
                valid = [c for score, c in ranked if score > 0]
                state = "ok" if valid else "filtered_empty" if rows else "empty"
                attempts.append({"query": query, "outcome": state, "elapsed_s": round(time.monotonic()-tick, 3), "count": len(valid)})
                for candidate in valid:
                    if candidate.url not in seen: pool.append(candidate); seen.add(candidate.url)
                last_error = None
                enough = len(pool) >= config.search.max_candidates
                if plan["intent"] != "general":
                    enough = sum(intent_score(c, plan["intent"]) > 0 for c in pool) >= config.search.max_candidates
                if enough: break
                if query_index + 1 < len(plan["queries"]):
                    query_index += 1
                    delay = config.search.retry_base_s if not rows else 0
                elif state == "empty":
                    delay = config.search.retry_base_s * 2 ** (number - 1)
                else: break
            except Exception as error:
                state, retry, status, retry_after = _retry_error(error)
                last_error = state
                attempt = {"query": query, "outcome": state, "elapsed_s": round(time.monotonic()-tick, 3)}
                if status is not None: attempt["http_status"] = status
                attempts.append(attempt)
                if not retry: break
                delay = max(config.search.retry_base_s * 2 ** (number - 1), retry_after or 0)
            if delay: delay += random.uniform(0, min(.2, config.search.retry_base_s / 10))
        # A later failed expansion cannot erase successful candidates, but remains visible.
        outcome = "partial" if pool and last_error else "ok" if pool else state if attempts else "unavailable"
        warning = None
        if last_error or not attempts:
            label = {"bilibili": "B站", "youtube": "YouTube"}.get(provider.name, provider.name)
            warning = f"{label}暂时未能完成搜索，已尝试 {len(attempts)} 次；不能据此判断没有相关视频。"
        pool.sort(key=lambda c: (relevance(c, plan["subject"]), intent_score(c, plan["intent"])), reverse=True)
        return pool, warning, {"provider": provider.name, "outcome": outcome, "attempts": attempts}

    try:
        batches = await asyncio.gather(*(search(p) for p in providers))
        session["warnings"] = [warning for _, warning, _ in batches if warning]
        session["sources"] = [diagnostic for _, _, diagnostic in batches]
        chosen, seen = [], set()
        for rank in range(max((len(candidates) for candidates, _, _ in batches), default=0)):
            for candidates, _, _ in batches:
                if rank < len(candidates) and candidates[rank].url not in seen:
                    chosen.append(candidates[rank].model_copy(deep=True)); seen.add(candidates[rank].url)
        chosen = sorted(chosen, key=lambda c: (relevance(c, plan["subject"]), intent_score(c, plan["intent"])), reverse=True)[:config.search.max_candidates]
        by_name = {p.name: p for p in providers}

        async def preview(number, candidate):
            if config.search.preview_mode == "captions_only":
                try:
                    remaining = deadline - time.monotonic() - .2
                    if remaining <= 0: raise TimeoutError()
                    async with asyncio.timeout(min(5, config.search.preview_timeout_s, remaining)):
                        normalized = await by_name[candidate.provider].preview(candidate, config.preferred_languages, client, config.search.preview_max_bytes)
                    if normalized is not None and normalized.transcript.segments:
                        if normalized.source.canonical_url != candidate.url: raise ValueError("caption identity mismatch")
                        candidate.caption_sha256 = store.put_caption(session["session_id"], number, normalized)
                        candidate.preview_status = "captions_ready"
                        candidate.preview_excerpt = clean_text(" ".join(s.text for s in normalized.transcript.segments), 1200)
                    else: candidate.preview_status = "no_accessible_captions"
                except Exception: candidate.preview_status = "preview_failed"
            basis = candidate.preview_excerpt if candidate.preview_status == "captions_ready" else candidate.description
            candidate.reason = "标题或简介匹配学习主题，已取得字幕，可进一步评估。" if candidate.preview_status == "captions_ready" else "标题或简介匹配学习主题，内容深度待核验。"
            if basis: candidate.reason += "内容线索：" + clean_text(basis, 180)

        await asyncio.gather(*(preview(i, c) for i, c in enumerate(chosen, 1)))
        session["candidates"] = [c.model_dump() for c in chosen]
        session["status"] = "awaiting_selection" if chosen else "search_failed" if session["warnings"] else "no_results"
        session["elapsed_s"] = round(time.monotonic() - started, 3)
        store.finish(session)
        latest = store.latest(scope)
        if latest is None or latest["session_id"] != session["session_id"]: session["status"] = "superseded"
        return session
    finally:
        if owns_client: await client.aclose()


def format_duration(value):
    if value is None:
        return "未提供"
    seconds = int(value)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}小时{minutes:02d}分{seconds:02d}秒" if hours else f"{minutes}分{seconds:02d}秒"


def format_count(value):
    if value is None:
        return "未提供"
    if value >= 100000000:
        return f"{value / 100000000:.1f}亿"
    if value >= 10000:
        return f"{value / 10000:.1f}万"
    return f"{value:,}"


def format_recommendations(session):
    if session["status"] == "cancelled":
        return "已取消这份推荐清单。"
    if session["status"] == "superseded":
        return "这次搜索已被后续请求替代，请使用最新推荐清单。"
    candidates = session["candidates"]
    if not candidates:
        return ("搜索暂时未完成。\n" + "\n".join(session["warnings"])) if session["warnings"] else "没有找到匹配的视频，请尝试调整关键词。"
    lines = [f"学习主题：{clean_text(session.get('query_plan', {}).get('subject') or search_query(session['topic']), 200)}", f"找到 {len(candidates)} 个候选："]
    platforms = {"youtube": "🟥 YouTube", "bilibili": "🟦 B站 · bilibili"}
    for i, item in enumerate(candidates, 1):
        clue = item.get("preview_excerpt") if item.get("preview_status") == "captions_ready" else item.get("description")
        lines.extend(["", "──────────", f"{i}｜{platforms.get(item['provider'], clean_text(item['provider'], 40))}", clean_text(item['title'], 200)])
        if item.get("author"):
            lines.append("作者：" + clean_text(item['author'], 90))
        metrics = []
        if item.get("duration_s") is not None:
            metrics.append("⏱ " + format_duration(item['duration_s']))
        if item.get("view_count") is not None:
            metrics.append("▶ 播放 " + format_count(item['view_count']))
        if item.get("like_count") is not None:
            metrics.append("👍 点赞 " + format_count(item['like_count']))
        if metrics:
            lines.append(" · ".join(metrics))
        if clue:
            clue = clean_text(re.sub(r"https?://\S+", "", clue), 100)
            if clue:
                lines.append("内容线索：" + clue)
        lines.append(item["url"])
    if session.get("sources") and session["warnings"]:
        lines.extend(["", "；".join(session["warnings"])])
    lines.extend(["", "回复 1 或 1,3 开始整理；也可回复「换一批」「取消」。", "内容线索依据标题、简介或可用字幕整理，尚未评审完整视频。"])
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
