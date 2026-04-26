"""Writer Agent — 使用 Claude 生成更像短视频爆款的 AI 文案。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from openai import OpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import AnalysisResult, CopyResult, CopyReviewResult, Job, Stage, Status, StageMessage
from agents.base import consume_queue, advance_job, fail_job, log

QUEUE_NAME = "closeclaw.write"
CLAUDE_REQUEST_TIMEOUT = float(os.getenv("CLAUDE_REQUEST_TIMEOUT", "25"))
CLAUDE_SOURCE_ORDER = os.getenv("CLAUDE_SOURCE_ORDER", "geek,any")


@dataclass
class ClaudeSource:
    name: str
    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True)
class StyleTemplate:
    key: str
    label: str
    angle: str
    hook_hint: str
    stance_hint: str


STYLE_TEMPLATES = (
    StyleTemplate(
        key="shock",
        label="震惊型",
        angle="把最反常识、最离谱的点顶到最前面",
        hook_hint="开头 1 句就要让人想继续听下去",
        stance_hint="语气要直接，像在提醒朋友别错过大事",
    ),
    StyleTemplate(
        key="conspiracy",
        label="阴谋型",
        angle="强调这件事表面一层、背后另一层",
        hook_hint="开头要有“真正关键不是表面新闻”的感觉",
        stance_hint="语气偏质疑、拆解和提醒风险",
    ),
    StyleTemplate(
        key="opportunity",
        label="机会型",
        angle="强调普通人、开发者或创业者能抓住的窗口",
        hook_hint="开头直接点出机会、门槛变化或格局重排",
        stance_hint="语气偏判断和行动建议，但不能鸡汤",
    ),
)

FEW_SHOTS = [
    (
        "热点标题：Claude代码泄露\n"
        "一句话摘要：Claude 核心实现细节被曝光，引发 AI 圈讨论。\n"
        "核心点：大家发现护城河没想象中深。\n"
        "为什么重要：这会影响行业对模型壁垒的判断。\n"
        "对普通人的影响：开发者机会变多，公司竞争压力变大。\n"
        "建议立场：这是一次行业护城河被重新定价的信号。\n"
        "关键词：Claude, 开源, 泄露",
        {
            "title": "Claude被扒光了？",
            "script": "昨天AI圈炸了！\nClaude源码居然泄露了。\n最离谱的还不是泄露。\n而是大家突然发现。\nAI护城河没那么深。\n这意味着什么？\n开发者机会可能来了。\n但做模型的公司要更慌。\n你觉得AI还有壁垒吗？",
            "hashtags": ["AI", "Claude", "科技"],
        },
    ),
    (
        "热点标题：某开源模型性能逼近头部闭源模型\n"
        "一句话摘要：开源模型能力快速追平闭源头部产品。\n"
        "核心点：模型能力差距正在被快速抹平。\n"
        "为什么重要：AI 的门槛和定价权都可能被改写。\n"
        "对普通人的影响：开发者成本更低，创业试错更快。\n"
        "建议立场：真正的机会不一定在做模型，而在用模型赚钱。\n"
        "关键词：开源模型, 成本, 创业",
        {
            "title": "普通人机会真来了",
            "script": "AI圈又变天了。\n开源模型正在追平闭源。\n真正可怕的不是技术进步。\n而是门槛突然掉下来了。\n以前只有大厂玩得起。\n现在小团队也能上桌。\n对普通人来说。\n机会不在造模型。\n而在更快用模型赚钱。\n你觉得谁最先被改写？",
            "hashtags": ["AI", "开源", "创业"],
        },
    ),
]

SYSTEM_PROMPT = """你是一个顶级短视频文案操盘手，专门做 AI 科技爆款内容。

你的目标不是介绍信息，而是：
让用户停留、产生情绪、愿意转发。

