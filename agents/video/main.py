"""Video Agent — 使用 TTS + FFmpeg 合成短视频（9:16 竖屏）。

视频结构：
  ┌─────────────────────┐
  │  左上轻量角标         │
  ├─────────────────────┤
  │                     │
  │   标题（入场动画）    │  ~160px
  │                     │
  │   背景：动态渐变      │
  │   或 Pexels 素材     │
  │                     │
  ├─────────────────────┤
  │  字幕（同步高亮）     │  底部 300px
  ├─────────────────────┤
  │  进度条              │  底部 10px
  └─────────────────────┘

背景策略（优先级）：
  1. output/assets/ 目录下已有素材视频
  2. PEXELS_API_KEY 已配置 → 按话题关键词下载
  3. 内置动态渐变（geq 波浪动画，无需外部资源）
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import edge_tts
import requests
from volcenginesdkarkruntime import Ark

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))
from shared.schema.job import Job, Stage, Status, StageMessage, VideoResult
from agents.base import consume_queue, advance_job, fail_job, log

QUEUE_NAME = "closeclaw.video"

OUTPUT_DIR    = os.getenv("VIDEO_OUTPUT_DIR", "output/videos")
ASSETS_DIR    = os.getenv("VIDEO_ASSETS_DIR", "output/assets")
TTS_DIR       = os.getenv("VIDEO_TTS_DIR",    "output/tts")
TTS_VOICE     = os.getenv("TTS_VOICE", "zh-CN-XiaoxiaoNeural")
VIDEO_GENERATION_BACKEND = os.getenv("VIDEO_GENERATION_BACKEND", "doubao_seedance").strip().lower()

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
VOLCENGINE_ARK_BASE_URL = os.getenv("VOLCENGINE_ARK_BASE_URL", "https://ark.cn-beijing.volces.com").rstrip("/")
VOLCENGINE_ARK_API_KEY = os.getenv("VOLCENGINE_ARK_API_KEY", "")
# 优先读取 VIDEO_MODEL，兼容旧变量 DOUBAO_SEEDANCE_MODEL。
DOUBAO_SEEDANCE_MODEL = os.getenv(
    "VIDEO_MODEL",
    os.getenv("DOUBAO_SEEDANCE_MODEL", "doubao-seedance-1-5-pro-251215"),
)
DOUBAO_SEEDANCE_DURATION = int(os.getenv("DOUBAO_SEEDANCE_DURATION", "5"))
DOUBAO_SEEDANCE_ASPECT_RATIO = os.getenv("DOUBAO_SEEDANCE_ASPECT_RATIO", "9:16")
DOUBAO_SEEDANCE_RESOLUTION = os.getenv("DOUBAO_SEEDANCE_RESOLUTION", "1080p")
DOUBAO_SEEDANCE_POLL_INTERVAL_SEC = float(os.getenv("DOUBAO_SEEDANCE_POLL_INTERVAL_SEC", "5"))
DOUBAO_SEEDANCE_TIMEOUT_SEC = int(os.getenv("DOUBAO_SEEDANCE_TIMEOUT_SEC", "180"))

VIDEO_WIDTH  = 1080
VIDEO_HEIGHT = 1920
ACCENT_COLOR = "0x6c63ff"   # 品牌紫
BG_COLOR     = "0x0a0e2e"   # 深海蓝（无素材时的背景底色）


# ── 字体 ─────────────────────────────────────────────────────────────────────

def find_chinese_font() -> str:
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            log.info("using font: %s", path)
            return path
    log.warning("no Chinese font found; drawtext may show boxes")
    return ""


# ── TTS + SRT ─────────────────────────────────────────────────────────────────

async def _tts_with_subtitles(script: str, audio_path: str, srt_path: str) -> None:
    communicate = edge_tts.Communicate(script, TTS_VOICE)
    submaker    = edge_tts.SubMaker()
    with open(audio_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                submaker.feed(chunk)
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(submaker.get_srt())
    log.info("tts done audio=%s srt=%s", audio_path, srt_path)


# ── 背景素材 ───────────────────────────────────────────────────────────────────

def get_local_asset() -> str | None:
    """返回 assets 目录中随机一个视频文件路径，没有则返回 None。"""
    assets = Path(ASSETS_DIR)
    if not assets.exists():
        return None
    videos = list(assets.glob("*.mp4")) + list(assets.glob("*.mov"))
    if not videos:
        return None
    import random
    return str(random.choice(videos))


def download_pexels_video(keywords: list[str]) -> str | None:
    """从 Pexels 按关键词搜索并下载第一个横屏视频，缓存到 assets。"""
    if not PEXELS_API_KEY:
        return None

    query = " ".join(keywords[:3])
    cache_key = hashlib.md5(query.encode()).hexdigest()[:8]
    cache_path = os.path.join(ASSETS_DIR, f"pexels_{cache_key}.mp4")

    if os.path.exists(cache_path):
        log.info("pexels cache hit: %s", cache_path)
        return cache_path

    try:
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "orientation": "portrait", "per_page": 5},
            timeout=15,
        )
        resp.raise_for_status()
        videos = resp.json().get("videos", [])
        if not videos:
            return None

        # 选时长最接近 60s 的素材
        best = min(videos, key=lambda v: abs(v.get("duration", 0) - 60))
        # 选分辨率最高的文件
        files = best.get("video_files", [])
        files = [f for f in files if f.get("quality") in ("hd", "sd")]
        if not files:
            return None
        files.sort(key=lambda f: f.get("width", 0) * f.get("height", 0), reverse=True)
        video_url = files[0]["link"]

        log.info("downloading pexels video: %s", video_url)
        Path(ASSETS_DIR).mkdir(parents=True, exist_ok=True)
        with requests.get(video_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(cache_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        log.info("pexels video saved: %s", cache_path)
        return cache_path

    except Exception as exc:
        log.warning("pexels download failed: %s", exc)
        return None


def get_classic_background_video(keywords: list[str]) -> str | None:
    """旧背景视频模式：本地素材 > Pexels > None（内置动画）。"""
    local = get_local_asset()
    if local:
        return local
    return download_pexels_video(keywords)


def _seedance_sdk_base_url() -> str:
    base_url = VOLCENGINE_ARK_BASE_URL.rstrip("/")
    if not base_url.endswith("/api/v3"):
        base_url = f"{base_url}/api/v3"
    return base_url


@functools.lru_cache(maxsize=1)
def _seedance_client() -> Ark:
    if not VOLCENGINE_ARK_API_KEY:
        raise RuntimeError("VOLCENGINE_ARK_API_KEY is not set")
    return Ark(
        api_key=VOLCENGINE_ARK_API_KEY,
        base_url=_seedance_sdk_base_url(),
    )


def _normalize_prompt_text(text: str, limit: int = 120) -> str:
    cleaned = " ".join(part.strip() for part in text.replace("\n", " ").split() if part.strip())
    return cleaned[:limit]


def _pick_seedance_visual_plan(title: str, script: str, keywords: list[str]) -> dict[str, str]:
    source = f"{title} {script} {' '.join(keywords)}".lower()

    plan = {
        "theme": "futuristic AI newsroom, premium technology motion design",
        "subject": "abstract AI labs, datacenters, glowing chips, neural network energy flow",
        "motion": "slow push-in, smooth orbit camera, layered parallax, elegant motion graphics",
        "tone": "tense, high-stakes, sharp contrast, cinematic, credible, not flashy",
        "shots": (
            "shot 1: strong opening with futuristic city lights or datacenter exterior; "
            "shot 2: move into chips, screens, server racks, model activity, signal flow; "
            "shot 3: end on a powerful wide shot with scale, tension, and momentum"
        ),
    }

    if any(token in source for token in ("浏览器", "browser", "入口", "search", "搜索")):
        plan.update(
            subject="futuristic browser interface, floating windows, search panels, user-entry portals, AI assistant overlays",
            motion="clean dolly movement, layered UI parallax, subtle camera drift, premium interface animation",
            tone="strategic, competitive, modern, polished, high-value product launch energy",
            shots=(
                "shot 1: dramatic reveal of futuristic browser interface and glowing search gateway; "
                "shot 2: multiple layered panels, AI assistant cards, user traffic flowing through the interface; "
                "shot 3: wide hero shot showing the browser as the new digital entry point"
            ),
        )
    elif any(token in source for token in ("芯片", "算力", "gpu", "server", "datacenter", "机房")):
        plan.update(
            subject="macro shots of advanced chips, server racks, cooling systems, datacenter corridors, electric signal flow",
            motion="macro tracking shots, dramatic rack focus, strong depth, controlled mechanical movement",
            tone="industrial, powerful, expensive, tense, infrastructure-level importance",
            shots=(
                "shot 1: dramatic datacenter corridor with cold blue lighting; "
                "shot 2: macro close-up of chips, boards, cooling fans, energy pulses; "
                "shot 3: large-scale compute cluster hero shot with massive industrial power"
            ),
        )
    elif any(token in source for token in ("机器人", "robot", "agent", "自动化", "workflow", "干活")):
        plan.update(
            subject="AI agents operating interfaces, humanoid silhouettes, robotic systems, autonomous workflows, task orchestration visuals",
            motion="confident tracking shots, interface transitions, elegant robotic motion, workflow nodes activating",
            tone="efficient, unstoppable, slightly tense, high-productivity future",
            shots=(
                "shot 1: autonomous AI workflow wakes up with multiple tasks activating; "
                "shot 2: robotic or agent-like systems operating screens and coordinated processes; "
                "shot 3: strong payoff shot showing scale and efficiency of autonomous execution"
            ),
        )
    elif any(token in source for token in ("开源", "创业", "机会", "应用层", "产品")):
        plan.update(
            subject="builders, startup war rooms, AI product screens, rapid prototyping tables, energetic tech workspace",
            motion="fast but controlled camera movement, layered reveal, momentum and upward energy",
            tone="opportunity-driven, urgent, ambitious, energetic, optimistic but competitive",
            shots=(
                "shot 1: ambitious tech workspace with glowing product screens; "
                "shot 2: prototype interfaces, code-inspired visuals, fast execution energy; "
                "shot 3: strong hero shot suggesting new opportunity and market opening"
            ),
        )

    return plan


def _build_seedance_prompt(title: str, script: str, keywords: list[str]) -> str:
    short_title = _normalize_prompt_text(title, limit=40)
    short_script = _normalize_prompt_text(script, limit=180)
    kw = ", ".join(keywords[:6]) if keywords else "AI, technology, futuristic"
    plan = _pick_seedance_visual_plan(title, script, keywords)

    return "\n".join(
        [
            "Generate a premium vertical 9:16 AI news background video.",
            "This is a background plate for a short-form AI commentary video, not the final edited video.",
            "",
            f"Topic title: {short_title}",
            f"Topic keywords: {kw}",
            f"Topic context: {short_script}",
            "",
            "Goal:",
            "Create a cinematic, visually rich, believable technology scene that supports a strong spoken commentary track.",
            "The background must feel expensive, modern, dynamic, and suitable for an AI industry breaking-news short.",
            "",
            "Visual direction:",
            f"- Theme: {plan['theme']}",
            f"- Main subject: {plan['subject']}",
            f"- Camera and motion: {plan['motion']}",
            f"- Tone and mood: {plan['tone']}",
            f"- Shot progression: {plan['shots']}",
            "",
            "Composition rules:",
            "- Keep the center area and lower-third visually readable for later subtitles and title overlays.",
            "- Use strong depth, layered composition, cinematic lighting, and controlled movement.",
            "- Use realistic detail, premium textures, volumetric light, reflections, particles, screens, and energy flow when appropriate.",
            "- Avoid crowded framing and avoid distracting elements that fight with the narration.",
            "",
            "Negative constraints:",
            "- No subtitles, no captions, no logos, no watermarks, no brand names, no readable text, no news lower-thirds.",
            "- No presenter speaking to camera, no selfie framing, no meme style, no cheap stock-footage look.",
            "- No flat slideshow, no low-detail animation, no oversaturated colors, no comedic tone.",
            "",
            "Output should feel like a polished AI-industry visual background for a high-retention short video.",
        ]
    )


def _extract_sdk_video_url(task) -> str:
    content = getattr(task, "content", None)
    if content is None:
        raise RuntimeError(f"Seedance task has no content: {task}")

    for field in ("video_url", "file_url"):
        candidate = getattr(content, field, None)
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate

    raise RuntimeError(f"Seedance task has no downloadable video url: {task}")


def _poll_seedance_video(task_id: str):
    client = _seedance_client()
    deadline = time.time() + DOUBAO_SEEDANCE_TIMEOUT_SEC

    while time.time() < deadline:
        task = client.content_generation.tasks.get(task_id=task_id)
        status = (getattr(task, "status", "") or "").lower()

        if status in {"succeeded", "success", "completed"}:
            return task
        if status in {"failed", "error", "cancelled", "canceled"}:
            error = getattr(task, "error", None)
            if error is not None:
                raise RuntimeError(f"Seedance task failed: {getattr(error, 'code', '')} {getattr(error, 'message', '')}".strip())
            raise RuntimeError(f"Seedance task failed: {task}")

        log.info("seedance task=%s status=%s", task_id, status or "unknown")
        time.sleep(DOUBAO_SEEDANCE_POLL_INTERVAL_SEC)

    raise RuntimeError(f"Seedance task timed out after {DOUBAO_SEEDANCE_TIMEOUT_SEC}s: {task_id}")


def _is_seedance_fast_2_model() -> bool:
    return "2-0-fast" in DOUBAO_SEEDANCE_MODEL or "2.0-fast" in DOUBAO_SEEDANCE_MODEL


def _seedance_prompt_with_model_controls(prompt: str) -> str:
    if _is_seedance_fast_2_model():
        return prompt

    # 1.5 Pro 兼容调用：将时长 / 镜头固定 / 水印等控制参数内联到文本里。
    return (
        f"{prompt}\n"
        f"--duration {DOUBAO_SEEDANCE_DURATION} "
        f"--camerafixed false "
        f"--watermark false"
    )


def _seedance_create_params(prompt: str) -> dict:
    content = [{"type": "text", "text": _seedance_prompt_with_model_controls(prompt)}]
    params = {
        "model": DOUBAO_SEEDANCE_MODEL,
        "content": content,
    }

    # Seedance 2.0 Fast 继续使用顶层参数控制。
    if _is_seedance_fast_2_model():
        params["duration"] = DOUBAO_SEEDANCE_DURATION
        params["ratio"] = DOUBAO_SEEDANCE_ASPECT_RATIO
        params["watermark"] = False

    return params


def generate_seedance_background_video(title: str, script: str, keywords: list[str]) -> str:
    client = _seedance_client()
    prompt = _build_seedance_prompt(title, script, keywords)
    cache_key = hashlib.md5(
        f"{DOUBAO_SEEDANCE_MODEL}|{DOUBAO_SEEDANCE_DURATION}|{DOUBAO_SEEDANCE_ASPECT_RATIO}|{title}|{script}|{'/'.join(keywords)}".encode(
            "utf-8"
        )
    ).hexdigest()[:12]
    cache_path = os.path.join(ASSETS_DIR, f"seedance_{cache_key}.mp4")
    if os.path.exists(cache_path):
        log.info("seedance cache hit: %s", cache_path)
        return cache_path

    Path(ASSETS_DIR).mkdir(parents=True, exist_ok=True)
    task = client.content_generation.tasks.create(**_seedance_create_params(prompt))
    task_id = getattr(task, "id", "")
    if not task_id:
        raise RuntimeError(f"Seedance create returned no task id: {task}")
    log.info("seedance task created id=%s model=%s", task_id, DOUBAO_SEEDANCE_MODEL)

    result_task = _poll_seedance_video(task_id)
    video_url = _extract_sdk_video_url(result_task)

    with requests.get(video_url, stream=True, timeout=120) as download_resp:
        download_resp.raise_for_status()
        with open(cache_path, "wb") as f:
            for chunk in download_resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

    log.info("seedance video saved: %s", cache_path)
    return cache_path


def _load_background_video(title: str, script: str, keywords: list[str]) -> str | None:
    # 默认启用火山 Doubao Seedance；如需切回旧模式，请把 VIDEO_GENERATION_BACKEND 改成 classic。
    if VIDEO_GENERATION_BACKEND == "classic":
        return get_classic_background_video(keywords)

    if VIDEO_GENERATION_BACKEND in {"doubao_seedance", "doubao-seedance", "seedance", "doubao"}:
        return generate_seedance_background_video(title, script, keywords)

    raise RuntimeError(
        f"unsupported VIDEO_GENERATION_BACKEND={VIDEO_GENERATION_BACKEND!r}; "
        "use doubao_seedance or classic"
    )


# ── 视频合成 ───────────────────────────────────────────────────────────────────

def _escape(text: str) -> str:
    return (
        text
        .replace("\\", "\\\\")
        .replace("'", "\u2019")
        .replace(":", "\\:")
        .replace("%", "\\%")
    )


@functools.lru_cache(maxsize=1)
def _ffmpeg_filters_output() -> str:
    """返回 ffmpeg -filters 输出，用于能力探测。"""
    try:
        result = subprocess.run(
            ["ffmpeg", "-filters"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""

    return f"{result.stdout}\n{result.stderr}"


def ffmpeg_supports_filter(name: str) -> bool:
    """检测当前 ffmpeg 是否内置指定滤镜。"""
    filters_output = _ffmpeg_filters_output()
    return f" {name} " in filters_output or filters_output.strip().endswith(name)


def ffmpeg_supports_subtitles() -> bool:
    return ffmpeg_supports_filter("subtitles")


def ffmpeg_supports_drawtext() -> bool:
    return ffmpeg_supports_filter("drawtext")


def _build_filter(
    font: str,
    safe_title: str,
    srt_path: str,
    has_bg_video: bool,
    duration: float,
) -> tuple[str, str]:
    """
    构建 filter_complex 字符串，返回 (filter_complex, out_label)。
    """
    font_opt  = f":fontfile='{font}'" if font else ""
    font_name = Path(font).stem if font else "sans"

    has_srt = bool(srt_path and os.path.exists(srt_path) and os.path.getsize(srt_path) > 0)
    has_drawtext = ffmpeg_supports_drawtext()

    if has_bg_video:
        # 背景视频：缩放 + 裁剪适配竖屏
        bg = (
            f"[0:v]"
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
            f"format=yuv420p"
            f"[bg_raw];"
            # 叠加半透明暗化层，让文字更易读
            f"[bg_raw]"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.45:thickness=fill"
            f"[bg];"
        )
        audio_idx = "1"
    else:
        # 静态渐变背景（深海蓝底 + 中部浅色晕 + 顶/底暗角）
        # 用多层 drawbox 近似径向渐变，避免 geq 慢编码
        bg = (
            f"[0:v]"
            # 中间高亮区（模拟中心发光）
            f"drawbox=x=0:y=400:w={VIDEO_WIDTH}:h=1120:color=0x0f1a3e@0.9:thickness=fill,"
            # 顶部暗角
            f"drawbox=x=0:y=0:w={VIDEO_WIDTH}:h=350:color=0x050812@1:thickness=fill,"
            # 底部暗角
            f"drawbox=x=0:y=1570:w={VIDEO_WIDTH}:h=350:color=0x050812@1:thickness=fill,"
            # 左侧竖向渐变条（品牌紫）
            f"drawbox=x=0:y=0:w=8:h={VIDEO_HEIGHT}:color={ACCENT_COLOR}@0.8:thickness=fill"
            f"[bg];"
        )
        audio_idx = "1"

    # 左上角轻量品牌角标，避免整条新闻栏压住画面
    brand = (
        f"[bg]"
        f"drawbox=x=42:y=42:w=18:h=58:color={ACCENT_COLOR}@0.95:thickness=fill,"
        f"drawbox=x=72:y=34:w=230:h=74:color=black@0.35:thickness=fill"
    )
    if has_drawtext:
        brand += (
            f",drawtext=text='CloseClaw'{font_opt}:fontsize=34:fontcolor=white@0.88:"
            f"x=86:y=54"
        )
    brand += "[branded];"

    # 标题（前 0.4s 从屏幕外滑入）
    if has_drawtext:
        title = (
            f"[branded]"
            f"drawtext=text='{safe_title}'{font_opt}:"
            f"fontsize=64:fontcolor=white:"
            f"x=(w-text_w)/2:"
            f"y=if(gte(t\\,0.4)\\,160\\,160+(-0.4+t)*(-500)):"
            f"shadowcolor=black@0.9:shadowx=3:shadowy=3:"
            f"borderw=2:bordercolor=black@0.4"
            f"[titled];"
        )
    else:
        title = "[branded]copy[titled];"

    # 底部字幕区渐变遮罩
    sub_mask = (
        f"[titled]"
        f"drawbox=x=0:y={VIDEO_HEIGHT - 340}:w=iw:h=340:color=black@0.55:thickness=fill"
        f"[masked];"
    )

    # 进度条（底部 12px，随播放时间增长）
    if duration > 0:
        progress = (
            f"[masked]"
            f"drawbox=x=0:y={VIDEO_HEIGHT - 12}:w=iw:h=12:color=black@0.6:thickness=fill,"
            f"drawbox=x=0:y={VIDEO_HEIGHT - 12}:"
            f"w='iw*t/{duration:.1f}':h=12:color={ACCENT_COLOR}@0.9:thickness=fill"
            f"[progress];"
        )
        prev = "[progress]"
    else:
        progress = ""
        prev = "[masked]"

    # 字幕 subtitles 滤镜
    if has_srt:
        escaped_srt = (
            srt_path
            .replace("\\", "/")
            .replace(":", "\\:")
            .replace(",", "\\,")
            .replace("'", r"\'")
        )
        style = (
            f"FontName={font_name},"
            f"Fontsize=44,"
            f"PrimaryColour=&H00ffffff,"
            f"OutlineColour=&H00000000,"
            f"Outline=2,"
            f"Bold=1,"
            f"Alignment=2,"
            f"MarginV=100"
        )
        style = style.replace(",", r"\,").replace("'", r"\'")
        subs = f"{prev}subtitles=filename='{escaped_srt}':force_style='{style}'[v]"
        out = "[v]"
    else:
        # 无字幕时直接输出
        subs = f"{prev}copy[v]"
        out = "[v]"

    fc = bg + brand + title + sub_mask + progress + subs
    return fc, out, audio_idx


def compose_video(
    audio_path: str,
    srt_path: str,
    output_path: str,
    title: str,
    script: str,
    keywords: list[str],
) -> int:
    font       = find_chinese_font()
    safe_title = _escape(title[:20])
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # 获取音频时长用于进度条
    try:
        from mutagen.mp3 import MP3
        audio_duration = MP3(audio_path).info.length
    except Exception:
        audio_duration = 0.0

    bg = _load_background_video(title, script, keywords)

    if bg:
        inputs = ["-stream_loop", "-1", "-i", bg, "-i", audio_path]
        log.info("using background video backend=%s path=%s", VIDEO_GENERATION_BACKEND, bg)
    else:
        inputs = [
            "-f", "lavfi",
            "-i", f"color=c={BG_COLOR}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:r=30",
            "-i", audio_path,
        ]
        log.info("using built-in animated gradient background")

    subtitle_supported = ffmpeg_supports_subtitles()
    if srt_path and not subtitle_supported:
        log.warning("ffmpeg subtitles filter unavailable; generating video without burned-in subtitles")
        srt_path = ""
    if not ffmpeg_supports_drawtext():
        log.warning("ffmpeg drawtext filter unavailable; generating video without title text overlays")

    fc, out, audio_idx = _build_filter(font, safe_title, srt_path, bool(bg), audio_duration)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-shortest",
        "-filter_complex", fc,
        "-map", out,
        "-map", f"{audio_idx}:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]

    log.info("ffmpeg start title='%s' bg=%s srt=%s", title[:20], bool(bg), bool(srt_path))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-1000:]}")

    log.info("ffmpeg done: %s", output_path)
    return int(audio_duration) or 60


def _title_to_filename(title: str) -> str:
    """将标题转换为安全的文件名。
    - 中文字符限 10 个
    - 非中文字符限 20 个
    - 其余特殊字符替换为下划线
    """
    import re
    import unicodedata

    # 去掉控制字符和不可见字符
    title = "".join(c for c in title if unicodedata.category(c)[0] != "C")

    # 判断是否以中文为主
    chinese_chars = [c for c in title if "\u4e00" <= c <= "\u9fff"]
    if chinese_chars:
        # 保留前 10 个中文字符（含中文的整段截取）
        kept, count = [], 0
        for c in title:
            if "\u4e00" <= c <= "\u9fff":
                count += 1
            if count > 10:
                break
            kept.append(c)
        name = "".join(kept)
    else:
        name = title[:20]

    # 将文件系统不允许的字符替换为 _
    name = re.sub(r'[\\/*?:"<>|！？。，、；：""''【】（）《》…—\s]+', "_", name)
    name = name.strip("_")
    return name or "video"


def generate_thumbnail(video_path: str, thumb_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-i", video_path,
           "-ss", "00:00:02", "-vframes", "1", thumb_path]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log.warning("thumbnail generation failed")


# ── Handler ───────────────────────────────────────────────────────────────────

def handle(msg: StageMessage) -> None:
    log.info("video start job_id=%s", msg.job_id)

    job = Job.from_json(msg.payload.decode())
    if not job.copy:
        fail_job(msg.job_id, "no copy in job")
        return

    keywords    = job.analysis.keywords if job.analysis else []

    video_id    = str(uuid.uuid4())
    base_name   = _title_to_filename(job.copy.title)

    # 同名文件加序号后缀避免覆盖
    candidate   = os.path.join(OUTPUT_DIR, f"{base_name}.mp4")
    if os.path.exists(candidate):
        suffix = video_id[:6]
        base_name = f"{base_name}_{suffix}"

    audio_path  = os.path.join(TTS_DIR,    f"{video_id}.mp3")
    srt_path    = os.path.join(TTS_DIR,    f"{video_id}.srt")
    output_path = os.path.join(OUTPUT_DIR, f"{base_name}.mp4")
    thumb_path  = os.path.join(OUTPUT_DIR, f"{base_name}_thumb.jpg")

    Path(TTS_DIR).mkdir(parents=True, exist_ok=True)

    try:
        asyncio.run(_tts_with_subtitles(job.copy.script, audio_path, srt_path))
        duration = compose_video(audio_path, srt_path, output_path, job.copy.title, job.copy.script, keywords)
        generate_thumbnail(output_path, thumb_path)
    except Exception as exc:
        fail_job(msg.job_id, f"video generation failed: {exc}")
        return

    job.stage = Stage.VIDEO
    job.status = Status.DONE
    job.video = VideoResult(
        file_path=output_path,
        duration_sec=duration,
        thumbnail_path=thumb_path,
    )
    advance_job(job)


if __name__ == "__main__":
    consume_queue(QUEUE_NAME, handle)
