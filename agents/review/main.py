"""Review Agent — 给文案打分，不达标则触发有限次重写。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from openai import OpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import CopyReviewResult, Job, Stage, Status, StageMessage
from agents.base import consume_queue, advance_job, fail_job, log

QUEUE_NAME = "closeclaw.review"
CLAUDE_REQUEST_TIMEOUT = float(os.getenv("CLAUDE_REQUEST_TIMEOUT", "25"))
COPY_REVIEW_MIN_SCORE = int(os.getenv("COPY_REVIEW_MIN_SCORE", "7"))
COPY_MAX_REWRITES = int(os.getenv("COPY_MAX_REWRITES", "2"))


@dataclass
class ClaudeSource:
    name: str
    base_url: str
    api_key: str
    model: str


SYSTEM_PROMPT = """你是短视频内容总监，负责审核 AI 热点口播文案。

请从以下 4 个维度为文案打分（0-10）：
- attraction：是否让人想继续看
- emotion：是否有情绪和态度
- information_density：是否废话少、信息够集中
- virality：是否有传播和评论欲望

判定规则：
1. 任一项低于 7 分，默认 verdict=rewrite。
2. 如果标题平、Hook 弱、观点太中立、结尾不想评论，也应该 verdict=rewrite。
3. 只有四项都较强时，才能 verdict=approve。

输出严格 JSON：
{
  "attraction": 0,
  "emotion": 0,
  "information_density": 0,
  "virality": 0,
  "verdict": "approve|rewrite",
  "summary": "一句话总结这版文案最大问题或最大优点",
  "suggestions": ["最多3条明确修改建议"]
}

只输出 JSON。"""


def _load_sources() -> list[ClaudeSource]:
    candidates = [
        ClaudeSource(
            name="any",
            base_url=os.getenv("CLAUDE_BASE_URL_ANY", ""),
            api_key=os.getenv("CLAUDE_API_KEY_ANY", ""),
            model=os.getenv("CLAUDE_MODEL_ANY", "claude-opus-4-6"),
        ),
        ClaudeSource(
            name="geek",
            base_url=os.getenv("CLAUDE_BASE_URL_GEEK", ""),
            api_key=os.getenv("CLAUDE_API_KEY_GEEK", ""),
            model=os.getenv("CLAUDE_MODEL_GEEK", "claude-sonnet-4-6"),
        ),
    ]
    return [s for s in candidates if s.api_key and s.base_url]


def _normalize_base_url(base_url: str) -> str:
    parsed = urlparse(base_url.strip())
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"
    return urlunparse(parsed._replace(path=path))


def _parse_json_response(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1].lstrip("json").strip()
    return json.loads(raw)


def _build_user_message(job: Job) -> str:
    assert job.topic and job.analysis and job.copy

    return "\n".join(
        [
            f"热点标题：{job.topic.title}",
            f"摘要：{job.analysis.summary}",
            f"核心点：{job.analysis.core_point}",
            f"为什么重要：{job.analysis.why_it_matters}",
            f"对普通人的影响：{job.analysis.impact_on_people}",
            f"建议立场：{job.analysis.stance_hint}",
            f"文案标题：{job.copy.title}",
            f"文案脚本：{job.copy.script}",
            f"话题标签：{', '.join(job.copy.hashtags)}",
        ]
    )


def _call_source(source: ClaudeSource, user_msg: str) -> CopyReviewResult:
    base_url = _normalize_base_url(source.base_url)
    client = OpenAI(
        api_key=source.api_key,
        base_url=base_url,
        timeout=CLAUDE_REQUEST_TIMEOUT,
        max_retries=0,
    )
    log.info("review calling source=%s model=%s", source.name, source.model)
    resp = client.chat.completions.create(
        model=source.model,
        max_tokens=400,
        temperature=0.2,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    raw = resp.choices[0].message.content or ""
    data = _parse_json_response(raw)
    return CopyReviewResult(
        attraction=int(data["attraction"]),
        emotion=int(data["emotion"]),
        information_density=int(data["information_density"]),
        virality=int(data["virality"]),
        verdict=str(data["verdict"]).strip().lower(),
        summary=str(data.get("summary", "")).strip(),
        suggestions=[str(item).strip() for item in data.get("suggestions", []) if str(item).strip()],
    )


def review_copy(job: Job) -> CopyReviewResult:
    user_msg = _build_user_message(job)
    sources = _load_sources()
    if not sources:
        raise RuntimeError(
            "no Claude sources configured: set CLAUDE_BASE_URL_ANY/CLAUDE_API_KEY_ANY "
            "or CLAUDE_BASE_URL_GEEK/CLAUDE_API_KEY_GEEK"
        )

    last_exc: Exception | None = None
    for source in sources:
        try:
            return _call_source(source, user_msg)
        except Exception as exc:
            log.warning(
                "review source=%s model=%s failed (%s): %s",
                source.name,
                source.model,
                type(exc).__name__,
                exc,
            )
            last_exc = exc

    raise RuntimeError(f"all review sources failed, last error: {last_exc}")


def _needs_rewrite(review: CopyReviewResult) -> bool:
    scores = [
        review.attraction,
        review.emotion,
        review.information_density,
        review.virality,
    ]
    return review.verdict == "rewrite" or any(score < COPY_REVIEW_MIN_SCORE for score in scores)


def handle(msg: StageMessage) -> None:
    log.info("review start job_id=%s", msg.job_id)

    job = Job.from_json(msg.payload.decode())
    if not job.topic or not job.analysis or not job.copy:
        fail_job(msg.job_id, "missing topic, analysis or copy")
        return

    try:
        review = review_copy(job)
    except Exception as exc:
        fail_job(msg.job_id, f"copy review failed: {exc}")
        return

    job.review = review
    job.stage = Stage.REVIEW
    job.status = Status.DONE

    if _needs_rewrite(review):
        if job.copy_rewrite_count >= COPY_MAX_REWRITES:
            fail_job(
                msg.job_id,
                f"copy review failed after {COPY_MAX_REWRITES} rewrites: {review.summary or review.suggestions}",
            )
            return

        job.copy_rewrite_count += 1
        log.info(
            "review requires rewrite job_id=%s rewrite_count=%s summary=%s",
            msg.job_id,
            job.copy_rewrite_count,
            review.summary,
        )
    else:
        log.info("review approved job_id=%s", msg.job_id)

    advance_job(job)


if __name__ == "__main__":
    consume_queue(QUEUE_NAME, handle)
