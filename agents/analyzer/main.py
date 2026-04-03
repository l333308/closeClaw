"""Analyzer Agent — 使用 Qwen 分析热点，产出摘要/关键词/情感/相关度。"""

from __future__ import annotations

import json
import os
import sys

from openai import OpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import AnalysisResult, Job, Stage, Status, StageMessage
from agents.base import consume_queue, advance_job, fail_job, log

# Qwen API 通过 OpenAI 兼容接口接入（阿里云 DashScope）
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

QUEUE_NAME = "closeclaw.analyze"

SYSTEM_PROMPT = """你是一个 AI 行业分析师。分析给定的新闻/文章，输出严格 JSON，格式：
{
  "summary": "一句话摘要（不超过50字）",
  "keywords": ["关键词1", "关键词2", "关键词3"],
  "sentiment": "positive|neutral|negative",
  "relevance": 0.0~1.0（与AI行业的相关程度）
}
只输出 JSON，不要其他内容。"""


def analyze(title: str, content: str) -> AnalysisResult:
    api_key = os.getenv("QWEN_API_KEY", "")
    if not api_key:
        raise RuntimeError("QWEN_API_KEY is not set")

    model = os.getenv("QWEN_MODEL_35_PLUS", "qwen-plus")
    client = OpenAI(api_key=api_key, base_url=QWEN_BASE_URL)
    user_msg = f"标题：{title}\n\n内容：{content[:3000]}"

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        max_tokens=300,
    )

    raw = resp.choices[0].message.content.strip()
    data = json.loads(raw)

    return AnalysisResult(
        summary=data["summary"],
        keywords=data["keywords"],
        sentiment=data["sentiment"],
        relevance=float(data["relevance"]),
    )


def handle(msg: StageMessage) -> None:
    log.info("analyze start job_id=%s", msg.job_id)

    job = Job.from_json(msg.payload.decode())
    if not job.topic:
        fail_job(msg.job_id, "no topic in job")
        return

    try:
        result = analyze(job.topic.title, job.topic.content)
    except Exception as exc:
        fail_job(msg.job_id, f"analysis failed: {exc}")
        return

    # 过滤低相关度内容
    if result.relevance < 0.5:
        log.info("low relevance=%.2f, skipping job_id=%s", result.relevance, msg.job_id)
        return

    job.stage = Stage.ANALYZE
    job.status = Status.DONE
    job.analysis = result
    advance_job(job)


if __name__ == "__main__":
    consume_queue(QUEUE_NAME, handle)
