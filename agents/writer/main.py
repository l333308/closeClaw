"""Writer Agent — 使用 Claude 生成短视频文案（标题/脚本/Hashtag）。

支持两个 Claude 源（OpenAI 兼容接口），主源失败自动切换备用源：
  - any  (CLAUDE_BASE_URL_ANY  / claude-opus-4-6)   ← 主源，质量优先
  - geek (CLAUDE_BASE_URL_GEEK / claude-sonnet-4-6) ← 备用源
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

from openai import OpenAI, APIError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import CopyResult, Job, Stage, Status, StageMessage
from agents.base import consume_queue, advance_job, fail_job, log

QUEUE_NAME = "closeclaw.write"


@dataclass
class ClaudeSource:
    name: str
    base_url: str
    api_key: str
    model: str


def _load_sources() -> list[ClaudeSource]:
    """按优先级加载 Claude 源，跳过未配置的。"""
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


SYSTEM_PROMPT = """你是一个专业的短视频文案创作者，擅长 AI 科技内容。
根据提供的热点摘要，生成一条适合抖音/TikTok 的短视频文案，输出严格 JSON：
{
  "title": "视频标题（15字以内，吸引眼球）",
  "script": "视频口播脚本（100-150字，口语化，节奏感强）",
  "hashtags": ["话题1", "话题2", "话题3", "话题4", "话题5"]
}
只输出 JSON。"""


def _call_source(source: ClaudeSource, user_msg: str) -> CopyResult:
    """调用单个 Claude 源，返回 CopyResult（失败则抛出异常）。"""
    client = OpenAI(api_key=source.api_key, base_url=source.base_url)
    resp = client.chat.completions.create(
        model=source.model,
        max_tokens=512,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.7,
    )
    raw = resp.choices[0].message.content.strip()
    # 兼容部分源在 JSON 外包裹 markdown 代码块
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    data = json.loads(raw)
    return CopyResult(
        title=data["title"],
        script=data["script"],
        hashtags=data["hashtags"],
    )


def generate_copy(title: str, summary: str, keywords: list[str]) -> CopyResult:
    user_msg = (
        f"热点标题：{title}\n"
        f"摘要：{summary}\n"
        f"关键词：{', '.join(keywords)}"
    )

    sources = _load_sources()
    if not sources:
        raise RuntimeError(
            "no Claude sources configured: set CLAUDE_BASE_URL_ANY/CLAUDE_API_KEY_ANY "
            "or CLAUDE_BASE_URL_GEEK/CLAUDE_API_KEY_GEEK"
        )

    last_exc: Exception | None = None
    for source in sources:
        try:
            result = _call_source(source, user_msg)
            log.info("copy generated via source=%s model=%s", source.name, source.model)
            return result
        except Exception as exc:
            log.warning("source=%s failed: %s, trying next...", source.name, exc)
            last_exc = exc

    raise RuntimeError(f"all Claude sources failed, last error: {last_exc}")


def handle(msg: StageMessage) -> None:
    log.info("write start job_id=%s", msg.job_id)

    job = Job.from_json(msg.payload.decode())
    if not job.topic or not job.analysis:
        fail_job(msg.job_id, "missing topic or analysis")
        return

    try:
        result = generate_copy(
            job.topic.title,
            job.analysis.summary,
            job.analysis.keywords,
        )
    except Exception as exc:
        fail_job(msg.job_id, f"copywriting failed: {exc}")
        return

    job.stage = Stage.WRITE
    job.status = Status.DONE
    job.copy = result
    advance_job(job)


if __name__ == "__main__":
    consume_queue(QUEUE_NAME, handle)
