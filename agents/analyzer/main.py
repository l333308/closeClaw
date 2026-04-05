"""Analyzer Agent — 使用 Qwen 分析热点，产出可直接写文案的分析素材。"""

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

SYSTEM_PROMPT = """你是一个擅长拆解 AI 热点的短视频选题编辑。
你的目标不是做百科摘要，而是给下游文案 Agent 提供可直接写成爆款口播的素材。

分析给定的新闻/文章，输出严格 JSON，格式：
{
  "summary": "一句话摘要（不超过50字）",
  "keywords": ["关键词1", "关键词2", "关键词3"],
  "sentiment": "positive|neutral|negative",
  "relevance": 0.0~1.0,
  "core_point": "这条热点最炸的一个点（不超过30字）",
  "why_it_matters": "为什么这件事值得关注（不超过40字）",
  "impact_on_people": "对普通人、开发者或公司的实际影响（不超过40字）",
  "stance_hint": "建议文案采用的判断/态度（不超过30字）"
}

规则：
1. 只抓一个最值得讲的点，不要面面俱到。
2. 优先提炼冲突、风险、机会、反常识。
3. `stance_hint` 必须有方向感，不能只是中性复述。
4. 只输出 JSON，不要其他内容。"""


def _parse_json_response(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1].lstrip("json").strip()
    return json.loads(raw)


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
    data = _parse_json_response(raw)

    return AnalysisResult(
        summary=data["summary"],
        keywords=data["keywords"],
        sentiment=data["sentiment"],
        relevance=float(data["relevance"]),
        core_point=data.get("core_point", ""),
        why_it_matters=data.get("why_it_matters", ""),
        impact_on_people=data.get("impact_on_people", ""),
        stance_hint=data.get("stance_hint", ""),
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