【强制规则】
1. 输出严格 JSON：
{
  "title": "15字以内，必须有冲突感或悬念感",
  "script": "按结构输出，用换行分段",
  "hashtags": ["AI", "科技", "热点"]
}
2. 脚本结构必须固定为：
- Hook（前3秒，制造冲突/震惊/反常识）
- 事件（快速讲清发生了什么）
- 核心分析（只讲一个最关键点）
- 观点（必须有态度，不能中立）
- 结尾（引导评论或留悬念）
3. 风格要求：
- 强口语化，像人在说话
- 句子短，每句尽量不超过15字
- 必须出现情绪词或冲突词，例如：炸了、离谱、危险、变天了
- 禁止 AI 味表达，例如：首先、其次、综上、值得注意的是、从某种意义上说
4. 内容要求：
- 只讲一个最炸的点，不要面面俱到
- 必须写出为什么重要
- 必须写出对普通人的影响
5. 只输出 JSON，不要解释，不要 markdown 代码块。"""


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
    available = [s for s in candidates if s.api_key and s.base_url]
    order = {name.strip(): idx for idx, name in enumerate(CLAUDE_SOURCE_ORDER.split(",")) if name.strip()}
    available.sort(key=lambda source: (order.get(source.name, len(order)), source.name))
    return available


def _normalize_base_url(base_url: str) -> str:
    """将 OpenAI 兼容源统一规范到 /v1 根路径。"""
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


def _normalize_hashtags(value: object) -> list[str]:
    if not isinstance(value, list):
        return ["AI", "科技", "热点"]

    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip().lstrip("#")
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) == 5:
            break
    return tags or ["AI", "科技", "热点"]


def _select_style_template(title: str, analysis: AnalysisResult) -> StyleTemplate:
    text = " ".join(
        [
            title,
            analysis.summary,
            analysis.core_point,
            analysis.why_it_matters,
            analysis.impact_on_people,
            analysis.stance_hint,
            *analysis.keywords,
        ]
    ).lower()

    if any(word in text for word in ("泄露", "封锁", "裁员", "风险", "安全", "危险", "争议", "lawsuit", "ban")):
        return STYLE_TEMPLATES[1]
    if any(word in text for word in ("开源", "免费", "创业", "机会", "开发者", "降价", "提效", "效率")):
        return STYLE_TEMPLATES[2]

    index = int(hashlib.sha1(title.encode("utf-8")).hexdigest(), 16) % len(STYLE_TEMPLATES)
    return STYLE_TEMPLATES[index]


def _few_shot_messages() -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for example_input, example_output in FEW_SHOTS:
        messages.append({"role": "user", "content": example_input})
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(example_output, ensure_ascii=False),
            }
        )
    return messages


def _build_user_message(
    title: str,
    analysis: AnalysisResult,
    template: StyleTemplate,
    previous_copy: CopyResult | None,
    review: CopyReviewResult | None,
    rewrite_count: int,
) -> str:
    parts = [
        f"热点标题：{title}",
        f"一句话摘要：{analysis.summary}",
        f"核心点：{analysis.core_point or analysis.summary}",
        f"为什么重要：{analysis.why_it_matters or '这会改变 AI 行业格局或普通人的使用成本。'}",
        f"对普通人的影响：{analysis.impact_on_people or '普通人会更直接感受到效率、成本或机会变化。'}",
        f"建议立场：{analysis.stance_hint or '请给出明确判断，不要中立。'}",
        f"关键词：{', '.join(analysis.keywords)}",
        f"本次风格模板：{template.label}",
        f"风格切入角度：{template.angle}",
        f"Hook 要求：{template.hook_hint}",
        f"语气要求：{template.stance_hint}",
    ]

    if previous_copy and review:
        suggestions = "；".join(review.suggestions) if review.suggestions else review.summary
        parts.extend(
            [
                "",
                f"这是第 {rewrite_count + 1} 次写文案，请基于评分反馈重写。",
                f"上一版标题：{previous_copy.title}",
                f"上一版脚本：{previous_copy.script}",
                f"评分总结：{review.summary}",
                f"必须修正的问题：{suggestions}",
                "重写要求：保留同一新闻事实，但 Hook 更狠、观点更明确、结尾更想让人评论。",
            ]
        )

    return "\n".join(parts)


def _call_source(source: ClaudeSource, messages: list[dict[str, str]]) -> CopyResult:
    """调用单个 Claude 源，返回 CopyResult（失败则抛出异常）。"""
    base_url = _normalize_base_url(source.base_url)
    client = OpenAI(
        api_key=source.api_key,
        base_url=base_url,
        timeout=CLAUDE_REQUEST_TIMEOUT,
        max_retries=0,
    )
    log.info(
        "calling source=%s base_url=%s model=%s timeout=%ss",
        source.name,
        base_url,
        source.model,
        int(CLAUDE_REQUEST_TIMEOUT),
    )
    resp = client.chat.completions.create(
        model=source.model,
        max_tokens=700,
        messages=messages,
        temperature=0.85,
    )
    raw = resp.choices[0].message.content or ""
    data = _parse_json_response(raw)
    return CopyResult(
        title=str(data["title"]).strip(),
        script=str(data["script"]).strip(),
        hashtags=_normalize_hashtags(data.get("hashtags", [])),
    )


def generate_copy(
    title: str,
    analysis: AnalysisResult,
    previous_copy: CopyResult | None = None,
    review: CopyReviewResult | None = None,
    rewrite_count: int = 0,
) -> CopyResult:
    template = _select_style_template(title, analysis)
    user_msg = _build_user_message(title, analysis, template, previous_copy, review, rewrite_count)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *_few_shot_messages(), {"role": "user", "content": user_msg}]

    sources = _load_sources()
    if not sources:
        raise RuntimeError(
            "no Claude sources configured: set CLAUDE_BASE_URL_ANY/CLAUDE_API_KEY_ANY "
            "or CLAUDE_BASE_URL_GEEK/CLAUDE_API_KEY_GEEK"
        )

    last_exc: Exception | None = None
    for source in sources:
        try:
            result = _call_source(source, messages)
            log.info(
                "copy generated via source=%s model=%s template=%s rewrite_count=%s",
                source.name,
                source.model,
                template.key,
                rewrite_count,
            )
            return result
        except Exception as exc:
            log.warning(
                "source=%s base_url=%s model=%s failed (%s): %s, trying next...",
                source.name,
                _normalize_base_url(source.base_url),
                source.model,
                type(exc).__name__,
                exc,
            )
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
            title=job.topic.title,
            analysis=job.analysis,
            previous_copy=job.copy,
            review=job.review,
            rewrite_count=job.copy_rewrite_count,
        )
    except Exception as exc:
        fail_job(msg.job_id, f"copywriting failed: {exc}")
        return

    job.stage = Stage.WRITE
    job.status = Status.DONE
    job.copy = result
    job.review = None
    advance_job(job)


if __name__ == "__main__":
    consume_queue(QUEUE_NAME, handle)
