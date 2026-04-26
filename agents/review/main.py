"""Review Agent — 给文案打分，不达标则触发有限次重写。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from openai import OpenAI
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import CopyReviewResult, Job, Stage, Status, StageMessage
from agents.base import ORCHESTRATOR_URL, consume_queue, advance_job, fail_job, log, save_job_snapshot

QUEUE_NAME = "closeclaw.review"
CLAUDE_REQUEST_TIMEOUT = float(os.getenv("CLAUDE_REQUEST_TIMEOUT", "25"))
CLAUDE_SOURCE_ORDER = os.getenv("CLAUDE_SOURCE_ORDER", "geek,any")
COPY_REVIEW_MIN_SCORE = int(os.getenv("COPY_REVIEW_MIN_SCORE", "7"))
COPY_MAX_REWRITES = int(os.getenv("COPY_MAX_REWRITES", "2"))
MAX_VIDEOS_PER_TRIGGER = int(os.getenv("MAX_VIDEOS_PER_TRIGGER", "3"))
TEXT_OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../../output/text.md")


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
    available = [s for s in candidates if s.api_key and s.base_url]
    order = {name.strip(): idx for idx, name in enumerate(CLAUDE_SOURCE_ORDER.split(",")) if name.strip()}
    available.sort(key=lambda source: (order.get(source.name, len(order)), source.name))
    return available


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


def _format_script_for_markdown(script: str) -> str:
    """将逐句脚本格式化为 markdown，逐句换行但不额外插空行。"""
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    return "\n".join(lines)


def _score_level(score: int) -> str:
    if score >= 9:
        return "很强"
    if score >= 7:
        return "合格"
    if score >= 5:
        return "偏弱"
    return "很弱"


def _overall_assessment(review: CopyReviewResult, final_status: str) -> str:
    if final_status == "approved":
        return "可直接评估是否发布"
    if review.verdict == "rewrite":
        return "不建议直接发布，建议重点重写"
    return "可作为失败样本复盘"


def _coerce_job_from_api(data: dict) -> Job:
    """兼容历史脏数据里的非法 stage 字段。"""
    payload = dict(data)
    valid_stages = {stage.value for stage in Stage}
    if payload.get("stage") not in valid_stages:
        payload["stage"] = Stage.REVIEW.value
    return Job.from_dict(payload)


def _list_trigger_jobs(trigger_job_id: str) -> list[Job]:
    if not trigger_job_id:
        return []

    resp = requests.get(f"{ORCHESTRATOR_URL}/triggers/{trigger_job_id}/jobs", timeout=10)
    resp.raise_for_status()

    jobs: list[Job] = []
    for item in resp.json().get("jobs", []):
        try:
            jobs.append(_coerce_job_from_api(item))
        except Exception as exc:
            log.warning("load trigger sibling failed trigger_job_id=%s err=%s", trigger_job_id, exc)
    return jobs


def _is_approved_candidate(job: Job) -> bool:
    return (
        job.review is not None
        and job.review.verdict == "approve"
        and job.stage == Stage.REVIEW
        and job.status == Status.DONE
    )


def _is_settled_for_video_pick(job: Job) -> bool:
    if job.status == Status.FAILED:
        return True
    if job.video_pick_status in {"selected", "skipped"}:
        return True
    if job.stage in {Stage.VIDEO, Stage.PUBLISH} or job.video is not None:
        return True
    return _is_approved_candidate(job)


def _hotness_score(job: Job) -> float:
    topic_score = 0.0
    relevance = 0.0
    if job.topic:
        topic_score = max(0.0, min(job.topic.score, 1.0)) * 10
    if job.analysis:
        relevance = max(0.0, min(job.analysis.relevance, 1.0)) * 10
    return (topic_score * 0.7) + (relevance * 0.3)


def _quality_score(job: Job) -> float:
    assert job.review is not None
    return (
        job.review.attraction * 0.35
        + job.review.virality * 0.30
        + job.review.emotion * 0.20
        + job.review.information_density * 0.15
    )


def _compute_video_rank(job: Job) -> float:
    """综合文案质量与热点热度，为视频预算做 Top-N 选择。"""
    quality = _quality_score(job)
    hotness = _hotness_score(job)
    rewrite_penalty = job.copy_rewrite_count * 0.25
    return round((quality * 0.7) + (hotness * 0.3) - rewrite_penalty, 4)


def _finalize_video_batch(trigger_job_id: str, expected_count: int) -> None:
    if not trigger_job_id or expected_count <= 0:
        return

    siblings = _list_trigger_jobs(trigger_job_id)
    if len(siblings) < expected_count:
        log.info(
            "video pick waiting trigger_job_id=%s siblings=%s expected=%s",
            trigger_job_id,
            len(siblings),
            expected_count,
        )
        return

    if not all(_is_settled_for_video_pick(job) for job in siblings):
        log.info("video pick waiting trigger_job_id=%s reason=batch_not_settled", trigger_job_id)
        return

    approved_jobs = [job for job in siblings if _is_approved_candidate(job)]
    if approved_jobs and all(job.video_pick_status in {"selected", "skipped"} for job in approved_jobs):
        log.info("video pick already finalized trigger_job_id=%s", trigger_job_id)
        return

    if not approved_jobs:
        log.info("video pick skipped trigger_job_id=%s reason=no_approved_jobs", trigger_job_id)
        return

    ranked_jobs = sorted(
        approved_jobs,
        key=lambda job: (
            _compute_video_rank(job),
            job.topic.score if job.topic else 0.0,
            job.review.virality if job.review else 0,
            -job.copy_rewrite_count,
        ),
        reverse=True,
    )

    selected_ids = {job.id for job in ranked_jobs[:MAX_VIDEOS_PER_TRIGGER]}
    for index, ranked_job in enumerate(ranked_jobs, start=1):
        ranked_job.video_rank_score = _compute_video_rank(ranked_job)
        if ranked_job.id in selected_ids:
            ranked_job.video_pick_status = "selected"
            ranked_job.status = Status.DONE
            ranked_job.error = ""
            save_job_snapshot(ranked_job)
            log.info(
                "video pick selected trigger_job_id=%s rank=%s score=%.3f job_id=%s",
                trigger_job_id,
                index,
                ranked_job.video_rank_score,
                ranked_job.id,
            )
            advance_job(ranked_job)
        else:
            ranked_job.video_pick_status = "skipped"
            ranked_job.status = Status.SKIPPED
            ranked_job.error = (
                f"not selected for video: ranked below top {MAX_VIDEOS_PER_TRIGGER} "
                f"within trigger {trigger_job_id}"
            )
            save_job_snapshot(ranked_job)
            log.info(
                "video pick skipped trigger_job_id=%s rank=%s score=%.3f job_id=%s",
                trigger_job_id,
                index,
                ranked_job.video_rank_score,
                ranked_job.id,
            )


def _append_final_copy_markdown(job: Job, review: CopyReviewResult, final_status: str) -> None:
    """将最终版文案追加到 output/text.md，便于人工横向评估。"""
    assert job.topic and job.copy

    output_path = os.path.abspath(TEXT_OUTPUT_PATH)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    marker = f"<!-- job_id: {job.id} -->"
    if os.path.exists(output_path):
        with open(output_path, "r", encoding="utf-8") as existing:
            if marker in existing.read():
                log.info("final copy already exported job_id=%s path=%s", job.id, output_path)
                return

    hashtags = " ".join(f"#{tag.lstrip('#')}" for tag in job.copy.hashtags)
    formatted_script = _format_script_for_markdown(job.copy.script)
    suggestions = "\n".join(f"- {item}" for item in review.suggestions) or "- 无"
    exported_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    topic_url = job.topic.url or "(no url)"
    assessment = _overall_assessment(review, final_status)

    entry = (
        f"{marker}\n"
        f"## {job.copy.title}\n"
        f"### 快速结论\n"
        f"- 当前判断: **{assessment}**\n"
        f"- 最终结果: `{final_status}`\n"
        f"- AI Verdict: `{review.verdict}`\n"
        f"- AI 总评: {review.summary or '无'}\n"
        f"- 重写次数: {job.copy_rewrite_count}\n"
        f"- 标签: {hashtags or '(none)'}\n\n"
        f"### 背景信息\n"
        f"- 导出时间: {exported_at}\n"
        f"- Job ID: `{job.id}`\n"
        f"- 原始热点: {job.topic.title}\n"
        f"- 热点链接: {topic_url}\n\n"
        f"### 维度评分\n"
        f"- 吸引力: {review.attraction}/10（{_score_level(review.attraction)}）\n"
        f"- 情绪感: {review.emotion}/10（{_score_level(review.emotion)}）\n"
        f"- 信息密度: {review.information_density}/10（{_score_level(review.information_density)}）\n"
        f"- 传播性: {review.virality}/10（{_score_level(review.virality)}）\n\n"
        f"### 成稿文案\n"
        f"{formatted_script}\n\n"
        f"### AI 修改建议\n"
        f"{suggestions}\n\n"
        f"### 人工评估记录\n"
        f"- 第一感觉（看完 3 秒内的直觉）:\n"
        f"- 我最想保留的一句:\n"
        f"- 我最想改掉的一句:\n"
        f"- 哪一句最像 AI 味:\n"
        f"- 这条文案最适合打的情绪点:\n"
        f"- 如果要发，我最担心的问题:\n\n"
        f"### 外部参考与优化\n"
        f"- 对标账号 / 爆款链接:\n"
        f"- 从外部参考里学到的 1 个 hook:\n"
        f"- 可以借用的表达节奏 / 结构:\n"
        f"- 下一版改写方向:\n"
        f"- 改后版本草稿:\n\n"
    )

    need_header = not os.path.exists(output_path) or os.path.getsize(output_path) == 0
    with open(output_path, "a", encoding="utf-8") as out:
        if need_header:
            out.write("# Writer Copy Evaluation Board\n\n")
            out.write("按最终版文案追加记录，方便人工评估、对标外部爆款，并继续二次优化。\n\n")
            out.write("## 使用建议\n\n")
            out.write("- 先看“快速结论”和“维度评分”，快速判断这条稿子值不值得继续打磨。\n")
            out.write("- 再读“成稿文案”，只判断一件事：你会不会愿意继续往下看。\n")
            out.write("- 最后去填“人工评估记录”和“外部参考与优化”，把外部资源带来的改写思路沉淀下来。\n\n")
        out.write(entry)

    log.info("final copy exported job_id=%s final_status=%s path=%s", job.id, final_status, output_path)


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
            job.video_pick_status = "skipped"
            _append_final_copy_markdown(job, review, final_status="max_rewrites_reached")
            save_job_snapshot(job)
            fail_job(
                msg.job_id,
                f"copy review failed after {COPY_MAX_REWRITES} rewrites: {review.summary or review.suggestions}",
            )
            if job.trigger_job_id and job.batch_topic_count > 0:
                _finalize_video_batch(job.trigger_job_id, job.batch_topic_count)
            return

        job.copy_rewrite_count += 1
        log.info(
            "review requires rewrite job_id=%s rewrite_count=%s summary=%s",
            msg.job_id,
            job.copy_rewrite_count,
            review.summary,
        )
        advance_job(job)
        return
    else:
        log.info("review approved job_id=%s", msg.job_id)
        _append_final_copy_markdown(job, review, final_status="approved")
        job.video_rank_score = _compute_video_rank(job)

    if not job.trigger_job_id or job.batch_topic_count <= 0:
        job.video_pick_status = "selected"
        save_job_snapshot(job)
        advance_job(job)
        return

    save_job_snapshot(job)
    _finalize_video_batch(job.trigger_job_id, job.batch_topic_count)


if __name__ == "__main__":
    consume_queue(QUEUE_NAME, handle)
